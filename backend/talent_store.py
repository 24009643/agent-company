"""人才库独立物理分库(Step 2.1 立墙)。

I1 物理分库定案:candidates/resumes/audit 存于独立 talent.sqlite,与 kb.sqlite / chroma
不同库、不同连接;检索层(rag.py / store.py)永不 import 本模块——"过滤会有 bug,不在场没有
bug"。v1 不建向量 collection(筛选=结构化 SQL,语义匹配记 v2)。

🟡Y1 公平性结构层:candidates schema 不设性别/年龄/照片/婚育/籍贯字段(第一层排除)。
🟡Y6 通用审计:状态变更/导出/群发共用一张 audit 表,不做三张专表。

自检:python test_talent_store.py
"""
import json, os, sqlite3, time, uuid, threading
import fairness
from config import DATA_DIR

_lock = threading.Lock()
_tconn = None
STATUSES = ("草稿", "待筛", "约面", "已面", "评估", "offer", "淘汰", "归档")
ALLOWED_TRANSITIONS = {
    "草稿": {"待筛"},
    "待筛": {"约面", "淘汰", "归档"},
    "约面": {"已面", "淘汰", "归档"},
    "已面": {"评估", "淘汰", "归档"},
    "评估": {"offer", "淘汰", "归档"},
    "offer": {"归档"},
    "淘汰": {"归档"},
    "归档": set(),
}


def valid_status(status):
    return status in STATUSES


def init(path=None):
    """建库 + 建表。app 启动时无参调用(→ data/talent.sqlite,与 kb.sqlite 平级);测试传临时路径。"""
    global _tconn
    path = path or os.path.join(DATA_DIR, "talent.sqlite")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _tconn = sqlite3.connect(path, check_same_thread=False)
    _tconn.row_factory = sqlite3.Row
    _tconn.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidates(
            id TEXT PRIMARY KEY, name TEXT, phone TEXT, email TEXT,
            years REAL, skills TEXT, education TEXT, expected_salary TEXT, experience TEXT,
            status TEXT DEFAULT '草稿', source_file TEXT, created_at REAL);
        CREATE TABLE IF NOT EXISTS resumes(
            id TEXT PRIMARY KEY, candidate_id TEXT, raw_text TEXT, parsed_text TEXT);
        CREATE TABLE IF NOT EXISTS audit(
            id TEXT PRIMARY KEY, actor TEXT, action TEXT, target TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, title TEXT, jd_text TEXT, jd_clean TEXT,
            status TEXT DEFAULT '开放', created_at REAL);
        CREATE TABLE IF NOT EXISTS job_candidate(
            job_id TEXT, candidate_id TEXT, PRIMARY KEY(job_id, candidate_id));
        CREATE TABLE IF NOT EXISTS job_rubrics(
            id TEXT PRIMARY KEY, job_id TEXT, version INTEGER, status TEXT,
            high_priority_threshold INTEGER, created_by TEXT, created_at REAL,
            frozen_by TEXT, frozen_at REAL, UNIQUE(job_id, version));
        CREATE TABLE IF NOT EXISTS rubric_dimensions(
            id TEXT PRIMARY KEY, rubric_id TEXT, position INTEGER, name TEXT,
            jd_requirement TEXT, description TEXT, kind TEXT, weight INTEGER,
            evidence_rule TEXT, protected INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS candidate_evaluations(
            id TEXT PRIMARY KEY, candidate_id TEXT, job_id TEXT, rubric_id TEXT,
            resume_fingerprint TEXT, status TEXT, direct_fit_score INTEGER,
            queue TEXT, model TEXT, created_at REAL);
        CREATE TABLE IF NOT EXISTS evaluation_dimensions(
            id TEXT PRIMARY KEY, evaluation_id TEXT, dimension_id TEXT,
            evidence_status TEXT, evidence_level INTEGER, note TEXT,
            evidence_json TEXT, transferable INTEGER);
        CREATE TABLE IF NOT EXISTS send_batches(
            id TEXT PRIMARY KEY, mail_type TEXT, job_id TEXT,
            interview_time TEXT, location TEXT, note TEXT,
            status TEXT, created_by TEXT, confirmed_by TEXT,
            created_at REAL, confirmed_at REAL);
        CREATE TABLE IF NOT EXISTS send_items(
            id TEXT PRIMARY KEY, batch_id TEXT, candidate_id TEXT,
            to_name TEXT, to_email TEXT, subject TEXT, body TEXT,
            material_id TEXT, material_value TEXT, material_source TEXT,
            wording TEXT, template_version TEXT, machine_checks TEXT,
            status TEXT, fail_reason TEXT, exception_reason TEXT, sent_at REAL);
        """
    )
    audit_cols = {r[1] for r in _tconn.execute("PRAGMA table_info(audit)").fetchall()}
    if "payload" not in audit_cols:
        _tconn.execute("ALTER TABLE audit ADD COLUMN payload TEXT")
    candidate_cols = {r[1] for r in _tconn.execute("PRAGMA table_info(candidates)").fetchall()}
    if "experience" not in candidate_cols:
        _tconn.execute("ALTER TABLE candidates ADD COLUMN experience TEXT")
    # Step 1 §12.1:v2 评分字段追加(加列式迁移,旧库自动补列,历史数据只读不改)。
    def _add_cols(table, cols):
        have = {r[1] for r in _tconn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, decl in cols:
            if col not in have:
                _tconn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    _add_cols("rubric_dimensions", [("group_name", "TEXT"), ("source_type", "TEXT"), ("source_quote", "TEXT"),
                                    ("importance_reason", "TEXT"), ("importance_level", "TEXT"), ("rank", "INTEGER"),
                                    ("assessment_mode", "TEXT"), ("interview_probe", "TEXT"), ("confidence", "TEXT")])
    _add_cols("job_rubrics", [("jd_fingerprint", "TEXT"), ("hiring_focus", "TEXT")])
    _add_cols("candidate_evaluations", [("coverage", "INTEGER")])
    _add_cols("evaluation_dimensions", [("evidence_coefficient", "REAL"), ("importance_level", "TEXT"),
                                        ("assessment_mode", "TEXT"), ("interview_checkpoint_pending", "INTEGER"),
                                        ("failure_code", "TEXT"), ("checkpoint_status", "TEXT"), ("attempt_count", "INTEGER")])
    _tconn.commit()


def log_audit(actor, action, target, payload=None):
    """🟡Y6 通用审计:一切副作用(状态变更/导出/群发)都落这里一行。"""
    with _lock:
        _tconn.execute("INSERT INTO audit(id, actor, action, target, payload, ts) VALUES (?,?,?,?,?,?)",
                       (uuid.uuid4().hex, actor, action, target,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else None, time.time()))
        _tconn.commit()


def create_candidate(name, actor, phone=None, email=None, years=None, skills=None,
                     education=None, expected_salary=None, experience=None, source_file=None):
    """建草稿态候选人(状态'草稿';2.2 摄取经 HR 确认后才流转)。actor 必填 → 落审计。返回 id。"""
    cid = uuid.uuid4().hex
    with _lock:
        _tconn.execute(
            """INSERT INTO candidates(id, name, phone, email, years, skills,
               education, expected_salary, experience, status, source_file, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, name, phone, email, years, skills, education, expected_salary, experience,
             "草稿", source_file, time.time()))
        _tconn.commit()
    log_audit(actor, "create", cid)  # 锁外调用:threading.Lock 非重入
    return cid


def get_candidate(cid):
    r = _tconn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def list_candidates(status=None, min_years=None, skill=None, education=None):
    sql = "SELECT * FROM candidates"
    clauses, args = [], []
    if status:
        clauses.append("status=?")
        args.append(status)
    if min_years is not None:
        clauses.append("years>=?")
        args.append(min_years)
    if skill:
        clauses.append("skills LIKE ?")
        args.append(f"%{skill}%")
    if education:
        clauses.append("education LIKE ?")
        args.append(f"%{education}%")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in _tconn.execute(sql, args).fetchall()]


def list_candidate_summaries(status=None, min_years=None, skill=None, education=None, limit=30, offset=0):
    """人才库列表只给工作台必要摘要；联系方式和薪资仅在详情端点返回。"""
    sql = " FROM candidates"
    clauses, args = [], []
    if status:
        clauses.append("status=?"); args.append(status)
    if min_years is not None:
        clauses.append("years>=?"); args.append(min_years)
    if skill:
        clauses.append("skills LIKE ?"); args.append(f"%{skill}%")
    if education:
        clauses.append("education LIKE ?"); args.append(f"%{education}%")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    total = _tconn.execute("SELECT COUNT(*)" + sql, args).fetchone()[0]
    rows = _tconn.execute(
        "SELECT id,name,years,skills,education,status,source_file,created_at" + sql +
        " ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    return {"candidates": [dict(r) for r in rows], "total": total}


def transition_candidate(cid, status, actor, reason):
    """唯一状态迁移入口:验证允许边,写前后状态与人工理由。"""
    candidate = get_candidate(cid)
    if not candidate or status not in ALLOWED_TRANSITIONS.get(candidate["status"], set()):
        return False
    with _lock:
        _tconn.execute("UPDATE candidates SET status=? WHERE id=?", (status, cid))
        _tconn.commit()
    log_audit(actor, "transition_status", cid,
              {"from": candidate["status"], "to": status, "reason": reason})
    return True


def confirm_candidate(cid, actor, reason, **fields):
    """确认页唯一入口:只允许草稿变待筛,同时写 HR 已审定字段。"""
    candidate = get_candidate(cid)
    if not candidate or candidate["status"] != "草稿":
        return False
    cols = [k for k in _EDITABLE if k in fields]
    sets = [f"{k}=?" for k in cols] + ["status=?"]
    vals = [fields[k] for k in cols] + ["待筛", cid]
    with _lock:
        _tconn.execute(f"UPDATE candidates SET {', '.join(sets)} WHERE id=?", vals)
        _tconn.commit()
    log_audit(actor, "confirm_candidate", cid,
              {"from": "草稿", "to": "待筛", "reason": reason, "fields": cols})
    return True


def archive_candidate(cid, actor, reason):
    return transition_candidate(cid, "归档", actor, reason)


def delete_draft_candidate(cid, actor):
    """退回只删除草稿;删除候选人、简历和岗位关联必须原子完成。"""
    candidate = get_candidate(cid)
    if not candidate or candidate["status"] != "草稿":
        return False
    with _lock:
        _tconn.execute("DELETE FROM job_candidate WHERE candidate_id=?", (cid,))
        _tconn.execute("DELETE FROM evaluation_dimensions WHERE evaluation_id IN (SELECT id FROM candidate_evaluations WHERE candidate_id=?)", (cid,))
        _tconn.execute("DELETE FROM candidate_evaluations WHERE candidate_id=?", (cid,))
        _tconn.execute("DELETE FROM resumes WHERE candidate_id=?", (cid,))
        _tconn.execute("DELETE FROM candidates WHERE id=?", (cid,))
        _tconn.commit()
    log_audit(actor, "delete_draft", cid, {"status": "草稿"})
    return True

def save_resume(candidate_id, raw_text, parsed_text=None):
    """存简历原文 + 结构化解析文本(2.2 摄取)。"""
    with _lock:
        _tconn.execute("INSERT INTO resumes(id, candidate_id, raw_text, parsed_text) VALUES (?,?,?,?)",
                       (uuid.uuid4().hex, candidate_id, raw_text, parsed_text))
        _tconn.commit()


def get_resume(candidate_id):
    """取该候选人最近一份简历(原文/解析),供确认页对照。"""
    r = _tconn.execute(
        "SELECT raw_text, parsed_text FROM resumes WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1",
        (candidate_id,)).fetchone()
    return dict(r) if r else None


_EDITABLE = ("name", "phone", "email", "years", "skills", "education", "expected_salary", "experience")

def update_candidate(cid, actor, **fields):
    """字段编辑不承担状态迁移;状态只能经 transition_candidate/confirm_candidate。"""
    cols = [k for k in _EDITABLE if k in fields]
    sets = [f"{k}=?" for k in cols]
    vals = [fields[k] for k in cols]
    if sets:
        with _lock:
            _tconn.execute(f"UPDATE candidates SET {', '.join(sets)} WHERE id=?", (*vals, cid))
            _tconn.commit()
    log_audit(actor, "update_candidate", cid, {"fields": cols})


# ---------- 岗位/JD 对象(Step 2.6:多人复用 + 岗位维度证据矩阵地基)----------
_JOB_EDITABLE = ("title", "jd_text", "jd_clean", "status")

def create_job(title, jd_text, jd_clean, actor):
    """建岗位。jd_clean 由调用方(端点)用 resume.clean_jd 算好传入,store 只存不算。落审计。"""
    jid = uuid.uuid4().hex
    with _lock:
        _tconn.execute("INSERT INTO jobs(id, title, jd_text, jd_clean, status, created_at) VALUES (?,?,?,?,?,?)",
                       (jid, title, jd_text, jd_clean, "开放", time.time()))
        _tconn.commit()
    log_audit(actor, "create_job", jid)  # 锁外调用:threading.Lock 非重入
    return jid


def list_jobs():
    return [dict(r) for r in _tconn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()]


def get_job(job_id):
    r = _tconn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        return None
    job = dict(r)
    job["candidate_ids"] = [x["candidate_id"] for x in
        _tconn.execute("SELECT candidate_id FROM job_candidate WHERE job_id=?", (job_id,)).fetchall()]
    return job


def is_candidate_attached(job_id, candidate_id):
    return _tconn.execute(
        "SELECT 1 FROM job_candidate WHERE job_id=? AND candidate_id=?", (job_id, candidate_id)
    ).fetchone() is not None


def update_job(job_id, actor, **fields):
    """更新白名单字段(title/jd_text/jd_clean/status)。列名走白名单(值全参数化),防 SQL 注入。"""
    cols = [k for k in _JOB_EDITABLE if k in fields]
    sets = [f"{k}=?" for k in cols]
    vals = [fields[k] for k in cols]
    if sets:
        with _lock:
            _tconn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*vals, job_id))
            _tconn.commit()
    log_audit(actor, "update_job", job_id)  # 锁外调用:threading.Lock 非重入


def delete_job(job_id, actor):
    """删岗位 + 其挂载关系(不动候选人本身)。落审计。"""
    with _lock:
        _tconn.execute("DELETE FROM job_candidate WHERE job_id=?", (job_id,))
        _tconn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        _tconn.commit()
    log_audit(actor, "delete_job", job_id)  # 锁外调用:threading.Lock 非重入


def attach_candidate(job_id, candidate_id, actor):
    """挂载候选人到岗位(多对多)。INSERT OR IGNORE:重复挂静默幂等。落审计。"""
    with _lock:
        _tconn.execute("INSERT OR IGNORE INTO job_candidate(job_id, candidate_id) VALUES (?,?)",
                       (job_id, candidate_id))
        _tconn.commit()
    log_audit(actor, "attach_candidate", f"{job_id}:{candidate_id}")


def detach_candidate(job_id, candidate_id, actor):
    with _lock:
        _tconn.execute("DELETE FROM job_candidate WHERE job_id=? AND candidate_id=?", (job_id, candidate_id))
        _tconn.commit()
    log_audit(actor, "detach_candidate", f"{job_id}:{candidate_id}")


# ---------- 岗位评分规则:草稿可调,冻结后不可改 ----------
_IMPORTANCE_LEVELS = ("priority", "important", "bonus")
_ASSESSMENT_MODES = ("resume_evidence", "self_reported")


def _dim_importance(d):
    return d.get("importance_level") or _KIND_TO_IMP.get(d.get("kind"), "important")


def _dim_mode(d):
    return d.get("assessment_mode") or "resume_evidence"


def _dim_cap(d):
    """单维权重上限:附加或仅自述维度 10 分,普通维度 40 分(§6.3)。"""
    return 10 if _dim_importance(d) == "bonus" or _dim_mode(d) == "self_reported" else 40


def _validate_draft_dimensions(dimensions):
    """§12.2 保存草稿=字段级校验:字段合法即可,不要求维度数/合计,方便 HR 逐步搭建。"""
    if not dimensions:
        raise ValueError("评分规则至少需要 1 个维度")
    for d in dimensions:
        if not (d.get("name") or "").strip() or not (d.get("jd_requirement") or "").strip():
            raise ValueError("每个维度必须包含名称和 JD 要求")
        if _dim_importance(d) not in _IMPORTANCE_LEVELS:
            raise ValueError("维度重要性必须为 priority / important / bonus")
        if _dim_mode(d) not in _ASSESSMENT_MODES:
            raise ValueError("维度评估方式必须为 resume_evidence / self_reported")
        w = d.get("weight")
        if not isinstance(w, int) or w < 0:
            raise ValueError("维度权重必须为非负整数")


def _validate_frozen_dimensions(dimensions):
    """§6.3 冻结硬校验:维度数/优先数/5点步进/单项上下限/附加合计/合计100/证据规则/无受保护。"""
    _validate_draft_dimensions(dimensions)
    if not (5 <= len(dimensions) <= 8):
        raise ValueError("冻结规则必须为 5–8 个维度:过少会失真,过多会稀释权重并抬高证据失败率")
    priorities = sum(1 for d in dimensions if _dim_importance(d) == "priority")
    if not (1 <= priorities <= 3):
        raise ValueError("必须有 1–3 个「优先」维度")
    for d in dimensions:
        w = d["weight"]
        if w % 5 != 0:
            raise ValueError("维度权重必须为 5 的整数倍(5 点步进)")
        if not (5 <= w <= 40):
            raise ValueError("每个维度权重必须在 5–40 分之间")
        if w > _dim_cap(d):
            raise ValueError("附加维度与仅自述维度权重不得超过 10 分")
        if not (d.get("evidence_rule") or "").strip():
            raise ValueError("冻结前每个维度必须有证据规则")
        exposed = " ".join(str(d.get(k) or "") for k in ("name", "jd_requirement", "evidence_rule", "importance_reason"))
        if fairness.scan_protected(exposed):
            raise ValueError("受保护属性(年龄/性别/婚育/籍贯/民族/照片/学校档次)不得成为评分维度")
    if sum(d["weight"] for d in dimensions if _dim_importance(d) == "bonus") > 20:
        raise ValueError("附加维度权重合计不得超过 20 分")
    if sum(d["weight"] for d in dimensions) != 100:
        raise ValueError("评分维度权重合计必须为 100")


def _rubric_dimensions(rubric_id):
    return [dict(r) for r in _tconn.execute(
        "SELECT * FROM rubric_dimensions WHERE rubric_id=? ORDER BY position", (rubric_id,)).fetchall()]


_IMP_TO_KIND = {"priority": "核心", "important": "重要", "bonus": "加分"}
_KIND_TO_IMP = {"核心": "priority", "重要": "important", "加分": "bonus"}
_DIM_COLS = ("id,rubric_id,position,name,jd_requirement,description,kind,weight,evidence_rule,"
             "group_name,source_type,source_quote,importance_reason,importance_level,rank,"
             "assessment_mode,interview_probe,confidence")


def _dimension_row(rubric_id, position, d):
    """把维度 dict 组装成 rubric_dimensions 全列元组;v1(kind)与 v2(双轴)互相回填,旧读者不断。"""
    importance = d.get("importance_level") or _KIND_TO_IMP.get(d.get("kind"), "important")
    kind = d.get("kind") or _IMP_TO_KIND.get(importance, "核心")
    rank = d.get("rank")
    return (uuid.uuid4().hex, rubric_id, position, d["name"].strip(), d["jd_requirement"].strip(),
            d.get("description") or "", kind, d["weight"], d.get("evidence_rule") or "",
            d.get("group_name") or "", d.get("source_type") or "", d.get("source_quote") or "",
            d.get("importance_reason") or d.get("description") or "", importance,
            position if rank is None else rank, d.get("assessment_mode") or "resume_evidence",
            d.get("interview_probe") or "", d.get("confidence") or "")


def create_rubric_draft(job_id, threshold, dimensions, actor, jd_fingerprint="", hiring_focus=""):
    _validate_draft_dimensions(dimensions)
    version = _tconn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM job_rubrics WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    rid = uuid.uuid4().hex
    with _lock:
        _tconn.execute(
            "INSERT INTO job_rubrics(id,job_id,version,status,high_priority_threshold,jd_fingerprint,hiring_focus,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, job_id, version, "草稿", threshold, jd_fingerprint or "", hiring_focus or "", actor, time.time()))
        _tconn.executemany(
            f"INSERT INTO rubric_dimensions({_DIM_COLS}) VALUES ({','.join('?' * 18)})",
            [_dimension_row(rid, i, d) for i, d in enumerate(dimensions)])
        _tconn.commit()
    log_audit(actor, "create_rubric", rid,
              {"job_id": job_id, "version": version, "dimension_count": len(dimensions), "threshold": threshold})
    return rid


def get_rubric(rubric_id):
    row = _tconn.execute("SELECT * FROM job_rubrics WHERE id=?", (rubric_id,)).fetchone()
    if not row:
        return None
    rubric = dict(row)
    rubric["dimensions"] = _rubric_dimensions(rubric_id)
    return rubric


def list_rubrics(job_id):
    rows = _tconn.execute("SELECT * FROM job_rubrics WHERE job_id=? ORDER BY version DESC", (job_id,)).fetchall()
    return [get_rubric(r["id"]) for r in rows]


def update_rubric_draft(rubric_id, actor, threshold=None, dimensions=None):
    rubric = get_rubric(rubric_id)
    if not rubric:
        return False
    if rubric["status"] != "草稿":
        raise ValueError("冻结版本不可修改，请复制为新草稿")
    next_threshold = rubric["high_priority_threshold"] if threshold is None else threshold
    next_dimensions = rubric["dimensions"] if dimensions is None else dimensions
    _validate_draft_dimensions(next_dimensions)
    with _lock:
        _tconn.execute("UPDATE job_rubrics SET high_priority_threshold=? WHERE id=?", (next_threshold, rubric_id))
        if dimensions is not None:
            _tconn.execute("DELETE FROM rubric_dimensions WHERE rubric_id=?", (rubric_id,))
            _tconn.executemany(
                f"INSERT INTO rubric_dimensions({_DIM_COLS}) VALUES ({','.join('?' * 18)})",
                [_dimension_row(rubric_id, i, d) for i, d in enumerate(dimensions)])
        _tconn.commit()
    log_audit(actor, "update_rubric", rubric_id,
              {"threshold": next_threshold, "dimension_count": len(next_dimensions)})
    return True


def freeze_rubric(rubric_id, actor):
    rubric = get_rubric(rubric_id)
    if not rubric:
        return False
    if rubric["status"] != "草稿":
        raise ValueError("评分规则已冻结")
    _validate_frozen_dimensions(rubric["dimensions"])
    with _lock:
        _tconn.execute("UPDATE job_rubrics SET status=?, frozen_by=?, frozen_at=? WHERE id=?",
                       ("已冻结", actor, time.time(), rubric_id))
        _tconn.commit()
    log_audit(actor, "freeze_rubric", rubric_id,
              {"job_id": rubric["job_id"], "version": rubric["version"]})
    return True


_COPY_KEYS = ("name", "jd_requirement", "description", "kind", "weight", "evidence_rule", "group_name",
              "source_type", "source_quote", "importance_reason", "importance_level", "rank",
              "assessment_mode", "interview_probe", "confidence")


def copy_rubric_as_draft(rubric_id, actor):
    """§13:把任一版本(尤其 v1 冻结版)复制为新 v2 草稿——迁移字段,HR 再补确认与冻结,原版只读不动。"""
    src = get_rubric(rubric_id)
    if not src:
        return None
    dims = [{k: d.get(k) for k in _COPY_KEYS} for d in src["dimensions"]]
    return create_rubric_draft(src["job_id"], src["high_priority_threshold"], dims, actor,
                               jd_fingerprint=src.get("jd_fingerprint") or "", hiring_focus=src.get("hiring_focus") or "")


def latest_frozen_rubric(job_id):
    row = _tconn.execute(
        "SELECT id FROM job_rubrics WHERE job_id=? AND status='已冻结' ORDER BY version DESC LIMIT 1", (job_id,)
    ).fetchone()
    return get_rubric(row["id"]) if row else None


def save_evaluation(candidate_id, job_id, rubric_id, resume_fingerprint, result, model, actor):
    eid = uuid.uuid4().hex
    with _lock:
        _tconn.execute(
            """INSERT INTO candidate_evaluations(id,candidate_id,job_id,rubric_id,resume_fingerprint,status,direct_fit_score,queue,coverage,model,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, candidate_id, job_id, rubric_id, resume_fingerprint, result["status"],
             result["direct_fit_score"], result["queue"], result.get("coverage"), model, time.time()))
        _tconn.executemany(
            """INSERT INTO evaluation_dimensions(id,evaluation_id,dimension_id,evidence_status,evidence_level,note,evidence_json,transferable,
                   evidence_coefficient,importance_level,assessment_mode,interview_checkpoint_pending,failure_code,checkpoint_status,attempt_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(uuid.uuid4().hex, eid, item["dimension_id"], item["evidence_status"], item["evidence_level"],
              item["note"], json.dumps(item["evidence"], ensure_ascii=False), 1 if item["transferable"] else 0,
              item.get("evidence_coefficient"), item.get("importance_level"), item.get("assessment_mode"),
              1 if item.get("interview_checkpoint_pending") else 0,
              item.get("failure_code") or ("needs_review" if item["evidence_status"] == "needs_review" else None),
              item.get("checkpoint_status") or ("pending" if item.get("interview_checkpoint_pending") else "not_applicable"),
              item.get("attempt_count") or 1)
             for item in result["items"]])
        _tconn.commit()
    log_audit(actor, "evaluate_candidate", eid,
              {"candidate_id": candidate_id, "job_id": job_id, "rubric_id": rubric_id,
               "status": result["status"], "score": result["direct_fit_score"], "queue": result["queue"]})
    return eid


def latest_evaluation(candidate_id, job_id):
    row = _tconn.execute(
        "SELECT * FROM candidate_evaluations WHERE candidate_id=? AND job_id=? ORDER BY created_at DESC LIMIT 1",
        (candidate_id, job_id)).fetchone()
    if not row:
        return None
    result = dict(row)
    dim_meta = {r["id"]: r for r in _tconn.execute(
        "SELECT id,name,weight FROM rubric_dimensions WHERE rubric_id=?", (result["rubric_id"],)).fetchall()}
    result["items"] = []
    for item in _tconn.execute("SELECT * FROM evaluation_dimensions WHERE evaluation_id=?", (result["id"],)).fetchall():
        data = dict(item)
        data["evidence"] = json.loads(data.pop("evidence_json") or "[]")
        data["transferable"] = bool(data["transferable"])
        meta = dim_meta.get(data["dimension_id"])
        data["name"] = meta["name"] if meta else "已删除维度"
        data["weight"] = meta["weight"] if meta else 0
        result["items"].append(data)
    return result


_CHECKPOINT_STATUSES = ("pending", "verified", "concern", "not_applicable")


def set_checkpoint(evaluation_id, dimension_id, checkpoint_status, actor):
    """§7.4:标注某维度面试考核点(verified/concern/not_applicable),只改标注不动分数。"""
    if checkpoint_status not in _CHECKPOINT_STATUSES:
        raise ValueError("考核点状态非法")
    with _lock:
        cur = _tconn.execute("UPDATE evaluation_dimensions SET checkpoint_status=? WHERE evaluation_id=? AND dimension_id=?",
                             (checkpoint_status, evaluation_id, dimension_id))
        _tconn.commit()
    if cur.rowcount == 0:
        return False
    log_audit(actor, "set_checkpoint", evaluation_id, {"dimension_id": dimension_id, "checkpoint_status": checkpoint_status})
    return True


def update_evaluation_queue(evaluation_id, queue):
    """考核点标注后重算的队列标签落库(不改直接契合分/覆盖率)。"""
    with _lock:
        _tconn.execute("UPDATE candidate_evaluations SET queue=? WHERE id=?", (queue, evaluation_id))
        _tconn.commit()


# ===== Step 2.4 群发(spec 2026-07-15-bulk-mail)=====
_SEND_ITEM_COLS = {"candidate_id","to_name","to_email","subject","body","material_id",
                   "material_value","material_source","wording","template_version",
                   "machine_checks","status","fail_reason","exception_reason","sent_at"}

def create_send_batch(actor, mail_type, job_id, interview_time="", location="", note=""):
    bid = uuid.uuid4().hex
    with _lock:
        _tconn.execute("INSERT INTO send_batches(id,mail_type,job_id,interview_time,location,note,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (bid, mail_type, job_id, interview_time, location, note, "草稿", actor, time.time()))
        _tconn.commit()
    log_audit(actor, "create_batch", bid, {"mail_type": mail_type, "job_id": job_id})
    return bid

def add_send_item(batch_id, item):
    bad = set(item) - _SEND_ITEM_COLS
    if bad:
        raise ValueError(f"未知列 {bad}")
    iid = uuid.uuid4().hex
    cols = ["id", "batch_id"] + list(item)
    with _lock:
        _tconn.execute(f"INSERT INTO send_items({','.join(cols)}) VALUES({','.join('?'*len(cols))})",
                       [iid, batch_id] + list(item.values()))
        _tconn.commit()
    return iid

def get_send_batch(batch_id):
    row = _tconn.execute("SELECT * FROM send_batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        return None
    b = dict(row)
    b["items"] = [dict(r) for r in _tconn.execute(
        "SELECT * FROM send_items WHERE batch_id=? ORDER BY rowid", (batch_id,))]
    return b

def set_send_item(item_id, actor, **cols):
    bad = set(cols) - _SEND_ITEM_COLS
    if bad:
        raise ValueError(f"未知列 {bad}")
    sets = ",".join(f"{c}=?" for c in cols)
    with _lock:
        _tconn.execute(f"UPDATE send_items SET {sets} WHERE id=?", list(cols.values()) + [item_id])
        _tconn.commit()
    log_audit(actor, "update_send_item", item_id, {k: cols[k] for k in ("status","wording") if k in cols})

def set_batch_status(batch_id, status, actor, confirmed_by=None):
    """房规:store 层统一落审计(actor 必填,端点想漏都漏不掉)——批状态含「确认发送」这个
    真邮件扳机,是本功能最敏感的写操作,不能只靠端点记得审计。"""
    with _lock:
        if confirmed_by:
            _tconn.execute("UPDATE send_batches SET status=?, confirmed_by=?, confirmed_at=? WHERE id=?",
                           (status, confirmed_by, time.time(), batch_id))
        else:
            _tconn.execute("UPDATE send_batches SET status=? WHERE id=?", (status, batch_id))
        _tconn.commit()
    log_audit(actor, "set_batch_status", batch_id, {"status": status})

def claim_batch(batch_id, expect_status, new_status, actor, confirmed_by=None):
    """CAS 批状态:仅当当前=expect_status 时原子转 new_status,返回本调用是否赢得转移。
    防并发双确认(🔴 confirm 双发竞态):read-check-write 本不原子,用 UPDATE...WHERE status=expect 做单一 mutex。"""
    with _lock:
        if confirmed_by:
            cur = _tconn.execute("UPDATE send_batches SET status=?, confirmed_by=?, confirmed_at=? WHERE id=? AND status=?",
                                 (new_status, confirmed_by, time.time(), batch_id, expect_status))
        else:
            cur = _tconn.execute("UPDATE send_batches SET status=? WHERE id=? AND status=?",
                                 (new_status, batch_id, expect_status))
        _tconn.commit()
        won = cur.rowcount == 1
    if won:
        log_audit(actor, "set_batch_status", batch_id, {"status": new_status, "from": expect_status})
    return won

def claim_send_item(item_id, expect_status, new_status):
    """CAS 封状态:仅当当前=expect_status 时原子转 new_status,返回是否赢得(原子领取,防两方同发一封,🔴)。"""
    with _lock:
        cur = _tconn.execute("UPDATE send_items SET status=? WHERE id=? AND status=?",
                             (new_status, item_id, expect_status))
        _tconn.commit()
        return cur.rowcount == 1

def has_prior_send(candidate_id, job_id, mail_type):
    # 计入 'sending'(已领取待发 / 已发但 sent 未落库,🟡A):防"已递未记账"经新建批被重发
    r = _tconn.execute("""SELECT 1 FROM send_items i JOIN send_batches b ON b.id=i.batch_id
        WHERE i.candidate_id=? AND b.job_id=? AND b.mail_type=? AND i.status IN ('sent','exported','sending') LIMIT 1""",
        (candidate_id, job_id, mail_type)).fetchone()
    return r is not None
