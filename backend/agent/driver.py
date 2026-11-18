"""主 agent 手写 driver:把线性 RAG 重构成显式 State + (state)->state 节点。
节点边界即《架构定稿》§2.2 的五框。现为单域(通用制度),故:
  - ① 路由恒返回通用域、plan 单层;≥2 域时这里出多域 plan
  - ④ 融合节点单域用不到,跳过(见 run())
LangGraph 迁移时:run() 的顺序调用换 add_conditional_edges,节点函数几乎照搬(定稿 §10)。
本文件是对旧 app.ask 内联逻辑的**等价重构**——golden 分数必须与重构前一字不差。
"""
import json
import re

import store, rag
from agent import tools_finance
from config import TOP_K, features, model_tag, DEFAULT_MODEL_ID, domain_for, domain_cfg

REFUSAL = "抱歉,在你当前权限可见的知识库中没有找到相关信息。建议补充相关文档,或咨询对应负责人。"
L6_REFUSAL = ("抱歉,本次回答未通过数字核对(答案中出现了工具结果与资料之外的数字),"
              "为避免给出错误金额已拒绝作答,请联系财务人工确认。")

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _norm_num(n):
    """750.0 与 750 是同一个数(实测 L6 误杀根因:工具参数 float 化后 JSON 带 .0)。"""
    return n.rstrip("0").rstrip(".") if "." in n else n


def numbers_outside_evidence(answer, evidence_texts):
    """L6 数字机械核对(③机械审核,纯代码):答案里的每个数字必须能在证据里找到——
    证据 = 引用 chunk 原文 ∪ 工具输入输出 ∪ 用户问题本身。找不到的就是编造嫌疑,返回可疑数字列表。
    ponytail: 只认阿拉伯数字(TOOL_RULE 已要求模型用阿拉伯数字);中文数字是已知天花板。"""
    allowed = set()
    for t in evidence_texts:
        allowed |= {_norm_num(x) for x in _NUM_RE.findall(t or "")}
    ans = re.sub(r"\[资料\d+\]", "", answer or "")  # 引用编号不算数字
    return sorted({_norm_num(x) for x in _NUM_RE.findall(ans)} - allowed)


async def _retrieve(state, q):
    """检索一次:版本只影响检索方式(纯向量 vs 混合),不影响下游闸门松紧(风险 #6)。"""
    qemb = (await rag.embed([q]))[0]
    if features().get("advanced_search"):
        return store.hybrid_search(qemb, q, TOP_K, state.depts)
    return store.query(qemb, TOP_K, state.depts)


def route(state):
    """① 拆解路由:按关键词判问题归哪个域,选闸门参数。域路由不改权限,但**无权访问某专业域的用户
    不得被路由进去**——否则该域的工具/硬编码政策数据会绕过检索权限咽喉成为越权后门(里程碑 C 审查 A1)。
    判据:域声明的受限部门 dept 必须在用户可见部门内;admin(depts=None)不受限。回退=默认通用域。"""
    did = domain_for(state.question)
    cfg = domain_cfg(did)
    req = cfg.get("dept")
    if did != "general" and req and state.depts is not None and req not in state.depts:
        did, cfg = "general", domain_cfg("general")  # 无权此域 → 回退,工具授权随之收回
    state.domain = did
    state.cfg = cfg
    return state


async def execute(state):
    """② 分发执行 = 检索 + L1 改写双路(权力容器见定稿 §3),按域 cfg 过三值闸门。
    原句腿用原句词过闸;改写腿只保留"擦不到 direct 线的候选块"里够 boundary 的——
    改写只许加召回不许升态:改写救回的块最高只能进 boundary,不能凭改写升到 direct(防漂移伪装权威)。
    改写固定用默认快模型(定稿 §9),全程落审计(state.rewritten)。"""
    cfg = state.cfg
    mode, hits = store.evidence_gate(await _retrieve(state, state.question), state.question, cfg)
    if state.history or mode == "refused":
        rw = await rag.rewrite_query(state.question, state.history, model_tag(DEFAULT_MODEL_ID))
        if rw and rw != state.question:
            state.rewritten = rw
            rw_hits = await _retrieve(state, rw)
            # 改写腿单独判态:strict_kw 语义在 boundary_min 之上仍要求长词——沿用擦边天花板
            rw_mode, rw_pass = store.evidence_gate(rw_hits, rw, cfg)
            if rw_mode != "refused":
                merged = {h["chunk_id"]: h for h in hits}
                for h in rw_pass:
                    old = merged.get(h["chunk_id"])
                    if not old or (h.get("score") or 0) > (old.get("score") or 0):
                        merged[h["chunk_id"]] = h
                pool = sorted(merged.values(), key=lambda h: h.get("score") or 0, reverse=True)[:TOP_K]
                # 并集后按原句重判态:改写只提供候选,最终定态仍看原句证据强度(向量漂移防线)
                mode, hits = store.evidence_gate(pool, state.question, cfg)
                if mode == "refused" and rw_mode == "boundary":
                    mode, hits = "boundary", pool  # 改写救回的边缘证据,封顶 boundary
    # L2 域的计算类问题:chunk 向量擦边是常态(个案数字 vs 通则条文),闸门判死前给工具轮一次机会。
    # 有效工具结果 = 读"已确认政策库"的确定性产出,证据级别高于 chunk → 升 direct(数字全程受 L6 白名单管)。
    if mode == "refused" and "L2" in (state.cfg.get("allowed_powers") or []):
        trace = await rag.plan_tools(state.question, state.history, model_tag(DEFAULT_MODEL_ID),
                                     tools_finance.SCHEMAS, tools_finance.run_tool)
        if any(not (t.get("result") or {}).get("error") for t in trace):
            state.tool_trace = trace
            mode = "direct"
    state.answer_mode = mode
    state.hits = hits
    return state


def review(state):
    """③ 机械审核:三值闸门产出的 mode 即决策,纯代码(定稿铁律 2)。
    refused=拒答;boundary/direct 都出答案但走不同模板。财务域上线后这里加 L6 数字自检。"""
    state.refused = (state.answer_mode == "refused")
    return state


async def _generate(state, question):
    """生成一次答案:财务域(L2 已授权)走"规划轮→作文轮"两段式,其余走普通生成。
    工具路径固定用默认快模型——tool-calling 能力依模型而异,推理模型(r1)不吃这套(定稿 §9 节点级模型)。
    execute 阶段可能已预跑规划轮(闸门救回场景),此时不重复规划,直接作文。"""
    powers = state.cfg.get("allowed_powers") or []
    if "L2" in powers:
        if not state.tool_trace:
            state.tool_trace = await rag.plan_tools(question, state.history,
                                                    model_tag(DEFAULT_MODEL_ID),
                                                    tools_finance.SCHEMAS, tools_finance.run_tool)
        return await rag.compose_with_tools(question, state.history, state.hits,
                                            state.tool_trace, model_tag(DEFAULT_MODEL_ID))
    return await rag.answer(question, state.history, state.hits,
                            model_tag(state.model_id), mode=state.answer_mode)


async def respond(state):
    """⑤ 出口:拒答走模板;direct 权威口吻;boundary 三段式;财务域带 L2 工具循环。
    L6 数字机械核对属 ③机械审核的职责,在生成侧执行以便就地重试(≤1 次,二次失败诚实拒答)。"""
    if state.refused:
        state.answer = REFUSAL
    else:
        state.answer = await _generate(state, state.question)
        if "L6" in (state.cfg.get("allowed_powers") or []):
            evidence = ([h.get("text", "") for h in state.hits] + [state.question] +
                        [json.dumps(t, ensure_ascii=False) for t in state.tool_trace])
            bad = numbers_outside_evidence(state.answer, evidence)
            if bad:
                retry_q = (state.question +
                           f"\n(系统提示:你上次的回答包含了工具结果与资料之外的数字 {bad},"
                           f"禁止使用这些数字,请重新作答)")
                state.answer = await _generate(state, retry_q)
                evidence = ([h.get("text", "") for h in state.hits] + [state.question] +
                            [json.dumps(t, ensure_ascii=False) for t in state.tool_trace])
                bad = numbers_outside_evidence(state.answer, evidence)
                if bad:
                    state.answer_mode, state.refused = "refused", True
                    state.answer = L6_REFUSAL
    state.qid = store.log_qa(state.question, state.answer, state.user["role"],
                             refused=state.refused, conversation_id=state.conversation_id,
                             username=state.user["username"], rewritten=state.rewritten,
                             answer_mode=state.answer_mode,
                             tool_trace=json.dumps(state.tool_trace, ensure_ascii=False) if state.tool_trace else None)
    return state


async def run(state):
    state = route(state)          # ①
    state = await execute(state)  # ②
    state = review(state)         # ③
    # ④ 条件融合:单域无跨域子答案可融合,跳过(≥2 域时补)
    state = await respond(state)  # ⑤
    return state
