import io, json, re
import httpx
from config import OLLAMA_URL, EMBED_MODEL, CHUNK_SIZE, CHUNK_OVERLAP

SYSTEM_PROMPT = (
    "你是企业内部知识库 AI 助手。只能依据【提供的资料】回答员工问题。\n"
    "规则:\n"
    "1. 只使用资料中的信息作答,不得使用资料之外的常识,不得编造。\n"
    "2. 在答案相应位置用 [资料N] 标注引用来源。\n"
    "3. 如果资料中找不到答案,只回答:抱歉,知识库中暂未找到相关信息,建议补充相关文档或咨询对应负责人。\n"
    "4. 用简洁、清晰的中文回答,不要寒暄。"
)

# 边界答(answer_mode=boundary):资料相关但未直接规定时的第三态。
# 设计动机(三态回答模型):不给推断修容器,模型会偷偷推断且不打标(r08 实证:
# 用午休段落答"能否健身"时直接断言"可以健身"=无据断言伪装有据)。允许+强制标注,比禁止更安全。
BOUNDARY_PROMPT = (
    "你是企业内部知识库 AI 助手。用户的问题在资料中【没有直接规定】,但资料包含相关背景。\n"
    "严格按以下三段作答,不得省略第 2 段:\n"
    "1. 给出资料中与问题相关的规定原意,用 [资料N] 标注;\n"
    "2. 明确声明:制度未对用户所问的事项作出直接规定;\n"
    "3. 如需给出建议或常识推断,必须以「制度未明文规定,以下仅供参考:」开头,不超过一句话。\n"
    "绝不虚构资料中不存在的数字、时间、名称。用简洁中文,不要寒暄。"
)


REWRITE_PROMPT = (
    "你是检索词改写器。把用户的最新问题改写成一句独立、完整、书面语的检索问题。\n"
    "规则:\n"
    "1. 只输出改写后的那一句问题,不要任何解释或前缀。\n"
    "2. 结合对话历史补全指代(如「那满3年呢?」要还原成完整问题)。\n"
    "3. 口语词换成制度文档的书面用语(如「大屏幕」→「外接显示器」)。\n"
    "4. 不得添加用户没有表达的意图。\n"
    "5. 不得引入原问题和历史中不存在的实体或场所(例如不得把「公司」写成「学校」)。"
)


async def rewrite_query(question, history, model):
    """L1 改写:输出仅作检索输入,永不作为事实展示(权力容器见《架构定稿》§3)。
    失败或输出异常时回退原句——改写只许加召回,不许减召回。"""
    msgs = [{"role": "system", "content": REWRITE_PROMPT}]
    for turn in (history or [])[-4:]:
        msgs.append(turn)
    msgs.append({"role": "user", "content": f"最新问题:{question}\n改写后的检索问题:"})
    try:
        out = await chat(msgs, model)
        out = (out or "").strip().splitlines()[0].strip().strip('"「」')
    except Exception:
        return question
    return out[:100] if out else question  # 长度上限=容器,防改写跑飞


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")


def _dense_len(text: str) -> int:
    """去掉所有空白字符后的"实际字数",用来判断到底抽出了多少内容。"""
    return len(re.sub(r"\s", "", text or ""))


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_control(text):
    """PDF 字体子集化偶发把字形解码成 NUL 或其它控制字符(实测某简历含 21 个 \x00),
    会截断 SQLite length()、污染 LLM 提示、并让证据逐字回贴失败 → 统一在解析出口清除;
    仅保留 \n\r\t,不动正常文字。"""
    return _CTRL_RE.sub("", text or "")


def parse_file(filename: str, raw: bytes):
    """上传文件解析唯一入口(简历与 KB 文档共用);出口统一清控制字符,见 _strip_control。"""
    text, needs_review = _parse_file(filename, raw)
    return _strip_control(text), needs_review


def _parse_file(filename: str, raw: bytes):
    """解析上传文件,返回 (text, needs_review)。
    needs_review=True 表示 OCR 低置信行占比过高——识别结果可用但不可全信,文档应标「待确认」,
    防止拍糊的数字以权威口吻出现在答案里(风险 #9)。非 OCR 路径恒为 False。"""
    name = (filename or "").lower()

    # 图片(扫描件/拍照件/截图):直接走本地 OCR
    if name.endswith(IMAGE_EXTS):
        import ocr
        text, total, low = ocr.ocr_image_bytes(raw)
        return text, (total > 0 and low / total > ocr.REVIEW_RATIO)

    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        n_pages = len(reader.pages)
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        # 判断是不是"扫描件/图片型 PDF":几乎抽不出文字(平均每页不足约 10 个字)
        # → 自动把每页转成图片走 OCR。文字版 PDF 不受影响,照旧走快速路径。
        if _dense_len(text) < max(10, 10 * n_pages):
            import ocr
            ocr_text, total, low = ocr.ocr_pdf_bytes(raw)
            if _dense_len(ocr_text) > _dense_len(text):
                return ocr_text, (total > 0 and low / total > ocr.REVIEW_RATIO)
        return text, False

    if name.endswith(".docx"):
        # 按文档顺序遍历段落和表格:.paragraphs 不含表格,而真实制度的额度/档位恰恰都在表格里(风险 #8)。
        # 表格每行转 "表头:值" 对,并带上表格前最近一段文字作题注,让行数据自带出处。
        import docx  # noqa: 下方 return 均为 (text, False),docx 无置信度概念
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        d = docx.Document(io.BytesIO(raw))
        parts, last_para = [], ""
        for el in d.element.body.iterchildren():
            if el.tag.endswith("}p"):
                p = Paragraph(el, d)
                if p.text.strip():
                    parts.append(p.text)
                    last_para = p.text.strip()
            elif el.tag.endswith("}tbl"):
                rows = Table(el, d).rows
                if len(rows) < 2:
                    continue  # ponytail: 单行表没有表头+数据结构,当没看见;需要时再议
                header = [c.text.strip() for c in rows[0].cells]
                caption = f"[{last_para[:30]}] " if last_para else ""
                for r in rows[1:]:
                    cells = [c.text.strip() for c in r.cells]
                    line = ";".join((f"{h}:{v}" if h else v) for h, v in zip(header, cells) if v)
                    if line:
                        parts.append(caption + line)
        return "\n".join(parts), False

    # txt / md / csv / 其他:按文本解码
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc), False
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore"), False


def chunk_text(text: str):
    paras = [p.strip() for p in re.split(r"\n{1,}", text or "") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 1 <= CHUNK_SIZE:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                chunks.append(buf); buf = ""
            if len(p) <= CHUNK_SIZE:
                buf = p
            else:
                i = 0
                while i < len(p):
                    chunks.append(p[i:i + CHUNK_SIZE])
                    i += CHUNK_SIZE - CHUNK_OVERLAP
    if buf:
        chunks.append(buf)
    return chunks or [text.strip()] if text.strip() else []


async def embed(texts):
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{OLLAMA_URL}/api/embed", json={"model": EMBED_MODEL, "input": texts})
        r.raise_for_status()
        return r.json()["embeddings"]


async def chat_raw(messages, model, tools=None, format=None):
    """底层对话原语:可带工具 schema / 约束解码 format(JSON schema)。所有 LLM 调用的唯一出口。"""
    payload = {"model": model, "messages": messages, "stream": False,
               "options": {"temperature": 0.2}}
    if tools:
        payload["tools"] = tools
    if format is not None:
        payload["format"] = format  # I4:Pydantic model_json_schema(),约束解码保 JSON 格式
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]


def _clean(content):
    # 去除 DeepSeek-R1 等推理模型的 <think>…</think> 段,只保留最终答案
    return re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()


async def chat(messages, model):
    return _clean((await chat_raw(messages, model))["content"])


PLAN_PROMPT = (
    "你是财务计算规划器。判断用户问题是否涉及金额/天数/限额/报销单的计算:\n"
    "需要计算 → 调用相应工具(可先算天数再算额度,可多次调用);\n"
    "不需要计算 → 只回复「无需计算」。\n"
    "你只负责调工具,不要回答问题本身,不要自己做任何算术。\n"
    "抽取参数务必逐字数准:「住宿3晚」→nights=3,「餐补3天/按3天算」→days=3,"
    "「住N晚」的 N 就是 nights,不得填 0 或漏填;每个必填参数都要给。"
)

TOOL_RULE = ("\n5. 【工具计算结果】是系统用确定性代码算出的可信数字,金额/天数结论以它为准;"
             "答案中的数字只能来自工具结果或资料原文,一律使用阿拉伯数字。")


async def plan_tools(question, history, model, tools_schemas, run_tool, max_rounds=3):
    """L2 工具规划轮:只给问题+工具(裸上下文,决策专注),循环 ≤max_rounds(容器:防无界)。
    实测教训:资料上下文+作文任务和工具决策混在同一轮时,qwen 会因为资料里有单价而放弃
    调工具直接心算(被 L6 拦下但可用性归零)——所以规划轮必须裸。返回 tool_trace。"""
    trace = []
    plan_msgs = [{"role": "system", "content": PLAN_PROMPT}]
    for turn in (history or [])[-2:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            plan_msgs.append({"role": turn["role"], "content": turn["content"]})
    plan_msgs.append({"role": "user", "content": question})
    for _ in range(max_rounds):
        msg = await chat_raw(plan_msgs, model, tools_schemas)
        calls = msg.get("tool_calls") or []
        if not calls:
            break
        plan_msgs.append(msg)
        for c in calls:
            fn = (c.get("function") or {})
            name, args = fn.get("name"), fn.get("arguments") or {}
            result = run_tool(name, args)
            trace.append({"tool": name, "args": args, "result": result})
            plan_msgs.append({"role": "tool", "content": json.dumps(result, ensure_ascii=False)})
    return trace


async def compose_with_tools(question, history, hits, trace, model):
    """L2 作文轮:资料+工具结果、不给工具,只负责组稿。工具结果=确定性代码产出,可信度最高。"""
    context = "\n\n".join(
        f"【资料{i+1}】(来自《{h['doc_title']}》)\n{h['text']}" for i, h in enumerate(hits)
    )
    if trace:
        context += "\n\n【工具计算结果】\n" + "\n".join(
            f"{t['tool']}({json.dumps(t['args'], ensure_ascii=False)}) → "
            f"{json.dumps(t['result'], ensure_ascii=False)}" for t in trace)
    messages = [{"role": "system", "content": SYSTEM_PROMPT + TOOL_RULE}]
    for turn in (history or [])[-6:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": f"提供的资料:\n{context}\n\n员工问题:{question}"})
    return await chat(messages, model)


async def answer(question, history, hits, model, mode="direct"):
    context = "\n\n".join(
        f"【资料{i+1}】(来自《{h['doc_title']}》)\n{h['text']}" for i, h in enumerate(hits)
    )
    messages = [{"role": "system", "content": BOUNDARY_PROMPT if mode == "boundary" else SYSTEM_PROMPT}]
    for turn in (history or [])[-6:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": f"提供的资料:\n{context}\n\n员工问题:{question}"})
    return await chat(messages, model)
