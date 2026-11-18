import os, re, sqlite3, time, uuid, threading, hashlib, binascii, datetime
from collections import OrderedDict
import chromadb
from config import DATA_DIR

_lock = threading.Lock()
_conn = None
_col = None


# ---------- 密码哈希(标准库,无需额外依赖) ----------
def _hash(password, salt=None):
    salt = salt or binascii.hexlify(os.urandom(16)).decode()
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return salt, binascii.hexlify(h).decode()


def init():
    global _conn, _col
    os.makedirs(DATA_DIR, exist_ok=True)
    _conn = sqlite3.connect(os.path.join(DATA_DIR, "kb.sqlite"), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents(
            id TEXT PRIMARY KEY, title TEXT, dept TEXT, n_chunks INTEGER, created_at REAL);
        CREATE TABLE IF NOT EXISTS chunks(
            id TEXT PRIMARY KEY, doc_id TEXT, idx INTEGER, text TEXT, doc_title TEXT, dept TEXT);
        CREATE TABLE IF NOT EXISTS qa_logs(
            id TEXT PRIMARY KEY, question TEXT, answer TEXT, role TEXT,
            rating INTEGER, refused INTEGER DEFAULT 0, created_at REAL);
        CREATE TABLE IF NOT EXISTS users(
            username TEXT PRIMARY KEY, pwd_hash TEXT, salt TEXT, role TEXT, created_at REAL);
        """
    )
    # 迁移:老库可能缺列(qa_logs: refused/会话链/用户名/改写句;documents: 状态)
    cols = [r[1] for r in _conn.execute("PRAGMA table_info(qa_logs)").fetchall()]
    for col, ddl in [("refused", "refused INTEGER DEFAULT 0"),
                     ("conversation_id", "conversation_id TEXT"),
                     ("username", "username TEXT"),
                     ("rewritten", "rewritten TEXT"),
                     ("answer_mode", "answer_mode TEXT"),
                     ("tool_trace", "tool_trace TEXT")]:
        if col not in cols:
            try:
                _conn.execute(f"ALTER TABLE qa_logs ADD COLUMN {ddl}")
            except Exception:
                pass
    dcols = [r[1] for r in _conn.execute("PRAGMA table_info(documents)").fetchall()]
    if "status" not in dcols:
        try:
            _conn.execute("ALTER TABLE documents ADD COLUMN status TEXT DEFAULT '已生效'")
        except Exception:
            pass
    _conn.commit()
    client = chromadb.PersistentClient(path=os.path.join(DATA_DIR, "chroma"))
    _col = client.get_or_create_collection("chunks", metadata={"hnsw:space": "cosine"})
    reconcile()


# ---------- 用户 ----------
def create_user(username, password, role):
    salt, h = _hash(password)
    with _lock:
        _conn.execute("INSERT INTO users VALUES(?,?,?,?,?)", (username, h, salt, role, time.time()))
        _conn.commit()

def verify_login(username, password):
    with _lock:
        row = _conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return None
    _, h = _hash(password, row["salt"])
    if h != row["pwd_hash"]:
        return None
    return {"username": row["username"], "role": row["role"]}

def get_user(username):
    with _lock:
        row = _conn.execute("SELECT username, role FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None

def list_users():
    with _lock:
        rows = _conn.execute("SELECT username, role, created_at FROM users ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]

def delete_user(username):
    with _lock:
        _conn.execute("DELETE FROM users WHERE username=?", (username,))
        _conn.commit()

def count_users():
    with _lock:
        return _conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


# ---------- 文档 / 向量 ----------
def add_document(title, dept, chunks, embeddings, status="已生效"):
    """与 delete_document 同理:向量先写、sqlite 后提交。此顺序下任何一步失败,
    最坏只留下孤儿向量(启动时 reconcile 自愈);反过来先提交 sqlite,
    chroma 写入失败会造成"目录有、书架无"的失明文档——列表可见、检索永远不到,且无自愈。"""
    doc_id = "d_" + uuid.uuid4().hex[:10]
    ts = time.time()
    ids = [f"{doc_id}#{i}" for i in range(len(chunks))]
    metas = [{"doc_id": doc_id, "doc_title": title, "dept": dept} for _ in chunks]
    with _lock:
        _col.add(ids=ids, embeddings=embeddings, metadatas=metas, documents=list(chunks))
        _conn.execute("INSERT INTO documents(id,title,dept,n_chunks,created_at,status) VALUES(?,?,?,?,?,?)",
                      (doc_id, title, dept, len(chunks), ts, status))
        for i, text in enumerate(chunks):
            _conn.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)", (ids[i], doc_id, i, text, title, dept))
        _conn.commit()
    return doc_id

def list_documents(depts=None):
    with _lock:
        rows = _conn.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
    out = [dict(r) for r in rows]
    if depts is not None:
        out = [d for d in out if d["dept"] in depts]
    return out

def delete_document(doc_id):
    """sqlite 是唯一 source of truth:先删向量、成功了才提交 sqlite(顺序反过来是风险 #2 的根因——
    旧版先提交 sqlite 再删 chroma,一旦后者失败,文档"看起来"已删,孤儿向量却仍会被检索到、
    被当成已删文档的引用来源答给用户)。chroma 删除失败会在这里直接抛出,sqlite 不受影响。"""
    with _lock:
        cids = [r["id"] for r in _conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,)).fetchall()]
        if cids:
            _col.delete(ids=cids)
        _conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        _conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        _conn.commit()


def reconcile():
    """启动自检:清掉 chroma 里 sqlite 已不认识的孤儿向量(风险 #2 自愈)。
    单向清理即可——sqlite 是唯一 source of truth,chroma 内容随时可从 sqlite 重建。
    反方向("sqlite 有、chroma 无"=失明文档)自动修不了(需重新 embed),但要报数让管理员看见。"""
    # ponytail: 全量比对,几十万 chunk 时启动变慢,届时改增量或后台跑
    with _lock:
        sqlite_ids = {r["id"] for r in _conn.execute("SELECT id FROM chunks").fetchall()}
        chroma_ids = set(_col.get(include=[])["ids"])
        orphans = list(chroma_ids - sqlite_ids)
        missing = len(sqlite_ids - chroma_ids)
        if orphans:
            _col.delete(ids=orphans)
    if orphans:
        print(f"[reconcile] 清理 chroma 孤儿向量 {len(orphans)} 条")
    if missing:
        print(f"[reconcile] 警告:{missing} 条 chunk 缺向量(文档在列表中但检索不到),请删除对应文档后重新上传")
    return len(orphans)

def _pending_doc_ids():
    """「待确认」文档的 id 集合。该状态的内容不进任何检索路径——
    标记≠治理(定稿 §7):未经人工裁决的低置信 OCR 内容,宁可检索不到,不可被权威引用。"""
    with _lock:
        return {r["id"] for r in _conn.execute(
            "SELECT id FROM documents WHERE status='待确认'").fetchall()}


def query(embedding, k, depts=None):
    pending = _pending_doc_ids()
    where = None if depts is None else {"dept": {"$in": depts}}
    with _lock:
        res = _col.query(query_embeddings=[embedding], n_results=k, where=where,
                         include=["documents", "metadatas", "distances"])
    hits = []
    if res["ids"] and res["ids"][0]:
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]):
            if meta["doc_id"] in pending:
                continue
            hits.append({"chunk_id": cid, "text": doc, "doc_id": meta["doc_id"],
                         "doc_title": meta["doc_title"], "dept": meta["dept"],
                         "score": round(1.0 - float(dist), 3)})
    return hits

def keyword_search(term, depts=None):
    pending = _pending_doc_ids()
    with _lock:
        rows = [dict(r) for r in _conn.execute("SELECT * FROM chunks WHERE text LIKE ?", (f"%{term}%",)).fetchall()]
    rows = [r for r in rows if r["doc_id"] not in pending]
    if depts is not None:
        rows = [r for r in rows if r["dept"] in depts]
    return rows

def stats(depts=None):
    docs = list_documents(depts)
    return {"documents": len(docs), "chunks": sum(d["n_chunks"] for d in docs)}


# ---------- 问答日志 / 分析 ----------
def log_qa(question, answer, role, refused=False, conversation_id=None, username=None,
           rewritten=None, answer_mode=None, tool_trace=None):
    qid = "q_" + uuid.uuid4().hex[:10]
    with _lock:
        _conn.execute("INSERT INTO qa_logs(id,question,answer,role,rating,refused,conversation_id,username,rewritten,answer_mode,tool_trace,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (qid, question, answer, role, 0, 1 if refused else 0, conversation_id, username,
                       rewritten, answer_mode, tool_trace, time.time()))
        _conn.commit()
    return qid


def get_history(conversation_id, username, limit=3):
    """服务端会话历史:只取「该会话且该用户」的最近 limit 轮。
    按 username 过滤是权限边界——伪造别人的 conversation_id 只会得到空历史。
    上下文卫生(铁律 3):进模型的历史先清洗——拒答轮只保留用户问题(道歉模板无信息量,
    还占 3 轮预算);答案剥掉 [资料N] 引用标记(实测改写器会把它学舌混进检索词)。"""
    if not conversation_id:
        return []
    with _lock:
        rows = _conn.execute(
            "SELECT question, answer, refused FROM qa_logs WHERE conversation_id=? AND username=? "
            "ORDER BY created_at DESC LIMIT ?", (conversation_id, username, limit)).fetchall()
    msgs = []
    for r in reversed(rows):
        msgs.append({"role": "user", "content": r["question"]})
        if not r["refused"]:
            msgs.append({"role": "assistant",
                         "content": re.sub(r"\[资料\d+\]", "", r["answer"]).strip()})
    return msgs

def rate_qa(qid, rating):
    with _lock:
        _conn.execute("UPDATE qa_logs SET rating=? WHERE id=?", (rating, qid))
        _conn.commit()

def qa_stats():
    with _lock:
        rows = _conn.execute("SELECT question, role, rating, refused, created_at FROM qa_logs ORDER BY created_at DESC").fetchall()
    total = len(rows)
    refused = sum(1 for r in rows if r["refused"])
    up = sum(1 for r in rows if r["rating"] == 1)
    down = sum(1 for r in rows if r["rating"] == -1)
    days = OrderedDict()
    today = datetime.date.today()
    for i in range(13, -1, -1):
        days[(today - datetime.timedelta(days=i)).isoformat()] = 0
    for r in rows:
        try:
            d = datetime.date.fromtimestamp(r["created_at"]).isoformat()
            if d in days:
                days[d] += 1
        except Exception:
            pass
    recent = [{"question_preview": "已隐藏", "role": r["role"], "refused": bool(r["refused"]), "rating": r["rating"]}
              for r in rows[:15]]
    return {"total": total, "refused": refused,
            "refused_rate": round(refused / total, 3) if total else 0,
            "up": up, "down": down,
            "by_day": [{"date": k, "count": v} for k, v in days.items()],
            "recent": recent}


# ---------- 混合检索(向量 + BM25 关键词)+ RRF 融合重排 ----------
# 中文常见停用词:用于判断"关键词是否真的命中",过滤掉无意义的词
_STOPWORDS = set("""
的 了 是 在 和 与 或 也 都 就 还 而 把 被 对 从 给 让 向 于 之 其 这 那 哪
什么 怎么 怎样 如何 为何 为什么 哪些 哪个 多少 是否 请问 介绍 告诉
我 你 他 她 它 我们 你们 他们 吗 呢 啊 吧 嘛 有 没 没有 一个 一些
关于 信息 内容 情况 相关 information
""".split())


def _tokenize(text):
    """中文分词(jieba):转小写、去空白。"""
    import jieba
    return [w.strip() for w in jieba.lcut((text or "").lower()) if w.strip()]


def _meaningful_terms(question):
    """从问题里抽出"有意义的检索词"(长度≥2 且非停用词)。"""
    return [t for t in set(_tokenize(question)) if len(t) >= 2 and t not in _STOPWORDS]


# BM25 索引缓存:分块数量/总长度没变就复用,变了自动重建
_bm25_cache = {"sig": None, "bm25": None, "ids": None, "meta": None}


def _get_bm25():
    from rank_bm25 import BM25Okapi
    with _lock:
        r = _conn.execute("SELECT COUNT(*) c, COALESCE(SUM(LENGTH(text)),0) s FROM chunks").fetchone()
        sig = (r["c"], r["s"])
        rows = [dict(x) for x in _conn.execute(
            "SELECT id, text, doc_id, doc_title, dept FROM chunks").fetchall()]
    if _bm25_cache["sig"] == sig and _bm25_cache["bm25"] is not None:
        return _bm25_cache
    corpus = [_tokenize(x["text"]) for x in rows]
    _bm25_cache.update(sig=sig,
                       bm25=BM25Okapi(corpus) if corpus else None,
                       ids=[x["id"] for x in rows],
                       meta={x["id"]: x for x in rows})
    return _bm25_cache


def _keyword_rank(question, depts, k):
    """BM25 关键词检索,返回 [(chunk_id, 分数, chunk信息), ...](已按分数降序)。"""
    cache = _get_bm25()
    if not cache["bm25"]:
        return []
    pending = _pending_doc_ids()
    import numpy as np
    scores = cache["bm25"].get_scores(_tokenize(question))
    out = []
    for i in np.argsort(scores)[::-1]:
        sc = float(scores[int(i)])
        if sc <= 0:
            break
        cid = cache["ids"][int(i)]
        m = cache["meta"][cid]
        if m["doc_id"] in pending:
            continue
        if depts is not None and m["dept"] not in depts:
            continue
        out.append((cid, sc, m))
        if len(out) >= k:
            break
    return out


def hybrid_search(embedding, question, k, depts=None, dense_n=20, sparse_n=20, rrf_k=60):
    """混合检索:向量(管语义)+ BM25 关键词(管精确词/人名/编号)→ RRF 融合重排 → 取前 k 条。
    只负责"检索+融合排序",不判定是否可信——可信判定统一交给 evidence_gate()(见风险 #6)。
    """
    dense = query(embedding, dense_n, depts)        # 复用已有的向量查询
    sparse = _keyword_rank(question, depts, sparse_n)

    rrf, info = {}, {}
    for rank, h in enumerate(dense):
        cid = h["chunk_id"]
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
        info[cid] = {"chunk_id": cid, "text": h["text"], "doc_id": h["doc_id"],
                     "doc_title": h["doc_title"], "dept": h["dept"],
                     "vec_score": h["score"], "kw_score": 0.0}
    for rank, (cid, sc, m) in enumerate(sparse):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
        if cid in info:
            info[cid]["kw_score"] = round(sc, 3)
        else:
            info[cid] = {"chunk_id": cid, "text": m["text"], "doc_id": m["doc_id"],
                         "doc_title": m["doc_title"], "dept": m["dept"],
                         "vec_score": 0.0, "kw_score": round(sc, 3)}

    out = []
    for cid, fused in sorted(rrf.items(), key=lambda x: x[1], reverse=True)[:k]:
        d = info[cid]
        # 展示用"相似度":向量命中显示余弦相似度;纯关键词命中则不显示该数字
        d["score"] = d["vec_score"] if d["vec_score"] > 0 else None
        out.append(d)
    return out


# ---------- 三值证据闸门(basic/pro/enterprise 共用,见风险 #6 + 三态回答模型) ----------
def evidence_gate(hits, question, cfg):
    """三值判定,返回 (mode, passed_hits):
      direct   充分证据 → 权威口吻直接答(向量分 ≥ min_score,或关键词证据分 ≥2)
      boundary 边缘证据 → 边界答:有据事实+空白声明+标注推断(仅低风险域 boundary_answer=true 时)
      refused  无证据   → 拒答
    进入哪一态是代码的决策权(定稿铁律 2);boundary 的措辞容器在 rag.BOUNDARY_PROMPT。
    cfg 来自域配置 domains/*.yaml:min_score / boundary_min / boundary_answer / extra_stopwords。
    extra_stopwords=域泛词表:通用域把"公司/培训/设备"级管理泛词计 0 分(防堆词凑分,s01/s02 攻击面);
    财务域不吸("发票/报销"虽短但信息量高)——词权重按域参数化,形状在引擎、参数在配置(增长规则)。
    关键词证据分:≥4 字长词记 2 分(特指词单独作证),2-3 字词记 1 分。
    ponytail: 字长是 IDF 的粗代理;语料大了换真 IDF 加权(升级路径见债表)。
    """
    drop = set(cfg.get("extra_stopwords") or [])
    # 复合泛词一并吸收:jieba 会把"培训流程"切成单个 4 字词(s02 实测借此绕过逐词吸收拿 2 分),
    # 含任一域泛词子串的词同样按泛词处理——"培训流程/设备管理/信息系统"在通用域都不配作证
    terms = [t for t in _meaningful_terms(question)
             if t not in drop and not any(s in t for s in drop)]

    def kw_score(h):
        return sum(2 if len(t) >= 4 else 1 for t in terms if t in (h.get("text") or ""))

    strong = [h for h in hits if (h.get("score") or 0) >= cfg["min_score"] or kw_score(h) >= 2]
    if strong:
        return "direct", strong
    if cfg.get("boundary_answer"):
        weak = [h for h in hits
                if (h.get("score") or 0) >= cfg.get("boundary_min", cfg["min_score"]) or kw_score(h) >= 1]
        if weak:
            return "boundary", weak
    return "refused", []
