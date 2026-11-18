"""Step 2.4 群发:模板/S 素材集/机检/选材/SMTP(spec docs/superpowers/specs/2026-07-15-bulk-mail-design.md)。

权力结构:收件人/称呼/岗位/时间/地点=代码机械填充(I3);LLM 仅有 S 内选材+固定 PHRASING 措辞 两权
(均为索引选择,无自由文本);发送动作永远等人一次确认(I2)。"""
import re
import json
import fairness
from config import LICENSED_TO
from resume import _DECISION_WORDS

STATUS_GATE = {"约面": ("约面",), "跟进": ("已面", "评估"), "拒信": ("淘汰",)}

TEMPLATES = {
    "约面": {"version": "约面v1",
        "subject": "「{job_title}」岗位面试邀请 · {company}",
        "body": ("{name},你好:\n\n"
                 "感谢你应聘 {company} 的「{job_title}」岗位。{p13n}\n\n"
                 "我们诚挚邀请你参加面试:\n时间:{interview_time}\n地点:{location}\n{note_line}"
                 "如时间不便,请直接回复本邮件协调。\n\n{company} 招聘团队")},
    "跟进": {"version": "跟进v1",
        "subject": "「{job_title}」岗位进展更新 · {company}",
        "body": ("{name},你好:\n\n"
                 "感谢你参加 {company}「{job_title}」岗位的面试。{p13n}\n\n"
                 "你的后续安排我们正在推进,有结果会第一时间与你联系。{note_line}\n\n{company} 招聘团队")},
    "拒信": {"version": "拒信v1",
        "subject": "「{job_title}」岗位申请结果 · {company}",
        "body": ("{name},你好:\n\n"
                 "感谢你对 {company}「{job_title}」岗位的关注与投入。{p13n}\n\n"
                 "经慎重考虑,本次我们暂不能与你继续推进。你的资料我们会妥善保留,"
                 "后续有更契合的机会将主动与你联系。{note_line}\n\n祝求职顺利。\n\n{company} 招聘团队")},
}

# 个性化句:按信型各一句(改措辞=改代码=重新审定,同模板纪律)。约面/跟进提"经历";
# 拒信只致谢其展示的能力、不提"契合/匹配"(避免"很匹配却拒了你"的事故文案)。None=该信型不个性化,走纯骨架。
PHRASING = {
    "约面": "我们特别关注到你在{v}方面的经历,期待与你进一步交流。",
    "跟进": "你在{v}方面的经历我们已认真了解,后续安排会尽快与你同步。",
    "拒信": "感谢你在{v}方面展示的积累与投入。",
}


def build_S(evaluation, frozen):
    """素材集 S(spec §3 governing 块,Approach 1)。

    结构保证(承重,勿"顺手优化"):
    - 签名无 candidate 参数——候选人字节在类型层就不可达,这是删掉注入向量的方式;
    - 每个 value 逐字节读自 frozen["dimensions"][i]["name"](HR 冻结产物);
    - evaluation 只贡献布尔(evidence_status=="confirmed")与整数(evidence_level),
      其 evidence / note / evidence_json 字节在本边界丢弃,不进返回值任何字段;
    - 无评分 / 评分 rubric_id 对不上当前冻结版 → 返回 [](调用方走 no_material 例外 → 纯骨架)。
    """
    if not (evaluation and frozen and evaluation.get("rubric_id") == frozen.get("id")):
        return []
    confirmed = {str(it["dimension_id"]): int(it.get("evidence_level") or 0)
                 for it in evaluation.get("items", [])
                 if it.get("evidence_status") == "confirmed"}          # 布尔边界:字节止步于此
    out = []
    for d in frozen.get("dimensions", []):          # _rubric_dimensions ORDER BY position → S 顺序确定
        did = str(d["id"])
        if did not in confirmed or d.get("protected"):                  # protected=列过滤,结构性(第二层;
            continue                                                    # 第一层是冻结校验 talent_store.py:383)
        v = (d.get("name") or "").strip()
        if not v or fairness.scan_protected(v):                         # house 文本仍扫,便宜(governing 块保留项)
            continue
        out.append({"id": f"dim:{did}",                                 # send_items.material_id 直接存这个
                    "value": v,                                         # 唯一可进邮件的字符串,⊆ frozen
                    "source": f"rubric:{frozen['id']}:{did}",           # send_items.material_source,纯 ID
                    "weight": int(d.get("weight") or 0),                # LLM 排序信号(HR 定的数)
                    "evidence_level": confirmed[did]})                  # LLM 排序信号(评分时定的 0/2/4 整数)
    return out


def render_mail(kind, name, job_title, interview_time, location, note, sentence):
    """机械渲染(I3):全部槽位来自代码入参;sentence 为空=纯骨架。"""
    t = TEMPLATES[kind]
    p13n = (sentence.strip() + " ") if sentence and sentence.strip() else ""
    note_line = f"备注:{note}\n" if note else ""
    subject = t["subject"].format(job_title=job_title, company=LICENSED_TO)
    body = t["body"].format(name=name, company=LICENSED_TO, job_title=job_title, p13n=p13n,
                            interview_time=interview_time, location=location, note_line=note_line)
    return subject, body


_DECISION_WORDS_SAFE = _DECISION_WORDS  # app.py 经此引用,不直接 import resume 私名

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")  # 与 app.py 同一把尺(app 依赖本模块,不能反向 import)

# F-CSV-1:导出 CSV 的单元格公式/DDE 注入中和(Task 10 对抗审查发现)。
_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def csv_field(value):
    """导出 CSV 单元格:先中和电子表格公式注入,再做 RFC4180 引号转义。
    姓名/正文是候选人可控字节,导出后 HR 用 Excel/LibreOffice/Sheets 打开——首字符 = + - @(含 \\t \\r)
    会被当公式求值(=HYPERLINK/=IMPORTDATA 可外传同表 PII,=cmd 触 DDE)。危险首字符前置单引号 → 按文本处理。
    引号内前置单引号仍有效:电子表格公式解析器看到的首字符是 ' 而非 =,故不求值(F-CSV-1)。"""
    s = "" if value is None else str(value)
    if s and s[0] in _CSV_FORMULA_LEAD:
        s = "'" + s
    return '"' + s.replace('"', '""') + '"'


EXC_REASONS = {
    "no_material":       "找不到这位候选人确认过的、和这个岗位相关的亮点,不敢瞎写——已附上纯模板版,可直接发。",
    "material_invalid":  "自动选出的亮点没通过成文机检(例如维度名恰好撞了录用/淘汰类决策词),已改发纯模板版。",
    "material_protected": "为他选的素材含有不该用于沟通的个人属性,已拦下——可换素材或发纯模板版。",
    "status_gate":       "他当前的候选状态和这封信对不上(比如拒信批里混进了约面状态的人),发出去就是事故——请先核对状态。",
    "bad_email":         "没有能用的邮箱地址——补上邮箱后可重试,或先跳过。",
    "duplicate":         "这个岗位的这类信他已经收到过,防止重复打扰——默认跳过,确有需要可强制重发。",
}


def machine_check(candidate, kind, body_text, material_id, whitelist_ids, has_prior):
    """机检七条,全部确定性。返回 checks 七项明细 + 第一个触发的例外键(spec §4.1/§4.2)。"""
    checks = {
        "recipient_bound": True,  # 结构性成立:to_name/to_email 只能取自 candidate 行(端点装配处无第二来源)
        "status_gate": candidate.get("status") in STATUS_GATE[kind],
        "email_valid": bool(_EMAIL_RE.match(candidate.get("email") or "")),
        "not_duplicate": not has_prior,
        "material_whitelisted": material_id is None or material_id in whitelist_ids,
        "protected_clean": not fairness.scan_protected(body_text),
        "decision_clean": not any(w in body_text for w in _DECISION_WORDS),
    }
    exception = None
    if not checks["status_gate"]:
        exception = "status_gate"
    elif not checks["email_valid"]:
        exception = "bad_email"
    elif not checks["not_duplicate"]:
        exception = "duplicate"
    elif not checks["material_whitelisted"]:
        exception = "material_invalid"
    return {"checks": checks, "ok": all(checks.values()), "exception": exception}


# ===== Step 2.4 群发 · 单封装配咽喉(Task 4 → Approach 1 定稿:发信路径零 LLM)=====
# 裁决(fable-ai-outreach-thesis.md,用户拍板):S 由本岗位冻结评分构造,岗位相关性是构造保证;
# LLM 在"选哪条"上只剩 weight/evidence_level 两信号,被一行确定性 argmax 严格支配(且更可解释、无 llm_failed
# 故障面)。故删 select_material/_SelectOut/_SELECT_SYSTEM,发信路径不再有任何模型调用。
def assemble_item(candidate, kind, job_title, interview_time, location, note, S, has_prior):
    """单封装配唯一入口。candidate 只供收件人/状态(I3 + 状态门),绝不供素材。
    选材=确定性 argmax(证据等级, 权重),平手取 S 前序(=rubric position 序,稳定,max 返回首个最大值)。
    🟡T1-C:material_id/value/source 全取自 S 元素→material_source 结构恒为 rubric:*、material_value 恒 ∈ S。
    出站个性化字节 = PHRASING[kind] 填 house 维度名,与模型、候选人输入无关(Approach 1 承重不变量)。"""
    name = candidate.get("name") or ""
    skel_subject, skel_body = render_mail(kind, name, job_title, interview_time, location, note, "")

    def item(status, subject, body, material=None, wording=None, exc_key=None, checks=None):
        return {"candidate_id": candidate.get("id"), "to_name": name,
                "to_email": candidate.get("email") or "", "subject": subject, "body": body,
                "material_id": material and material["id"],
                "material_value": material and material["value"],
                "material_source": material and material["source"],
                "wording": wording, "template_version": TEMPLATES[kind]["version"],
                "machine_checks": json.dumps(checks or {}, ensure_ascii=False),
                "status": status, "exception_reason": exc_key and EXC_REASONS[exc_key]}

    # 1. 不依赖成文的硬门(状态/邮箱/防重)
    pre = machine_check(candidate, kind, skel_body, None, set(), has_prior)
    if pre["exception"]:
        return item("exception", skel_subject, skel_body, exc_key=pre["exception"], checks=pre["checks"])
    # 2. 空 S → no_material 骨架(未评分/无 confirmed 维度=已评分专属的既定后果)
    if not S:
        return item("exception", skel_subject, skel_body, exc_key="no_material", checks=pre["checks"])
    # 3. 确定性选材:证据等级×权重最高;平手取 S 前序
    material = max(S, key=lambda e: (e.get("evidence_level") or 0, e.get("weight") or 0))
    # 4. 该信型的个性化句(PHRASING[kind]=None → 纯骨架);house 维度名填进固定模板
    tmpl = PHRASING.get(kind)
    sentence = tmpl.format(v=material["value"]) if tmpl else ""
    subject, body = render_mail(kind, name, job_title, interview_time, location, note, sentence)
    # 5. 成文机检(素材 id ∈ S 复核 + 成文 fairness/决策词;维度名撞决策词/受保护→降骨架,T1-E)
    chk = machine_check(candidate, kind, body, material["id"], {e["id"] for e in S}, has_prior)
    if not chk["ok"]:
        return item("exception", skel_subject, skel_body,
                    exc_key=(chk["exception"] or "material_invalid"), checks=chk["checks"])
    return item("pending", subject, body, material=material, wording="rule", checks=chk["checks"])


# ===== Step 2.4 群发 · SMTP 发送器(Task 5,传输安全硬化见 fix task-5-tls-fix-brief.md)=====
import time as _time
import ssl
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import config as _cfg


def smtp_configured():
    return bool(_cfg.SMTP_HOST and _cfg.SMTP_USER and _cfg.SMTP_PASS)


def _connect():
    """465 走 SMTP_SSL(校验证书的默认上下文);非 465 走 SMTP+STARTTLS,同样校验证书。
    SMTP_INSECURE 打开时改用不校验上下文(自签/内网场景);服务器不支持 STARTTLS 且未开
    该逃生阀 → 直接拒绝,判为连接失败(T-SEC-1:默认拒明文,绝不退回明文发送)。"""
    insecure = getattr(_cfg, "SMTP_INSECURE", False)
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    is_ssl = _cfg.SMTP_PORT == 465
    if is_ssl:
        conn = smtplib.SMTP_SSL(_cfg.SMTP_HOST, _cfg.SMTP_PORT, timeout=15, context=ctx)
    else:
        conn = smtplib.SMTP(_cfg.SMTP_HOST, _cfg.SMTP_PORT, timeout=15)
    try:  # 连接已建立:此后任何异常都先关连接再抛,杜绝 fd 泄漏(review Minor #1 根因修法)
        if not is_ssl:
            conn.ehlo()
            if conn.has_extn("starttls"):
                conn.starttls(context=ctx)
                conn.ehlo()
            elif not insecure:
                raise smtplib.SMTPException(
                    "服务器不支持 STARTTLS,拒绝明文发送(设 SMTP_INSECURE=1 显式放行)")
            # insecure 且服务器无 STARTTLS:允许明文(逃生阀语义,操作者已显式承担风险)
        conn.login(_cfg.SMTP_USER, _cfg.SMTP_PASS)
    except Exception:
        _safe_quit(conn)
        raise
    return conn


def _safe_quit(conn):
    if conn is not None:
        try:
            conn.quit()
        except Exception:
            pass


def send_items_smtp(items, on_result, throttle=None):
    """逐封发送:每封独立记账不回滚(spec §5)。单封 send_message 失败=隔离故障,断连重连
    一次(原语义保留);_connect() 本身失败(含重连时)=系统性故障——当前+所有剩余 item 立即
    标失败并熔断退出,不再逐封重复 15s 超时连接(T-SEC-3:黑洞主机下 _connect 调用次数 O(1),
    不是 O(N))。finally 保证连接必被关闭,即便 on_result 回调抛错也是如此(异常仍会向上
    传播,T-SEC-4)。"""
    throttle = _cfg.MAIL_THROTTLE_S if throttle is None else throttle
    conn, sent_n, failed_n = None, 0, 0
    try:
        idx = 0
        while idx < len(items):
            it = items[idx]
            err, connect_failed = None, False
            for attempt in (1, 2):  # 至多重连一次(隔离故障原语义)
                try:
                    if conn is None:
                        try:
                            conn = _connect()
                        except Exception:
                            connect_failed = True
                            raise
                    msg = MIMEText(it["body"], "plain", "utf-8")
                    msg["Subject"] = Header(it["subject"], "utf-8")
                    msg["From"] = _cfg.SMTP_FROM
                    msg["To"] = it["to_email"]
                    conn.send_message(msg)
                    err = None
                    break
                except Exception as e:
                    err = str(e)[:200]
                    _safe_quit(conn)
                    conn = None
                    if connect_failed:
                        break  # _connect() 本身失败 = 系统性,本层不再重试
            if connect_failed:
                # 系统性连接失败:当前 + 所有剩余标失败,立即熔断退出(不再逐封重复连接)
                for rest in items[idx:]:
                    failed_n += 1
                    on_result(rest, False, err)
                break
            if err is None:
                sent_n += 1
                on_result(it, True, None)
            else:
                failed_n += 1
                on_result(it, False, err)
            idx += 1
            if throttle:
                _time.sleep(throttle)
    finally:
        _safe_quit(conn)
    return sent_n, failed_n
