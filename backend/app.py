import os, secrets, datetime, uuid, re, hashlib, time
from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Depends, Body, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import store, rag, talent_store, resume, fairness, rubric, mailer
from agent import driver, tools_finance
from agent.state import AskState
from config import (role_depts, ROLES, EMBED_MODEL, MAX_UPLOAD_MB, MAX_UPLOAD_BYTES,
                    EDITION, EDITION_NAMES, LICENSE_SEATS, LICENSE_EXPIRES, LICENSED_TO, features,
                    LLM_MODELS, DEFAULT_MODEL_ID, EVAL_MODEL_ID, model_name, model_tag)

SEED_DOCS = [
    ("员工手册(节选)", "全员",
     "工作时间:标准工作时间为周一至周五 9:00–18:00,午休 12:00–13:30。实行弹性打卡,每日有效工作时长不少于 8 小时。\n"
     "请假:员工请假需提前在系统提交申请。事假按天扣减当月工资;病假凭医院证明,全年累计 5 天内按全薪计算。年假根据司龄计算,满 1 年享 5 天,满 3 年享 10 天。\n"
     "加班:加班须事先经直属主管审批。工作日加班可申请调休;法定节假日加班按规定支付 3 倍工资。"),
    ("差旅与报销制度", "财务",
     "报销额度:市内交通据实报销;出差住宿一线城市每晚不超过 600 元,其他城市每晚不超过 400 元;餐补每人每天 100 元。\n"
     "发票要求:所有报销须提供合规发票,发票抬头为公司全称;餐饮发票需备注事由与参与人。\n"
     "报销流程:在系统提交报销单并上传发票,经直属主管与财务两级审批后,款项于次月 15 日前打入工资卡。"),
    ("新员工入职指南", "全员",
     "第一天:到前台领取门禁卡与办公设备;IT 会在入职当天为你开通邮箱、企业微信与知识库账号。\n"
     "设备:公司统一配发笔记本电脑;如需外接显示器可向行政申请,设备领用需签署资产登记表。\n"
     "培训:入职首周完成线上合规与安全培训;有任何制度问题,可直接在知识库 AI 问答中提问。"),
    ("客户支持常见问题(FAQ)", "销售",
     "部署方式:产品支持私有化部署,数据完全留在客户自有服务器内,不上传公有云。\n"
     "试用:提供 15 天免费试用,试用期内可体验基础版全部功能。\n"
     "价格:基础版一次性 49,800 元,含 50 个用户授权;专业版、企业版按年订阅。"),
    ("系统运维与备份规范", "技术",
     "备份策略:系统每周进行一次全量备份,每日进行增量备份,备份保留 30 天。\n"
     "数据恢复:发生故障时,数据恢复目标时间小于 1 小时;灾难恢复目标时间小于 4 小时。\n"
     "监控:使用 Prometheus + Grafana 监控系统状态,出现异常自动告警。"),
]

SESSIONS = {}  # token -> username(单进程内存会话;重启需重新登录)


def license_info():
    try:
        exp = datetime.date.fromisoformat(LICENSE_EXPIRES)
    except Exception:
        exp = datetime.date(2099, 12, 31)
    days_left = (exp - datetime.date.today()).days
    return {"edition": EDITION, "edition_name": EDITION_NAMES.get(EDITION, EDITION),
            "licensed_to": LICENSED_TO, "seats": LICENSE_SEATS, "seats_used": store.count_users(),
            "expires": LICENSE_EXPIRES, "days_left": days_left, "expired": days_left < 0,
            "features": features()}


def current_user(authorization: Optional[str] = Header(None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    username = SESSIONS.get(token)
    if not username:
        raise HTTPException(401, "未登录或登录已失效")
    u = store.get_user(username)
    if not u:
        raise HTTPException(401, "用户不存在")
    return u

def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user

def require_hr(user=Depends(current_user)):
    # I7 权限咽喉:人才库仅 HR 内容角色可见;admin 是系统管理权非内容权,同样 403。
    if user["role"] not in {"hr", "hr_rubric_editor"}:
        raise HTTPException(403, "需要人事权限")
    return user

def require_rubric_editor(user=Depends(current_user)):
    if user["role"] != "hr_rubric_editor":
        raise HTTPException(403, "需要人事评分配置权限")
    return user

def visible_depts(user):
    # 部门隔离是安全边界,不是付费功能;所有版本一律按角色隔离(风险 #3)
    return role_depts(user["role"])


async def read_capped(file):
    """🔴R1b 上传上限:防 file.read() 无上限的内存 DoS。文档与简历上传共用唯一入口。"""
    size = getattr(file, "size", None)
    if size is not None and size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB}MB 上限")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)  # ponytail: 至多读 MAX+1,内存有界;multipart 落盘 DoS 属 ASGI 层,内部工具不拦
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB}MB 上限")
    return raw


async def ingest_document(title, dept, text, status="已生效"):
    """统一入库:切分 → 每 chunk 拼文档级前缀 → embed → 存储,返回 (doc_id, n_chunks) 或 None。
    前缀让孤立句子(如"每晚不超过 600 元")在向量空间里自带出身;存储文本同步带前缀,引用展示一致。"""
    chunks = rag.chunk_text(text)
    if not chunks:
        return None
    prefixed = [f"《{title}》·{dept}\n{c}" for c in chunks]
    embs = await rag.embed(prefixed)
    return store.add_document(title, dept, prefixed, embs, status), len(prefixed)


async def seed():
    if store.count_users() == 0:
        store.create_user("admin", "admin123", "admin")
        store.create_user("finance", "finance123", "finance")
        store.create_user("hr", "hr123", "hr")
        store.create_user("staff", "staff123", "staff")
        print("[seed] 已创建演示账号:admin/finance/hr/staff(默认密码见 README)")
    # 评分配置者是 HR 工作台演示闭环所必需的最小专用角色；已有库只补此账号，不重置任何用户。
    if not store.get_user("hr_editor"):
        store.create_user("hr_editor", "hreditor123", "hr_rubric_editor")
        print("[seed] 已补齐评分配置演示账号:hr_editor")
    if store.list_documents():
        return
    for title, dept, content in SEED_DOCS:
        try:
            await ingest_document(title, dept, content)
        except Exception as e:
            print(f"[seed] 示例文档跳过(Ollama 未就绪): {e}")
            return
    print("[seed] 已写入示例文档")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    talent_store.init()  # 人才库独立物理分库(data/talent.sqlite,与 kb.sqlite 平级);检索层永不触及(I1)
    await seed()
    yield


app = FastAPI(title="知识库 AI Agent", lifespan=lifespan)
# ponytail: 前端由本 app 同源托管(见文件末尾 index),同源请求无需 CORS;删掉原 allow_origins=["*"]
# 收紧到同源(风险 Y5)。将来若前端独立部署再按需加白名单,别再放通配。


class LoginBody(BaseModel):
    username: str
    password: str
class AskBody(BaseModel):
    # 会话历史由服务端按 conversation_id 持有(qa_logs 串链),不再接受客户端上传 history:
    # 客户端可伪造 assistant 轮次操纵答案,且审计日志看不见——见《架构定稿.md》风险 #1
    question: str
    conversation_id: Optional[str] = None
    model: Optional[str] = None
class TextDoc(BaseModel):
    title: str
    dept: str = "全员"
    content: str
class SearchBody(BaseModel):
    q: str
class FeedbackBody(BaseModel):
    qid: str
    rating: int
class NewUser(BaseModel):
    username: str
    password: str
    role: str = "staff"
class TalentCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    years: Optional[float] = None
    skills: Optional[str] = None
    education: Optional[str] = None
    expected_salary: Optional[str] = None
    experience: Optional[str] = None
    source_file: Optional[str] = None
class TalentStatus(BaseModel):
    status: str
    reason: str = Field(min_length=1, max_length=200)
class TalentArchive(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
class JdGapBody(BaseModel):
    jd: Optional[str] = None
    job_id: Optional[str] = None      # 双入口:选岗位(取岗位 JD)或临时粘贴 jd
class MessageDraftBody(BaseModel):
    kind: str
    job_title: Optional[str] = None
    interview_time: Optional[str] = None
    location: Optional[str] = None
    note: Optional[str] = None
class InterviewQuestionsBody(BaseModel):
    jd: Optional[str] = None
    gap_items: List[dict] = Field(default_factory=list)
class JobBody(BaseModel):
    title: str
    jd_text: str = ""
class JobUpdate(BaseModel):
    title: Optional[str] = None
    jd_text: Optional[str] = None
    status: Optional[str] = None
class JobAttach(BaseModel):
    candidate_id: str
class RubricDimensionBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    jd_requirement: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=500)
    kind: str = "核心"
    weight: int = Field(ge=0, le=100)
    evidence_rule: str = Field(default="", max_length=500)
    group_name: str = Field(default="", max_length=40)
    source_type: str = Field(default="", max_length=40)
    source_quote: str = Field(default="", max_length=500)
    importance_reason: str = Field(default="", max_length=500)
    importance_level: Optional[str] = None
    rank: Optional[int] = None
    assessment_mode: Optional[str] = None
    interview_probe: str = Field(default="", max_length=500)
    confidence: str = Field(default="", max_length=20)
class RubricCreateBody(BaseModel):
    high_priority_threshold: int = Field(default=75, ge=0, le=100)
    dimensions: List[RubricDimensionBody] = Field(default_factory=list)
    hiring_focus: str = Field(default="", max_length=2000)
class RubricDraftBody(BaseModel):
    hiring_focus: str = Field(default="", max_length=2000)
class RubricUpdateBody(BaseModel):
    high_priority_threshold: Optional[int] = Field(default=None, ge=0, le=100)
    dimensions: Optional[List[RubricDimensionBody]] = None
class RebalanceBody(BaseModel):
    dimensions: List[dict] = Field(default_factory=list)
class CheckpointBody(BaseModel):
    dimension_id: str
    checkpoint_status: str


# ---------- 公开 ----------
@app.get("/api/health")
def health():
    return {"ok": True, "models": [{"id": m["id"], "name": m["name"]} for m in LLM_MODELS],
            "embed": EMBED_MODEL, "edition": EDITION, "edition_name": EDITION_NAMES.get(EDITION, EDITION)}

@app.post("/api/login")
def login(body: LoginBody):
    if license_info()["expired"]:
        raise HTTPException(403, "授权已过期,请联系供应商续期")
    u = store.verify_login(body.username, body.password)
    if not u:
        raise HTTPException(401, "用户名或密码错误")
    token = secrets.token_hex(24)
    SESSIONS[token] = u["username"]
    return {"token": token, "user": u, "license": license_info()}

@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(None)):
    SESSIONS.pop((authorization or "").replace("Bearer ", "").strip(), None)
    return {"ok": True}

@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"user": user, "license": license_info(),
            "roles": {k: v["name"] for k, v in ROLES.items()},
            "depts": visible_depts(user),  # 前端按可见部门决定显示哪些工作台入口(None=admin 全可见)
            "models": [{"id": m["id"], "name": m["name"]} for m in LLM_MODELS],
            "default_model": DEFAULT_MODEL_ID}


# ---------- 文档 ----------
@app.get("/api/documents")
def documents(user=Depends(current_user)):
    return store.list_documents(visible_depts(user))

@app.get("/api/stats")
def stats(user=Depends(current_user)):
    return store.stats(visible_depts(user))

@app.post("/api/documents")
async def add_text_document(body: TextDoc, user=Depends(require_admin)):
    res = await ingest_document(body.title, body.dept, body.content)
    if not res:
        raise HTTPException(400, "内容为空")
    return {"id": res[0], "n_chunks": res[1]}

@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...), dept: str = Form("全员"),
                          title: Optional[str] = Form(None), user=Depends(require_admin)):
    raw = await read_capped(file)
    text, needs_review = rag.parse_file(file.filename, raw)
    res = await ingest_document(title or file.filename, dept, text,
                                status="待确认" if needs_review else "已生效")
    if not res:
        raise HTTPException(400, "扫描件识别置信度过低,无法可靠入库,请提供更清晰的版本" if needs_review
                            else "未能从文件解析出文本")
    return {"id": res[0], "n_chunks": res[1], "status": "待确认" if needs_review else "已生效"}

@app.delete("/api/documents/{doc_id}")
def remove_document(doc_id: str, user=Depends(require_admin)):
    store.delete_document(doc_id)
    return {"ok": True}


# ---------- 检索 / 问答 ----------
@app.post("/api/search")
def search(body: SearchBody, user=Depends(current_user)):
    return {"hits": store.keyword_search(body.q, visible_depts(user))}

@app.post("/api/ask")
async def ask(body: AskBody, user=Depends(current_user)):
    # 瘦控制器:组装 State(含权限咽喉 depts、服务端会话 history)→ 交主 agent driver → 拼契约。
    # 问答逻辑全在 agent/driver.py 的五节点里(定稿 §2.2)。
    cid = body.conversation_id or ""
    if not (cid.startswith("c_") and len(cid) == 12):  # 只认服务端签发格式,伪造发新会话
        cid = "c_" + uuid.uuid4().hex[:10]
    st = AskState(question=body.question, user=user,
                  model_id=body.model or DEFAULT_MODEL_ID, conversation_id=cid,
                  depts=visible_depts(user), history=store.get_history(cid, user["username"]))
    st = await driver.run(st)
    return {"answer": st.answer, "sources": st.hits, "refused": st.refused,
            "answer_mode": st.answer_mode, "domain": st.domain,
            "tool_trace": st.tool_trace,  # 完整轨迹(工具/参数/结果),前端渲染计算明细卡;仅财务腿跑过才非空
            "model": model_name(st.model_id), "conversation_id": cid,
            "rewritten": st.rewritten, "qid": st.qid}

@app.post("/api/feedback")
def feedback(body: FeedbackBody, user=Depends(current_user)):
    store.rate_qa(body.qid, body.rating)
    return {"ok": True}


# ---------- 财务工具(工作台直调,与问答共用 run_tool 唯一入口) ----------
@app.post("/api/tools/finance/{name}")
def finance_tool(name: str, args: dict = Body(default={}), user=Depends(current_user)):
    # 权限=可见部门含"财务"(与 A1 修复同一条边界:非财务用户不得经任何路径用财务工具)。
    # 执行走 tools_finance.run_tool——工作台与问答唯一入口,杜绝第二条执行路径漂移(架构不变量 I5)。
    depts = visible_depts(user)
    if depts is not None and "财务" not in depts:
        raise HTTPException(403, "无权使用财务工具")
    if name not in tools_finance.TOOLS:
        raise HTTPException(404, "未知工具")
    return tools_finance.run_tool(name, args)


# ---------- 人才库(HR;独立物理分库 talent.sqlite,require_hr 咽喉,I1/I7) ----------
@app.get("/api/talent")
def talent_list(status: Optional[str] = None, min_years: Optional[float] = None,
                skill: Optional[str] = None, education: Optional[str] = None,
                limit: int = 30, offset: int = 0,
                user=Depends(require_hr)):
    if status and not talent_store.valid_status(status):
        raise HTTPException(400, "状态无效")
    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(400, "分页参数无效")
    page = talent_store.list_candidate_summaries(status, min_years, skill, education, limit, offset)
    return {**page, "limit": limit, "offset": offset, "statuses": list(talent_store.STATUSES)}

@app.get("/api/talent/{cid}")
def talent_get(cid: str, user=Depends(require_hr)):
    c = talent_store.get_candidate(cid)
    if not c:
        raise HTTPException(404, "候选人不存在")
    rz = talent_store.get_resume(cid)
    c["original_text"] = rz["raw_text"] if rz else None  # 确认页对照原文(前端过 esc,I6)
    return c

@app.post("/api/talent")
def talent_create(body: TalentCreate, user=Depends(require_hr)):
    cid = talent_store.create_candidate(
        name=body.name, actor=user["username"], phone=body.phone, email=body.email,
        years=body.years, skills=body.skills, education=body.education,
        expected_salary=body.expected_salary, experience=body.experience, source_file=body.source_file)
    return {"id": cid}

@app.patch("/api/talent/{cid}/status")
def talent_status(cid: str, body: TalentStatus, user=Depends(require_hr)):
    if not talent_store.valid_status(body.status):
        raise HTTPException(400, "状态无效")
    candidate = talent_store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "候选人不存在")
    if not talent_store.transition_candidate(cid, body.status, user["username"], body.reason):
        raise HTTPException(409, f"不允许从「{candidate['status']}」变更为「{body.status}」")
    return {"ok": True}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[\d\-+ ]{6,20}$")

@app.post("/api/talent/ingest")
async def talent_ingest(file: UploadFile = File(...), user=Depends(require_hr)):
    # 摄取管线:大小上限(R1b)→ 复用 rag.parse_file(PDF/Word/图片/OCR 零新解析)→ LLM 约束解码抽取
    # (定界符+"数据非指令"注入防线,R1a)→ 存草稿 + 简历原文。抽取只出草稿(尺子②),生效需 HR 走 /confirm。
    raw = await read_capped(file)
    text, needs_review = rag.parse_file(file.filename, raw)
    r = await resume.extract(text, model_tag(DEFAULT_MODEL_ID))
    fields = resume.to_candidate_fields(r)
    cid = talent_store.create_candidate(**fields, source_file=file.filename, actor=user["username"])
    talent_store.save_resume(cid, raw_text=text, parsed_text=r.model_dump_json())
    # extracted/original_text 回给确认页做对照(前端全过 esc,I6);needs_review=OCR 低置信提示
    return {"id": cid, "extracted": fields, "original_text": text, "needs_review": needs_review}

@app.post("/api/talent/{cid}/confirm")
def talent_confirm(cid: str, body: TalentCreate, user=Depends(require_hr)):
    # HR 逐份审定(🔴R2)→ 最终字段落库 + 状态 草稿→待筛。email/手机机械校验=信任边界输入校验。
    candidate = talent_store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "候选人不存在")
    if candidate["status"] != "草稿":
        raise HTTPException(409, "仅草稿候选人可确认")
    if body.email and not _EMAIL_RE.match(body.email):
        raise HTTPException(400, "邮箱格式不合法")
    if body.phone and not _PHONE_RE.match(body.phone):
        raise HTTPException(400, "手机号格式不合法")
    talent_store.confirm_candidate(cid, actor=user["username"], reason="HR 确认原文",
                                   name=body.name, phone=body.phone, email=body.email,
                                   years=body.years, skills=body.skills,
                                   education=body.education, expected_salary=body.expected_salary,
                                   experience=body.experience)
    return {"ok": True}

@app.post("/api/talent/{cid}/jd-gap")
async def talent_jd_gap(cid: str, body: JdGapBody, user=Depends(require_hr)):
    c = talent_store.get_candidate(cid)
    if not c:
        raise HTTPException(404, "候选人不存在")
    jd_text = body.jd or ""
    if body.job_id:  # 双入口:选岗位则取岗位 JD(一次维护、多人复用);否则用临时粘贴的 jd
        job = talent_store.get_job(body.job_id)
        if not job:
            raise HTTPException(404, "岗位不存在")
        if job["status"] != "开放":
            raise HTTPException(409, "关闭岗位不能运行 JD 分析")
        if c["status"] in ("草稿", "归档") or not talent_store.is_candidate_attached(body.job_id, cid):
            raise HTTPException(409, "请先将已确认候选人挂到该开放岗位")
        jd_text = job["jd_text"] or ""
    rz = talent_store.get_resume(cid) or {}
    result = await resume.jd_gap(c, rz.get("raw_text") or "", jd_text, model_tag(DEFAULT_MODEL_ID))
    return result.model_dump()

@app.post("/api/talent/{cid}/review")
async def talent_review(cid: str, user=Depends(require_hr)):
    c = talent_store.get_candidate(cid)
    if not c:
        raise HTTPException(404, "候选人不存在")
    rz = talent_store.get_resume(cid) or {}
    result = await resume.resume_review(c, rz.get("raw_text") or "", model_tag(DEFAULT_MODEL_ID))
    return result.model_dump()

@app.post("/api/talent/{cid}/message-draft")
async def talent_message_draft(cid: str, body: MessageDraftBody, user=Depends(require_hr)):
    if body.kind not in ("约面", "拒信", "跟进"):
        raise HTTPException(400, "话术类型无效")
    c = talent_store.get_candidate(cid)
    if not c:
        raise HTTPException(404, "候选人不存在")
    draft = await resume.message_draft(c, body.kind, model_tag(DEFAULT_MODEL_ID),
                                       job_title=body.job_title or "", interview_time=body.interview_time or "",
                                       location=body.location or "", note=body.note or "")
    name = c.get("name") or ""
    full_body = (f"尊敬的{name}:\n\n" if name else "") + (draft.body or "")   # 称呼机械填充,不由 LLM(I3/F4)
    hits = fairness.scan_protected(full_body)                                # 只扫一次(F-E)
    warnings = ["正文含受保护属性:" + "/".join(hits)] if hits else []
    return {"recipient": {"name": name, "email": c.get("email") or "", "phone": c.get("phone") or ""},
            "kind": body.kind, "subject": draft.subject, "body": full_body, "warnings": warnings}

@app.post("/api/talent/{cid}/interview-questions")
async def talent_interview_questions(cid: str, body: InterviewQuestionsBody, user=Depends(require_hr)):
    c = talent_store.get_candidate(cid)
    if not c:
        raise HTTPException(404, "候选人不存在")
    result = await resume.interview_questions(c, body.jd or "", body.gap_items, model_tag(DEFAULT_MODEL_ID))
    return result.model_dump()

@app.delete("/api/talent/{cid}")
def talent_delete(cid: str, user=Depends(require_hr)):
    if not talent_store.get_candidate(cid):
        raise HTTPException(404, "候选人不存在")
    if not talent_store.delete_draft_candidate(cid, actor=user["username"]):
        raise HTTPException(409, "仅草稿候选人可退回删除，请将已确认候选人归档")
    return {"ok": True}

@app.post("/api/talent/{cid}/archive")
def talent_archive(cid: str, body: TalentArchive, user=Depends(require_hr)):
    candidate = talent_store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "候选人不存在")
    if not talent_store.archive_candidate(cid, user["username"], body.reason):
        raise HTTPException(409, f"不允许从「{candidate['status']}」归档")
    return {"ok": True}


# ---------- 岗位/JD 对象(Step 2.6:多人复用 + 岗位维度证据矩阵地基;require_hr 咽喉,I7)----------
@app.post("/api/jobs")
def job_create(body: JobBody, user=Depends(require_hr)):
    jid = talent_store.create_job(body.title, body.jd_text, resume.clean_jd(body.jd_text),
                                  actor=user["username"])
    return {"id": jid}

@app.get("/api/jobs")
def job_list(user=Depends(require_hr)):
    jobs = talent_store.list_jobs()
    for j in jobs:
        j["flags"] = fairness.scan_protected(j.get("jd_text") or "")  # 岗位段合规警示(单一事实源 fairness,不在前端重复词表)
        j["candidate_ids"] = talent_store.get_job(j["id"])["candidate_ids"]
    return {"jobs": jobs}

@app.get("/api/jobs/{job_id}")
def job_get(job_id: str, user=Depends(require_hr)):
    job = talent_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    return job

@app.patch("/api/jobs/{job_id}")
def job_update(job_id: str, body: JobUpdate, user=Depends(require_hr)):
    if not talent_store.get_job(job_id):
        raise HTTPException(404, "岗位不存在")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "jd_text" in fields:
        fields["jd_clean"] = resume.clean_jd(fields["jd_text"])  # 改 JD 同步重算清洗预览
    talent_store.update_job(job_id, actor=user["username"], **fields)
    return {"ok": True}

@app.delete("/api/jobs/{job_id}")
def job_delete(job_id: str, user=Depends(require_hr)):
    if not talent_store.get_job(job_id):
        raise HTTPException(404, "岗位不存在")
    talent_store.delete_job(job_id, actor=user["username"])
    return {"ok": True}

@app.post("/api/jobs/{job_id}/candidates")
def job_attach(job_id: str, body: JobAttach, user=Depends(require_hr)):
    job = talent_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    if job["status"] != "开放":
        raise HTTPException(409, "关闭岗位不能挂候选人")
    candidate = talent_store.get_candidate(body.candidate_id)
    if not candidate:
        raise HTTPException(404, "候选人不存在")
    if candidate["status"] in ("草稿", "归档"):
        raise HTTPException(409, "仅已确认且未归档候选人可挂岗位")
    talent_store.attach_candidate(job_id, body.candidate_id, actor=user["username"])
    return {"ok": True}

@app.delete("/api/jobs/{job_id}/candidates/{candidate_id}")
def job_detach(job_id: str, candidate_id: str, user=Depends(require_hr)):
    talent_store.detach_candidate(job_id, candidate_id, actor=user["username"])
    return {"ok": True}


# ---------- 岗位评分规则:仅评分配置者可写;冻结版本不可改 ----------
@app.post("/api/jobs/{job_id}/rubrics/draft")
async def rubric_draft(job_id: str, body: Optional[RubricDraftBody] = None, user=Depends(require_rubric_editor)):
    """§12.2 AI 拆解:只产出草稿候选池(8–12 项 + 质量门),不冻结、不落库。"""
    job = talent_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    focus = body.hiring_focus if body else ""
    return await rubric.generate_capability_pool(job.get("jd_text") or "", focus, model_tag(DEFAULT_MODEL_ID))

@app.post("/api/jobs/{job_id}/rubrics")
def rubric_create(job_id: str, body: RubricCreateBody, user=Depends(require_rubric_editor)):
    job = talent_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    jd_fp = hashlib.sha256((job.get("jd_text") or "").encode("utf-8")).hexdigest()
    try:
        rid = talent_store.create_rubric_draft(job_id, body.high_priority_threshold,
                                                [d.model_dump() for d in body.dimensions], user["username"],
                                                jd_fingerprint=jd_fp, hiring_focus=body.hiring_focus)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": rid}

@app.get("/api/jobs/{job_id}/rubrics")
def rubric_list(job_id: str, user=Depends(require_hr)):
    if not talent_store.get_job(job_id):
        raise HTTPException(404, "岗位不存在")
    return {"rubrics": talent_store.list_rubrics(job_id)}

@app.get("/api/rubrics/{rubric_id}")
def rubric_get(rubric_id: str, user=Depends(require_hr)):
    rubric = talent_store.get_rubric(rubric_id)
    if not rubric:
        raise HTTPException(404, "评分规则不存在")
    return rubric

@app.patch("/api/rubrics/{rubric_id}")
def rubric_update(rubric_id: str, body: RubricUpdateBody, user=Depends(require_rubric_editor)):
    try:
        updated = talent_store.update_rubric_draft(
            rubric_id, user["username"], body.high_priority_threshold,
            [d.model_dump() for d in body.dimensions] if body.dimensions is not None else None)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not updated:
        raise HTTPException(404, "评分规则不存在")
    return {"ok": True}

@app.post("/api/rubrics/{rubric_id}/freeze")
def rubric_freeze(rubric_id: str, user=Depends(require_rubric_editor)):
    try:
        frozen = talent_store.freeze_rubric(rubric_id, user["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not frozen:
        raise HTTPException(404, "评分规则不存在")
    return {"ok": True}

@app.post("/api/rubrics/rebalance")
def rubric_rebalance(body: RebalanceBody, user=Depends(require_rubric_editor)):
    """§6.3 重新平衡:确定性预览权重(不调用 LLM,只按重要性×顺序算),HR 确认后才覆盖草稿。"""
    try:
        weights = rubric.initial_weights(body.dimensions)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"weights": weights}

@app.post("/api/rubrics/{rubric_id}/copy")
def rubric_copy(rubric_id: str, user=Depends(require_rubric_editor)):
    """§13 复制为新草稿:冻结版只读,任何调整都必须走新版本。"""
    new_id = talent_store.copy_rubric_as_draft(rubric_id, user["username"])
    if not new_id:
        raise HTTPException(404, "评分规则不存在")
    return {"id": new_id}


# ---------- 候选人岗位评分:冻结规则 + 原文证据 + 代码公式 ----------
def _evaluation_context(job_id: str, candidate_id: str):
    job = talent_store.get_job(job_id)
    candidate = talent_store.get_candidate(candidate_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    if not candidate:
        raise HTTPException(404, "候选人不存在")
    if job["status"] != "开放":
        raise HTTPException(409, "关闭岗位不能评分")
    if candidate["status"] in ("草稿", "归档") or not talent_store.is_candidate_attached(job_id, candidate_id):
        raise HTTPException(409, "请先将已确认候选人挂到该开放岗位")
    frozen = talent_store.latest_frozen_rubric(job_id)
    if not frozen:
        raise HTTPException(409, "请先冻结该岗位的评分规则")
    resume_row = talent_store.get_resume(candidate_id)
    if not resume_row or not resume_row.get("raw_text"):
        raise HTTPException(409, "候选人没有可评分的简历原文")
    return candidate, frozen, resume_row["raw_text"]


def _evaluation_response(candidate_id: str, job_id: str, resume_text: str, frozen_rubric_id: str):
    """给历史评分显式标注是否仍对应当前简历和当前冻结规则。"""
    saved = talent_store.latest_evaluation(candidate_id, job_id)
    if saved:
        current_fingerprint = hashlib.sha256(resume_text.encode("utf-8")).hexdigest()
        if saved["resume_fingerprint"] != current_fingerprint:
            saved["is_current"] = False
            saved["stale_reason"] = "候选人简历已更新"
        elif saved["rubric_id"] != frozen_rubric_id:
            saved["is_current"] = False
            saved["stale_reason"] = "评分规则版本已变更"
        else:
            saved["is_current"] = True
            saved["stale_reason"] = ""
    return saved

@app.post("/api/jobs/{job_id}/candidates/{candidate_id}/evaluate")
async def candidate_evaluate(job_id: str, candidate_id: str, user=Depends(require_hr)):
    candidate, frozen, resume_text = _evaluation_context(job_id, candidate_id)
    model = model_tag(EVAL_MODEL_ID)  # 评分节点级模型旋钮(默认=DEFAULT_MODEL_ID)
    result = await rubric.evaluate_candidate(candidate, resume_text, frozen, model)
    fingerprint = hashlib.sha256(resume_text.encode("utf-8")).hexdigest()
    eid = talent_store.save_evaluation(candidate_id, job_id, frozen["id"], fingerprint, result, model, user["username"])
    saved = _evaluation_response(candidate_id, job_id, resume_text, frozen["id"])
    saved["id"] = eid
    return saved

@app.get("/api/jobs/{job_id}/candidates/{candidate_id}/evaluate")
def candidate_evaluation_get(job_id: str, candidate_id: str, user=Depends(require_hr)):
    _, frozen, resume_text = _evaluation_context(job_id, candidate_id)
    result = _evaluation_response(candidate_id, job_id, resume_text, frozen["id"])
    if not result:
        raise HTTPException(404, "尚无评分结果")
    return result

@app.post("/api/jobs/{job_id}/candidates/{candidate_id}/evaluate/retry")
async def candidate_evaluate_retry(job_id: str, candidate_id: str, user=Depends(require_hr)):
    """§8:只重跑上次评分中 needs_review 的维度,成功维度原样保留,不偷偷重算。"""
    candidate, frozen, resume_text = _evaluation_context(job_id, candidate_id)
    saved = talent_store.latest_evaluation(candidate_id, job_id)
    if not saved:
        raise HTTPException(409, "尚无评分结果,请先运行评分")
    fingerprint = hashlib.sha256(resume_text.encode("utf-8")).hexdigest()
    if saved["resume_fingerprint"] != fingerprint or saved["rubric_id"] != frozen["id"]:
        raise HTTPException(409, "评分已过期(简历或规则已变更),请重新运行评分")
    model = model_tag(EVAL_MODEL_ID)  # 评分节点级模型旋钮(默认=DEFAULT_MODEL_ID)
    result = await rubric.retry_failed_dimensions(candidate, resume_text, frozen, saved["items"], model)
    eid = talent_store.save_evaluation(candidate_id, job_id, frozen["id"], fingerprint, result, model, user["username"])
    out = _evaluation_response(candidate_id, job_id, resume_text, frozen["id"])
    out["id"] = eid
    return out

@app.post("/api/jobs/{job_id}/candidates/{candidate_id}/checkpoints")
def candidate_checkpoint(job_id: str, candidate_id: str, body: CheckpointBody, user=Depends(require_hr)):
    """§7.4:HR 标注面试考核点(verified/concern/not_applicable),只重算队列标签不回写直接契合分。"""
    if body.checkpoint_status not in ("verified", "concern", "not_applicable"):
        raise HTTPException(400, "考核点状态只能是 verified/concern/not_applicable")
    _, frozen, resume_text = _evaluation_context(job_id, candidate_id)
    saved = talent_store.latest_evaluation(candidate_id, job_id)
    if not saved:
        raise HTTPException(409, "尚无评分结果")
    fingerprint = hashlib.sha256(resume_text.encode("utf-8")).hexdigest()
    if saved["resume_fingerprint"] != fingerprint or saved["rubric_id"] != frozen["id"]:
        raise HTTPException(409, "评分已过期,请重新运行评分")
    if not any(str(it["dimension_id"]) == body.dimension_id for it in saved["items"]):
        raise HTTPException(404, "该评分中没有此维度")
    if not talent_store.set_checkpoint(saved["id"], body.dimension_id, body.checkpoint_status, user["username"]):
        raise HTTPException(404, "更新考核点失败")
    reloaded = talent_store.latest_evaluation(candidate_id, job_id)
    new_queue = rubric.recompute_queue(reloaded["items"], reloaded["direct_fit_score"],
                                       reloaded.get("coverage") or 0, frozen["high_priority_threshold"])
    talent_store.update_evaluation_queue(saved["id"], new_queue)
    return _evaluation_response(candidate_id, job_id, resume_text, frozen["id"])


@app.get("/api/jobs/{job_id}/evaluation-queue")
def job_evaluation_queue(job_id: str, user=Depends(require_hr)):
    """岗位工作队列：只读、可解释，不自动改变候选人状态。"""
    job = talent_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    frozen = talent_store.latest_frozen_rubric(job_id)
    rows = []
    for candidate_id in job["candidate_ids"]:
        candidate = talent_store.get_candidate(candidate_id)
        if not candidate:
            continue
        entry = {
            "candidate_id": candidate_id,
            "name": candidate["name"],
            "candidate_status": candidate["status"],
            "evaluation_status": "unassessed",
            "direct_fit_score": None,
            "queue": "未评估",
            "rubric_id": frozen["id"] if frozen else None,
            "is_current": False,
            "stale_reason": "尚未冻结评分规则" if not frozen else "尚未评分",
            "created_at": None,
            "coverage": None,
            "pending_review": 0,
        }
        resume_row = talent_store.get_resume(candidate_id)
        saved = talent_store.latest_evaluation(candidate_id, job_id)
        if saved and resume_row and resume_row.get("raw_text") and frozen:
            current = _evaluation_response(candidate_id, job_id, resume_row["raw_text"], frozen["id"])
            entry.update({
                "evaluation_status": current["status"],
                "direct_fit_score": current["direct_fit_score"],
                "queue": current["queue"] if current["is_current"] else "未评估",
                "rubric_id": current["rubric_id"],
                "is_current": current["is_current"],
                "stale_reason": current["stale_reason"],
                "created_at": current["created_at"],
                "coverage": current.get("coverage"),
                "pending_review": sum(1 for it in current.get("items", [])
                                      if it.get("evidence_status") == "needs_review"
                                      or it.get("checkpoint_status") in ("pending", "concern")),
            })
        elif saved and not resume_row:
            entry["stale_reason"] = "候选人没有当前简历原文"
        rows.append(entry)
    order = {"优先评估": 0, "必须复核": 1, "常规复核": 2, "未评估": 3}
    rows.sort(key=lambda x: (order[x["queue"]], -(x["direct_fit_score"] or -1), x["created_at"] or 0))
    return {"job_id": job_id, "rubric_id": frozen["id"] if frozen else None, "candidates": rows}


# ---------- 群发(Step 2.4;全部 require_hr,I7;发信数据只在 talent.sqlite,I1)----------
class SendBatchBody(BaseModel):
    mail_type: str
    job_id: str
    candidate_ids: List[str]
    interview_time: str = ""
    location: str = ""
    note: str = ""


class ResolveBody(BaseModel):
    action: str
    sentence: str = ""   # 仅 edit_sentence 用;body 无 to_email 字段=结构上无法经 resolve 改收件人(🟡T2-E ①)


@app.post("/api/send-batches")
async def create_send_batch(body: SendBatchBody, user=Depends(require_hr)):
    if body.mail_type not in mailer.STATUS_GATE:
        raise HTTPException(400, "未知邮件类型")
    if len(body.candidate_ids) > 50:
        raise HTTPException(400, "单批最多 50 人")
    job = talent_store.get_job(body.job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    bid = talent_store.create_send_batch(user["username"], body.mail_type, body.job_id,
                                         body.interview_time, body.location, body.note)
    frozen = talent_store.latest_frozen_rubric(body.job_id)   # 无冻结规则→S 恒空→全走 no_material 骨架(已评分专属)
    for cid in body.candidate_ids:
        cand = talent_store.get_candidate(cid)
        if not cand:
            continue
        ev = talent_store.latest_evaluation(cid, body.job_id)
        S = mailer.build_S(ev, frozen)   # Approach 1:S 只含冻结维度名,无候选人字节
        item = mailer.assemble_item(cand, body.mail_type, job["title"],
                                    body.interview_time, body.location, body.note, S,
                                    talent_store.has_prior_send(cid, body.job_id, body.mail_type))
        talent_store.add_send_item(bid, item)
    return talent_store.get_send_batch(bid)


@app.get("/api/send-batches/{bid}")
def get_send_batch(bid: str, user=Depends(require_hr)):
    b = talent_store.get_send_batch(bid)
    if not b:
        raise HTTPException(404, "批次不存在")
    return b


def _send_pending(b, actor, from_status="pending"):
    """确认/重试共用。承重加固:
    🔴 双发竞态:每封做 CAS 原子领取(from_status→'sending'),只有领到的那一方发它——DB 作单一 mutex,
      并发的第二个 confirm/retry 领不到,天然不重发。
    🟡T2-E:发送前重验快照 to_email(machine_check 校验 candidate.email,实发的是快照);非法→failed 不发。
    🟡B:发送前重验候选人当前状态门(防建批后状态漂移,如 约面→淘汰 后仍收到面试邀请);不符→failed 不发。
    🟡T2-A:on_result 的 DB 写 best-effort——发信成功后 DB 若抛不中断批次;该封留 'sending'(已计入 has_prior_send,不重发)。"""
    gate = mailer.STATUS_GATE.get(b["mail_type"], ())
    items, pre_failed = [], 0
    for it in b["items"]:
        if it["status"] != from_status:
            continue
        if not talent_store.claim_send_item(it["id"], from_status, "sending"):
            continue   # 别人已领取/已处理 → 跳过(单一 mutex)
        if not mailer._EMAIL_RE.match(it.get("to_email") or ""):
            talent_store.set_send_item(it["id"], actor, status="failed", fail_reason="收件人邮箱快照非法")
            pre_failed += 1
            continue
        cand = talent_store.get_candidate(it["candidate_id"])
        if not cand or cand.get("status") not in gate:
            talent_store.set_send_item(it["id"], actor, status="failed", fail_reason="发送前状态门不符(候选人状态已变更)")
            pre_failed += 1
            continue
        items.append(it)

    def on_result(it, ok, err):
        try:
            talent_store.set_send_item(it["id"], actor, status="sent" if ok else "failed",
                                       fail_reason=None if ok else err,
                                       sent_at=time.time() if ok else None)
            talent_store.log_audit(actor, "send_item", it["id"], {"ok": ok})
        except Exception:
            pass   # 🟡T2-A:发后落库失败不吞掉批次;该封留 'sending'(has_prior_send 计入,不自动重发)
    sent_n, failed_n = mailer.send_items_smtp(items, on_result)
    failed_n += pre_failed
    talent_store.set_batch_status(b["id"], "完成" if failed_n == 0 else "部分失败", actor)
    return sent_n, failed_n


@app.post("/api/send-batches/{bid}/confirm")
def confirm_send_batch(bid: str, user=Depends(require_hr)):
    b = talent_store.get_send_batch(bid)
    if not b:
        raise HTTPException(404, "批次不存在")
    if b["status"] != "草稿":
        raise HTTPException(409, "仅草稿批可确认")
    # 🔴 CAS 原子领取:只有把批从 草稿 翻成目标态的那一次调用继续,防并发双确认(双击/代理重试)
    dest = "已确认" if not mailer.smtp_configured() else "发送中"
    if not talent_store.claim_batch(bid, "草稿", dest, user["username"], confirmed_by=user["username"]):
        raise HTTPException(409, "批次已被并发处理(非草稿态)")
    talent_store.log_audit(user["username"], "confirm_batch", bid,
                           {"n": len([i for i in b["items"] if i["status"] == "pending"])})
    if dest == "已确认":
        return {"mode": "export", "batch": talent_store.get_send_batch(bid)}
    sent_n, failed_n = _send_pending(b, user["username"])
    return {"mode": "smtp", "sent": sent_n, "failed": failed_n, "batch": talent_store.get_send_batch(bid)}


@app.post("/api/send-batches/{bid}/retry-failed")
def retry_send_batch(bid: str, user=Depends(require_hr)):
    b = talent_store.get_send_batch(bid)
    if not b:
        raise HTTPException(404, "批次不存在")
    if not mailer.smtp_configured():
        raise HTTPException(409, "SMTP 未配置")
    talent_store.log_audit(user["username"], "retry_failed", bid, None)
    sent_n, failed_n = _send_pending(b, user["username"], from_status="failed")
    return {"sent": sent_n, "failed": failed_n, "batch": talent_store.get_send_batch(bid)}


@app.get("/api/send-batches/{bid}/export.csv")
def export_send_batch(bid: str, user=Depends(require_hr)):
    b = talent_store.get_send_batch(bid)
    if not b:
        raise HTTPException(404, "批次不存在")
    rows = ["姓名,邮箱,主题,正文"]
    for i in b["items"]:
        if i["status"] in ("pending", "failed"):
            talent_store.set_send_item(i["id"], user["username"], status="exported", sent_at=time.time())
        if i["status"] in ("pending", "failed", "exported", "sent"):
            cells = [i["to_name"], i["to_email"], i["subject"], i["body"]]
            rows.append(",".join(mailer.csv_field(c) for c in cells))   # F-CSV-1:中和公式注入 + RFC4180 转义
    talent_store.log_audit(user["username"], "export_batch", bid, {"rows": len(rows) - 1})
    if b["status"] in ("草稿", "已确认"):
        talent_store.set_batch_status(bid, "完成", user["username"])
    return Response("﻿" + "\n".join(rows), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=batch-{bid[:8]}.csv"})


@app.post("/api/send-items/{iid}/resolve")
async def resolve_send_item(iid: str, body: ResolveBody, user=Depends(require_hr)):
    row = talent_store._tconn.execute("SELECT * FROM send_items WHERE id=?", (iid,)).fetchone()
    if not row:
        raise HTTPException(404, "条目不存在")
    it = dict(row)
    b = talent_store.get_send_batch(it["batch_id"])
    if body.action == "skip":
        talent_store.set_send_item(iid, user["username"], status="skipped")
        return {"ok": True}
    if b["status"] == "草稿":
        raise HTTPException(400, "请先确认批次,再处置例外(骨架/改句会直接发送)")
    if body.action not in ("send_skeleton", "edit_sentence", "retry"):
        raise HTTPException(400, "未知动作")
    cand = talent_store.get_candidate(it["candidate_id"])
    job = talent_store.get_job(b["job_id"])
    material = {"material_id": None, "material_value": None, "material_source": None}
    if body.action == "retry":
        frozen = talent_store.latest_frozen_rubric(b["job_id"])
        ev = talent_store.latest_evaluation(it["candidate_id"], b["job_id"])
        S = mailer.build_S(ev, frozen)
        new_it = mailer.assemble_item(cand, b["mail_type"], job["title"], b["interview_time"],
                                      b["location"], b["note"], S, False)
        if new_it["status"] == "exception":
            talent_store.set_send_item(iid, user["username"], exception_reason=new_it["exception_reason"])
            return {"ok": False, "reason": new_it["exception_reason"]}
        subject, body_text, wording = new_it["subject"], new_it["body"], new_it["wording"]
        material = {"material_id": new_it["material_id"], "material_value": new_it["material_value"],
                    "material_source": new_it["material_source"]}
    elif body.action == "edit_sentence":
        # Approach 3:HR 手打句子(可信操作者 I2)仍扫 fairness + 决策词(合规网,非注入防御;不再有 check_wording)
        s = body.sentence.strip()
        if not s or fairness.scan_protected(s) or any(w in s for w in mailer._DECISION_WORDS_SAFE):
            raise HTTPException(400, "句子为空或含受保护属性/决策词")
        subject, body_text = mailer.render_mail(b["mail_type"], cand["name"], job["title"],
                                                b["interview_time"], b["location"], b["note"], s)
        wording = "manual"
    else:   # send_skeleton
        subject, body_text = mailer.render_mail(b["mail_type"], cand["name"], job["title"],
                                                b["interview_time"], b["location"], b["note"], "")
        wording = "skeleton"
    chk = mailer.machine_check(cand, b["mail_type"], body_text, None, set(), False)
    if not chk["ok"]:
        raise HTTPException(400, mailer.EXC_REASONS.get(chk["exception"], "机检未过"))
    # 🟡T2-E:resolve 只改 body/wording/status(+ retry 素材元数据),绝不改 to_email
    talent_store.set_send_item(iid, user["username"], subject=subject, body=body_text,
                               wording=wording, status="pending", exception_reason=None, **material)
    if mailer.smtp_configured():
        if not mailer._EMAIL_RE.match(it.get("to_email") or ""):   # 🟡T2-E:发送前重验快照 to_email
            talent_store.set_send_item(iid, user["username"], status="failed", fail_reason="收件人邮箱快照非法")
            return {"ok": False, "reason": "收件人邮箱快照非法"}
        # 🔴 CAS 原子领取该封(pending→sending),防并发 resolve/retry 同发一封
        if not talent_store.claim_send_item(iid, "pending", "sending"):
            return {"ok": False, "reason": "该条目正在处理中"}
        one = [dict(talent_store._tconn.execute("SELECT * FROM send_items WHERE id=?", (iid,)).fetchone())]

        def on_result(x, ok, err):
            try:
                talent_store.set_send_item(x["id"], user["username"], status="sent" if ok else "failed",
                                           fail_reason=None if ok else err, sent_at=time.time() if ok else None)
                talent_store.log_audit(user["username"], "send_item", x["id"], {"ok": ok})
            except Exception:
                pass
        sent_n, _ = mailer.send_items_smtp(one, on_result)
        return {"ok": sent_n == 1}
    talent_store.set_send_item(iid, user["username"], status="exported", sent_at=time.time())
    return {"ok": True, "mode": "export"}


# ---------- 用户管理(管理员) ----------
@app.get("/api/users")
def users(user=Depends(require_admin)):
    return {"users": store.list_users(), "seats": LICENSE_SEATS, "used": store.count_users(),
            "role_names": {k: v["name"] for k, v in ROLES.items()}}

@app.post("/api/users")
def add_user(body: NewUser, user=Depends(require_admin)):
    if store.count_users() >= LICENSE_SEATS:
        raise HTTPException(403, f"已达授权席位上限({LICENSE_SEATS}),请升级授权")
    if not body.username or not body.password:
        raise HTTPException(400, "用户名和密码不能为空")
    if store.get_user(body.username):
        raise HTTPException(400, "用户名已存在")
    if body.role not in ROLES:
        raise HTTPException(400, "角色无效")
    store.create_user(body.username, body.password, body.role)
    return {"ok": True}

@app.delete("/api/users/{username}")
def remove_user(username: str, user=Depends(require_admin)):
    if username == user["username"]:
        raise HTTPException(400, "不能删除当前登录的账号")
    store.delete_user(username)
    return {"ok": True}


# ---------- 数据分析(企业版 + 管理员) ----------
@app.get("/api/analytics")
def analytics(user=Depends(require_admin)):
    if not features().get("analytics"):
        raise HTTPException(403, "数据分析为企业版功能,当前版本未开启")
    return store.qa_stats()


# ---------- 托管前端 ----------
_FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
if os.path.isfile(_FRONTEND):
    @app.get("/")
    def index():
        return FileResponse(_FRONTEND)
