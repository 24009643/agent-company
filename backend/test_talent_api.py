"""Step 2.1 端点集成自检:4 个 talent 端点全走 require_hr(I7 咽喉)。

进程内 TestClient + 临时库(DATA_DIR 在 import app 前改,不碰真实 data/);
lifespan 的 seed() 自动建 admin/finance/hr/staff,无需 Ollama。
运行:python test_talent_api.py
"""
import os, tempfile, sqlite3
os.environ["DATA_DIR"] = tempfile.mkdtemp()  # 必须早于 import app:强制临时库

from fastapi.testclient import TestClient
from app import app as api  # 模块名与 FastAPI 实例同名,取实例
import resume, rubric, store, talent_store


def _tok(c, u, p):
    r = c.post("/api/login", json={"username": u, "password": p})
    assert r.status_code == 200, f"{u} 登录失败:{r.text}"
    return {"Authorization": "Bearer " + r.json()["token"]}


def main():
    with TestClient(api) as c:
        hr = _tok(c, "hr", "hr123")
        staff = _tok(c, "staff", "staff123")
        admin = _tok(c, "admin", "admin123")
        finance = _tok(c, "finance", "finance123")
        editor = _tok(c, "hr_editor", "hreditor123")
        assert c.get("/api/talent", headers=editor).status_code == 200, "评分配置者应可使用人才库"

        # 1) 未登录 → 401
        assert c.get("/api/talent").status_code == 401, "未登录应 401"

        # 2) 非 hr(含 admin)打任意 talent 端点 → 403(I7:admin 是系统管理权非内容权)
        for h, who in [(staff, "staff"), (finance, "finance"), (admin, "admin")]:
            assert c.get("/api/talent", headers=h).status_code == 403, f"{who} 应 403"
            assert c.post("/api/talent", headers=h, json={"name": "x"}).status_code == 403, f"{who} 应 403"

        # 3) 列表默认分页且只给摘要,详情才给联系方式
        first_page = c.get("/api/talent", headers=hr).json()
        assert first_page["candidates"] == [] and first_page["total"] == 0, f"初始分页异常:{first_page}"
        cid = c.post("/api/talent", headers=hr,
                     json={"name": "张三", "phone": "13800000000", "email": "z@x.com",
                           "years": 3, "skills": "Python", "education": "本科",
                           "expected_salary": "20-25k", "source_file": "z.pdf"}).json()["id"]
        got = c.get("/api/talent/" + cid, headers=hr).json()
        assert got["name"] == "张三" and got["status"] == "草稿" and got["phone"] == "13800000000", f"读回异常:{got}"
        assert c.patch("/api/talent/" + cid + "/status", headers=hr,
                       json={"status": "offer", "reason": "跳过流程"}).status_code == 409, "草稿不得直达 offer"
        assert c.get("/api/talent/nope", headers=hr).status_code == 404, "不存在应 404"

        # 4) 审计随写且 actor=登录用户(端到端 actor 接线)
        con = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "talent.sqlite"))
        aud = con.execute("SELECT actor, action, target FROM audit").fetchall()
        con.close()
        assert ("hr", "create", cid) in aud, f"审计 actor 接线异常:{aud}"

        # 5) 简历摄取端点:非 hr 被 require_hr 挡在 LLM 之前(不碰 Ollama)
        assert c.post("/api/talent/ingest", headers=staff,
                      files={"file": ("r.txt", b"x", "text/plain")}).status_code == 403, "staff ingest 应 403"

        # 6) 确认页:HR 审定 → 状态 草稿→待筛;坏 email 机械校验拦(信任边界输入校验)
        cid2 = c.post("/api/talent", headers=hr, json={"name": "李四"}).json()["id"]
        r_ok = c.post("/api/talent/" + cid2 + "/confirm", headers=hr,
                      json={"name": "李四", "phone": "13900000000", "email": "l@x.com", "skills": "Java", "education": "本科"})
        assert r_ok.status_code == 200, f"确认应 200:{r_ok.text}"
        assert c.get("/api/talent/" + cid2, headers=hr).json()["status"] == "待筛", "确认后应转待筛"
        assert c.post("/api/talent/" + cid2 + "/confirm", headers=hr,
                      json={"name": "李四"}).status_code == 409, "非草稿不得重复确认"
        cid_bad = c.post("/api/talent", headers=hr, json={"name": "邮箱待校验"}).json()["id"]
        assert c.post("/api/talent/" + cid_bad + "/confirm", headers=hr,
                      json={"name": "邮箱待校验", "email": "not-an-email"}).status_code == 400, "坏 email 应 400"
        assert c.post("/api/talent/nope/confirm", headers=hr, json={"name": "x"}).status_code == 404, "不存在确认应 404"

        # 7) 状态机既校验枚举也校验迁移边;每次人工推进需要理由
        assert c.patch("/api/talent/" + cid2 + "/status", headers=hr,
                       json={"status": "随便写", "reason": "测试"}).status_code == 400, "非法状态应 400"
        assert c.patch("/api/talent/" + cid2 + "/status", headers=hr,
                       json={"status": "评估", "reason": "跳级"}).status_code == 409, "待筛不得跳到评估"
        for status, reason in [("约面", "安排首轮"), ("已面", "完成首轮"), ("评估", "进入评估")]:
            assert c.patch("/api/talent/" + cid2 + "/status", headers=hr,
                           json={"status": status, "reason": reason}).status_code == 200, f"应允许迁移到{status}"
        assert c.get("/api/talent/" + cid2, headers=hr).json()["status"] == "评估"

        # 8) Step 2.3:结构化筛选=纯 SQL 可复算(status/min_years/skill/education)
        cid3 = c.post("/api/talent", headers=hr,
                      json={"name": "王五", "years": 5, "skills": "Python, SQL",
                            "education": "硕士"}).json()["id"]
        c.post("/api/talent/" + cid3 + "/confirm", headers=hr,
               json={"name": "王五", "years": 5, "skills": "Python, SQL",
                     "education": "硕士"})
        filtered_page = c.get("/api/talent?status=待筛&min_years=4&skill=SQL&education=硕士&limit=1&offset=0",
                              headers=hr).json()
        got = filtered_page["candidates"]
        assert [x["id"] for x in got] == [cid3], f"筛选结果应只含王五:{got}"
        assert filtered_page["total"] == 1 and "phone" not in got[0] and "email" not in got[0], f"列表不应泄露 PII:{filtered_page}"

        # 9) Step 2.3:JD 差距=证据化差距,结构无裁决字段;坏条目被过滤并计数
        talent_store.save_resume(cid3, raw_text="王五 熟悉 Python 和 SQL,做过 5 年数据平台。", parsed_text=None)

        async def fake_gap(messages, model, format=None):
            return {"content": '{"items":['
                '{"requirement":"Spark","status":"未见","gap":"建议录用","evidence":"王五 熟悉 Python 和 SQL,做过 5 年数据平台。"},'
                '{"requirement":"数据平台","status":"有据支持","gap":"做过 5 年数据平台,可追问吞吐量","evidence":"王五 熟悉 Python 和 SQL,做过 5 年数据平台。"}'
                ']}'}

        old_chat_raw = resume.rag.chat_raw
        resume.rag.chat_raw = fake_gap
        try:
            gap = c.post("/api/talent/" + cid3 + "/jd-gap", headers=hr, json={"jd": "需要 Spark"}).json()
        finally:
            resume.rag.chat_raw = old_chat_raw
        assert len(gap["items"]) == 1 and gap["items"][0]["requirement"] == "数据平台", f"含'建议录用'的条目应被过滤:{gap}"
        assert gap["filtered"] == 1, f"应计过滤 1 条:{gap}"
        assert all(k in gap["items"][0] for k in ("requirement", "status", "gap", "evidence")), "新字段契约"
        assert "录用" not in str(gap["items"]) and "淘汰" not in str(gap["items"]), f"存活条目不得含裁决:{gap}"

        # 10) Step 2.3:analytics 最近问答默认不暴露 question 原文(候选人姓名/PII 不应给 admin recent)
        store.log_qa("候选人王五电话是多少?", "x", "hr", username="hr")
        recent = c.get("/api/analytics", headers=admin).json()["recent"]
        assert recent and "question" not in recent[0], f"recent 不应含问题原文:{recent[0]}"
        assert "question_preview" in recent[0], f"recent 应给脱敏预览字段:{recent[0]}"

        # 11) Step 2.4 第一刀:单人沟通只生成草稿,收件人字段由代码机械填充,LLM 只写正文
        async def fake_message(messages, model, format=None):
            return {"content": '{"subject":"面试邀请","body":"王五你好,请参加数据工程师面试。"}'}

        old_chat_raw = resume.rag.chat_raw
        resume.rag.chat_raw = fake_message
        try:
            msg = c.post("/api/talent/" + cid3 + "/message-draft", headers=hr,
                         json={"kind": "约面", "job_title": "数据工程师",
                               "interview_time": "周五 10:00", "location": "线上"}).json()
        finally:
            resume.rag.chat_raw = old_chat_raw
        assert msg["recipient"]["name"] == "王五" and msg["recipient"]["email"] == "", f"收件人必须由代码填:{msg}"
        assert msg["body"].startswith("尊敬的王五:"), f"称呼必须由代码机械拼,不由 LLM:{msg}"
        assert "warnings" in msg and "sent" not in msg, f"应有 warnings、无 sent:{msg}"

        # 12) Step 2.4 第一刀:面试问题生成=差距/待追问的措辞草稿,不做评价结论
        async def fake_questions(messages, model, format=None):
            return {"content": '{"questions":["请介绍一次你处理 Spark 任务性能问题的经历。"]}'}

        resume.rag.chat_raw = fake_questions
        try:
            qs = c.post("/api/talent/" + cid3 + "/interview-questions", headers=hr,
                        json={"jd": "需要 Spark", "gap_items": gap["items"]}).json()
        finally:
            resume.rag.chat_raw = old_chat_raw
        assert qs["questions"] == ["请介绍一次你处理 Spark 任务性能问题的经历。"], f"问题草稿异常:{qs}"
        assert "淘汰" not in str(qs) and "录用" not in str(qs), f"问题草稿不得含决策:{qs}"

        # 13) 草稿退回只允许删草稿,并在事务中移除岗位关联
        cidD = c.post("/api/talent", headers=hr, json={"name": "待删草稿"}).json()["id"]
        delete_jid = c.post("/api/jobs", headers=hr, json={"title": "草稿岗位", "jd_text": "Python"}).json()["id"]
        assert c.post("/api/jobs/" + delete_jid + "/candidates", headers=hr,
                      json={"candidate_id": cidD}).status_code == 409, "草稿不得挂到岗位"
        assert c.delete("/api/talent/" + cidD, headers=staff).status_code == 403, "非 hr 删应 403"
        assert c.delete("/api/talent/" + cidD, headers=hr).status_code == 200, "hr 删应 200"
        assert c.get("/api/talent/" + cidD, headers=hr).status_code == 404, "删后应 404"
        assert c.delete("/api/talent/" + cid3, headers=hr).status_code == 409, "已确认候选人不得硬删"
        assert c.delete("/api/talent/nope", headers=hr).status_code == 404, "删不存在应 404"

        # 14) Step 2.3.6:AI 简历体检端点=速写+亮点(有实据)+存疑;坏条目过滤,结构无裁决字段
        talent_store.save_resume(cid3, raw_text="王五 熟悉 Python 和 SQL,做过 5 年数据平台。", parsed_text=None)

        async def fake_review(messages, model, format=None):
            return {"content": '{"summary":"数据平台背景。","highlights":['
                '{"point":"做过 5 年数据平台","evidence":"王五 熟悉 Python 和 SQL,做过 5 年数据平台。"},'
                '{"point":"建议录用","evidence":"王五 熟悉 Python 和 SQL,做过 5 年数据平台。"}'
                '],"flags":[{"category":"夸大模糊","note":"无量化可追问","ask":"给个具体数字?"}]}'}

        old_chat_raw = resume.rag.chat_raw
        resume.rag.chat_raw = fake_review
        try:
            rev = c.post("/api/talent/" + cid3 + "/review", headers=hr).json()
        finally:
            resume.rag.chat_raw = old_chat_raw
        assert len(rev["highlights"]) == 1 and rev["filtered"] == 1, f"含'建议录用'的亮点应过滤:{rev}"
        assert "score" not in str(rev) and "录用" not in str(rev["highlights"]), f"不得含裁决:{rev}"
        assert rev["flags"][0]["category"] == "夸大模糊", f"存疑分类契约:{rev}"

        # 15) Step 2.6 Task 3:岗位/JD 对象 —— CRUD + 挂人(多对多)+ jd-gap 双入口;require_hr 咽喉(I7)
        assert c.post("/api/jobs", headers=staff, json={"title": "x", "jd_text": "y"}).status_code == 403, "非 hr 建岗位应 403"
        assert c.get("/api/jobs", headers=admin).status_code == 403, "admin 看岗位应 403(I7)"
        jid = c.post("/api/jobs", headers=hr,
                     json={"title": "数据工程师", "jd_text": "需要 Spark 与 5 年数据平台经验"}).json()["id"]
        assert any(j["id"] == jid for j in c.get("/api/jobs", headers=hr).json()["jobs"]), "岗位列表应含新岗位"
        job = c.get("/api/jobs/" + jid, headers=hr).json()
        assert job["jd_clean"] and job["status"] == "开放", f"get 应回 jd_clean + 默认状态开放:{job}"
        # 岗位 JD 分析必须先显式挂人，不能把任意 job_id 当通用评分入口
        assert c.post("/api/talent/" + cid3 + "/jd-gap", headers=hr,
                      json={"job_id": jid}).status_code == 409, "未挂岗位不得运行该岗位 JD 分析"
        # 挂人 / 摘人(多对多):只接受已确认候选人与开放岗位
        assert c.post("/api/jobs/" + jid + "/candidates", headers=hr, json={"candidate_id": cid3}).status_code == 200
        assert cid3 in c.get("/api/jobs/" + jid, headers=hr).json()["candidate_ids"], "挂载后应含候选人"
        listed_job = next(j for j in c.get("/api/jobs", headers=hr).json()["jobs"] if j["id"] == jid)
        assert cid3 in listed_job["candidate_ids"], "岗位列表应返回挂人状态供前端显示"
        # jd-gap 双入口:job_id 取岗位 JD 送进 prompt(与临时粘贴 jd 等价)
        seen = {}
        async def fake_gap_capture(messages, model, format=None):
            seen["msg"] = str(messages)
            return {"content": '{"items":[{"requirement":"数据平台","status":"有据支持",'
                    '"gap":"做过 5 年数据平台,可追问吞吐量","evidence":"王五 熟悉 Python 和 SQL,做过 5 年数据平台。"}]}'}
        old_chat_raw = resume.rag.chat_raw
        resume.rag.chat_raw = fake_gap_capture
        try:
            by_job = c.post("/api/talent/" + cid3 + "/jd-gap", headers=hr, json={"job_id": jid}).json()
        finally:
            resume.rag.chat_raw = old_chat_raw
        assert "数据平台经验" in seen["msg"], "job_id 入口应把岗位 JD 送进 LLM 输入"
        assert by_job["items"] and by_job["items"][0]["requirement"] == "数据平台", f"job_id 入口应出差距:{by_job}"
        assert c.post("/api/talent/" + cid3 + "/jd-gap", headers=hr, json={"job_id": "nope"}).status_code == 404, "坏 job_id 应 404"
        assert c.patch("/api/jobs/" + jid, headers=hr, json={"status": "关闭"}).status_code == 200
        assert c.get("/api/jobs/" + jid, headers=hr).json()["status"] == "关闭", "状态应改为关闭"
        assert c.post("/api/jobs/" + jid + "/candidates", headers=hr,
                      json={"candidate_id": cid2}).status_code == 409, "关闭岗位不得挂人"
        assert c.delete("/api/jobs/" + jid + "/candidates/" + cid3, headers=hr).status_code == 200
        assert cid3 not in c.get("/api/jobs/" + jid, headers=hr).json()["candidate_ids"], "摘除后应移出"
        # 16) rubric: 普通 HR 无配置权；评分配置者可建草稿、冻结；冻结版本不可改
        rubric_jid = c.post("/api/jobs", headers=hr,
                             json={"title": "评分岗位", "jd_text": "熟悉 Python 与数据平台"}).json()["id"]
        assert c.post("/api/jobs/" + rubric_jid + "/rubrics", headers=hr, json={}).status_code == 403, "普通 HR 不得配置 rubric"
        assert c.post("/api/jobs/" + rubric_jid + "/rubrics/draft", headers=hr).status_code == 403, "普通 HR 不得让模型越权生成 rubric"
        for h, who in [(staff, "staff"), (finance, "finance"), (admin, "admin")]:
            assert c.post("/api/jobs/" + rubric_jid + "/rubrics", headers=h, json={}).status_code == 403, f"{who} 不得配置 rubric"
            assert c.post("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/evaluate", headers=h).status_code == 403, f"{who} 不得运行评分"
        rubric_body = {"high_priority_threshold": 75, "dimensions": [
            {"name": "Python", "jd_requirement": "熟悉 Python", "importance_level": "priority", "assessment_mode": "resume_evidence", "weight": 30, "evidence_rule": "简历有 Python 项目"},
            {"name": "数据平台", "jd_requirement": "数据平台经验", "importance_level": "priority", "assessment_mode": "resume_evidence", "weight": 25, "evidence_rule": "简历有平台经历"},
            {"name": "SQL", "jd_requirement": "SQL 分析", "importance_level": "important", "assessment_mode": "resume_evidence", "weight": 20, "evidence_rule": "简历有 SQL"},
            {"name": "数据建模", "jd_requirement": "数据建模", "importance_level": "important", "assessment_mode": "resume_evidence", "weight": 15, "evidence_rule": "简历有建模"},
            {"name": "跨部门沟通", "jd_requirement": "跨部门推动", "importance_level": "bonus", "assessment_mode": "self_reported", "weight": 10, "evidence_rule": "简历有沟通职责"},
        ]}
        created = c.post("/api/jobs/" + rubric_jid + "/rubrics", headers=editor, json=rubric_body)
        assert created.status_code == 200, f"评分配置者应可建 rubric:{created.text}"
        rid = created.json()["id"]
        assert c.get("/api/rubrics/" + rid, headers=hr).json()["jd_fingerprint"], "创建规则应记录 JD 指纹(§12.1/§1.4)"
        assert c.get("/api/jobs/" + rubric_jid + "/rubrics", headers=hr).json()["rubrics"][0]["id"] == rid, "HR 应能读取 rubric"
        assert c.post("/api/rubrics/" + rid + "/freeze", headers=editor).status_code == 200, "应可冻结合法 rubric"
        assert c.patch("/api/rubrics/" + rid, headers=editor,
                       json={"high_priority_threshold": 80}).status_code == 409, "冻结版本不得修改"
        # §12.2 重新平衡=确定性预览(不调 LLM);仅配置者;权重 5 点步进合计 100。
        assert c.post("/api/rubrics/rebalance", headers=hr, json={"dimensions": []}).status_code == 403, "普通 HR 不得用配置端点"
        rb = c.post("/api/rubrics/rebalance", headers=editor, json={"dimensions": [
            {"importance_level": "priority", "assessment_mode": "resume_evidence"},
            {"importance_level": "priority", "assessment_mode": "resume_evidence"},
            {"importance_level": "important", "assessment_mode": "resume_evidence"},
            {"importance_level": "important", "assessment_mode": "resume_evidence"},
            {"importance_level": "bonus", "assessment_mode": "self_reported"}]}).json()
        assert sum(rb["weights"]) == 100 and all(w % 5 == 0 for w in rb["weights"]), f"重新平衡应 5 点步进合计 100:{rb}"
        # §12.2 AI 拆解:草稿池端点(仅配置者)返回候选而非直接维度。
        async def fake_pool_ep(messages, model, format=None):
            return {"content": '{"candidates":[{"name":"SQL 分析","source_type":"jd_inferred","importance_level":"priority","assessment_mode":"resume_evidence","evidence_rule":"有 SQL","confidence":"中"}]}'}
        _old_pool = rubric.rag.chat_raw
        rubric.rag.chat_raw = fake_pool_ep
        try:
            pool_resp = c.post("/api/jobs/" + rubric_jid + "/rubrics/draft", headers=editor, json={"hiring_focus": "更偏数据"})
        finally:
            rubric.rag.chat_raw = _old_pool
        assert pool_resp.status_code == 200 and "candidates" in pool_resp.json(), f"草稿池端点应返回候选:{pool_resp.text}"
        # §13 复制为新草稿:冻结版复制出可编辑草稿,原版仍冻结;普通 HR 不得复制。
        copy_id = c.post("/api/rubrics/" + rid + "/copy", headers=editor).json()["id"]
        assert c.get("/api/rubrics/" + copy_id, headers=hr).json()["status"] == "草稿", "复制应产出新草稿"
        assert c.post("/api/rubrics/" + rid + "/copy", headers=hr).status_code == 403, "普通 HR 不得复制配置"
        # 17) 评分=冻结 rubric + 已挂岗位 + 原文证据，分数由代码算；迁移证据仅改变队列
        unready_jid = c.post("/api/jobs", headers=hr,
                              json={"title": "未挂人评分岗位", "jd_text": "Python"}).json()["id"]
        assert c.post("/api/jobs/" + unready_jid + "/candidates/" + cid3 + "/evaluate", headers=editor).status_code == 409, "评分配置者不可绕过候选人挂岗和冻结咽喉"
        assert c.post("/api/jobs/" + rubric_jid + "/candidates", headers=hr,
                      json={"candidate_id": cid3}).status_code == 200
        frozen = c.get("/api/rubrics/" + rid, headers=hr).json()
        dim_ids = [d["id"] for d in frozen["dimensions"]]
        async def fake_evaluation(messages, model, format=None):
            return {"content": '{"items":['
                + '{"dimension_id":"' + dim_ids[0] + '","evidence_status":"confirmed","note":"有 Python","evidence":["熟悉 Python 和 SQL"],"transferable":false},'
                + '{"dimension_id":"' + dim_ids[1] + '","evidence_status":"partial","note":"平台可迁移","evidence":["做过 5 年数据平台"],"transferable":true},'
                + '{"dimension_id":"' + dim_ids[2] + '","evidence_status":"confirmed","note":"有 SQL","evidence":["熟悉 Python 和 SQL"],"transferable":false},'
                + '{"dimension_id":"' + dim_ids[3] + '","evidence_status":"not_evidenced","note":"未见建模","evidence":[],"transferable":false},'
                + '{"dimension_id":"' + dim_ids[4] + '","evidence_status":"not_evidenced","note":"未见沟通","evidence":[],"transferable":false}'
                + ']}' }
        old_chat_raw = rubric.rag.chat_raw
        rubric.rag.chat_raw = fake_evaluation
        try:
            evaluated = c.post("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/evaluate", headers=hr)
        finally:
            rubric.rag.chat_raw = old_chat_raw
        assert evaluated.status_code == 200, f"应可运行评分:{evaluated.text}"
        score = evaluated.json()
        assert score["status"] == "assessed" and score["direct_fit_score"] == 62.5, f"分数必须由冻结权重×证据系数计算(30+25×0.5+20):{score}"
        assert score["queue"] == "必须复核", f"迁移证据应提升复核优先级但不改直接分:{score}"
        assert c.get("/api/talent/" + cid3, headers=hr).json()["status"] == "待筛", "评分不得自动改变候选人状态"
        latest_score = c.get("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/evaluate", headers=hr).json()
        assert latest_score["id"] == score["id"] and latest_score["is_current"] is True, "应可读取当前简历对应的最新评分"
        # 评分必须绑定简历原文版本。候选人更新简历后，旧评分不可继续作为当前矩阵或复核依据。
        talent_store.save_resume(cid3, raw_text="王五更新后的简历：Python 与 SQL，新增 Spark 项目。", parsed_text=None)
        stale_score = c.get("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/evaluate", headers=hr).json()
        assert stale_score["is_current"] is False, f"简历变更后历史评分必须标为失效:{stale_score}"
        # 新冻结版本可重新评估，但必须返回不同 rubric_id；前端矩阵据此拒绝混排。
        v2 = c.post("/api/jobs/" + rubric_jid + "/rubrics", headers=editor, json=rubric_body)
        assert v2.status_code == 200 and c.post("/api/rubrics/" + v2.json()["id"] + "/freeze", headers=editor).status_code == 200
        talent_store.save_resume(cid3, raw_text="王五 熟悉 Python 和 SQL,做过 5 年数据平台。", parsed_text=None)
        v2_dimensions = c.get("/api/rubrics/" + v2.json()["id"], headers=hr).json()["dimensions"]
        async def fake_v2_evaluation(messages, model, format=None):
            return {"content": '{"items":['
                + '{"dimension_id":"' + v2_dimensions[0]["id"] + '","evidence_status":"confirmed","note":"有 Python","evidence":["熟悉 Python 和 SQL"],"transferable":false},'
                + '{"dimension_id":"' + v2_dimensions[1]["id"] + '","evidence_status":"partial","note":"平台可迁移","evidence":["做过 5 年数据平台"],"transferable":true},'
                + '{"dimension_id":"' + v2_dimensions[2]["id"] + '","evidence_status":"confirmed","note":"有 SQL","evidence":["熟悉 Python 和 SQL"],"transferable":false},'
                + '{"dimension_id":"' + v2_dimensions[3]["id"] + '","evidence_status":"not_evidenced","note":"未见建模","evidence":[],"transferable":false},'
                + '{"dimension_id":"' + v2_dimensions[4]["id"] + '","evidence_status":"not_evidenced","note":"未见沟通","evidence":[],"transferable":false}'
                + ']}' }
        rubric.rag.chat_raw = fake_v2_evaluation
        try:
            v2_score = c.post("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/evaluate", headers=hr).json()
        finally:
            rubric.rag.chat_raw = old_chat_raw
        assert v2_score["rubric_id"] != score["rubric_id"] and v2_score["is_current"] is True, f"跨版本评分必须显式隔离:{v2_score}"
        queue = c.get("/api/jobs/" + rubric_jid + "/evaluation-queue", headers=hr)
        assert queue.status_code == 200, f"岗位评分队列应可读取:{queue.text}"
        queue_row = queue.json()["candidates"][0]
        assert queue_row["candidate_id"] == cid3 and queue_row["queue"] == "必须复核" and queue_row["is_current"] is True, f"队列只应消费当前简历和当前冻结版本:{queue_row}"
        # B-1 §8:定向重试端点——非 hr 403;hr 可重试(无失败维度则原样返回评分)。
        assert c.post("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/evaluate/retry", headers=staff).status_code == 403, "非 HR 不得重试评分"
        rr = c.post("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/evaluate/retry", headers=hr)
        assert rr.status_code == 200 and rr.json()["status"] in ("assessed", "partial"), f"重试应返回评分:{rr.text}"
        # B-2 §7.4:考核点标注端点——非 hr 403;坏状态 400;缺维度 404;hr 标 verified 落库且不回写分。
        dim0 = v2_dimensions[0]["id"]
        assert c.post("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/checkpoints", headers=staff, json={"dimension_id": dim0, "checkpoint_status": "verified"}).status_code == 403, "非 HR 不得标注考核点"
        assert c.post("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/checkpoints", headers=hr, json={"dimension_id": dim0, "checkpoint_status": "乱填"}).status_code == 400, "考核点状态非法应 400"
        assert c.post("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/checkpoints", headers=hr, json={"dimension_id": "nope", "checkpoint_status": "verified"}).status_code == 404, "评分中无此维度应 404"
        score_before = c.get("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/evaluate", headers=hr).json()["direct_fit_score"]
        cpres = c.post("/api/jobs/" + rubric_jid + "/candidates/" + cid3 + "/checkpoints", headers=hr, json={"dimension_id": dim0, "checkpoint_status": "verified"})
        assert cpres.status_code == 200, f"HR 标注考核点应成功:{cpres.text}"
        marked = [it for it in cpres.json()["items"] if str(it["dimension_id"]) == dim0][0]
        assert marked["checkpoint_status"] == "verified", f"考核点状态应落库:{marked}"
        assert cpres.json()["direct_fit_score"] == score_before, "标注考核点不得回写直接契合分"
        # 审计:建岗位落 create_job
        con = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "talent.sqlite"))
        ja = con.execute("SELECT actor, action FROM audit WHERE action='create_job'").fetchall()
        con.close()
        assert ("hr", "create_job") in ja, f"建岗位应落审计:{ja}"
        # 删岗位 → 404
        assert c.delete("/api/jobs/" + jid, headers=hr).status_code == 200
        assert c.get("/api/jobs/" + jid, headers=hr).status_code == 404, "删后应 404"

        # ===== block 16: Step 2.4 群发端点(Approach 1 索引契约 + 🟡T2-A/T2-E)=====
        import mailer, smtplib
        # cid3 已有 v2 冻结评分(confirmed 维度 Python/SQL);补邮箱 + 约面状态 → 可产出 pending
        talent_store.update_candidate(cid3, "hr", email="c3@x.com")
        c_nomail = c.post("/api/talent", headers=hr, json={"name": "无邮箱", "email": ""}).json()["id"]
        c_wrong = c.post("/api/talent", headers=hr, json={"name": "待筛人", "email": "w@x.com"}).json()["id"]
        # 直接置状态(测试夹具,绕过状态机转移守卫;与本文件既有 save_resume/update_candidate 直调同类)
        for _cc, _st in ((cid3, "约面"), (c_nomail, "约面"), (c_wrong, "待筛")):
            talent_store._tconn.execute("UPDATE candidates SET status=? WHERE id=?", (_st, _cc))
        talent_store._tconn.commit()

        # 发信路径已无 LLM(argmax 确定性选材):cid3 有 confirmed 维度 Python/SQL → 直接产出 pending,无需打桩 chat_raw
        try:
            assert c.post("/api/send-batches", headers=staff, json={"mail_type": "约面", "job_id": rubric_jid, "candidate_ids": [cid3]}).status_code == 403
            assert c.post("/api/send-batches", headers=hr, json={"mail_type": "约面", "job_id": rubric_jid, "candidate_ids": ["x"] * 51}).status_code == 400
            assert c.post("/api/send-batches", headers=hr, json={"mail_type": "乱来", "job_id": rubric_jid, "candidate_ids": [cid3]}).status_code == 400
            assert c.post("/api/send-batches", headers=hr, json={"mail_type": "约面", "job_id": "nojob", "candidate_ids": [cid3]}).status_code == 404
            # 建批:cid3 pending(评分+约面+邮箱)、c_nomail bad_email、c_wrong status_gate
            r = c.post("/api/send-batches", headers=hr, json={"mail_type": "约面", "job_id": rubric_jid,
                       "candidate_ids": [cid3, c_nomail, c_wrong], "interview_time": "周五 14:00", "location": "3F"})
            assert r.status_code == 200, r.text
            b = r.json(); bid = b["id"]
            st = {i["candidate_id"]: i["status"] for i in b["items"]}
            assert st[cid3] == "pending", f"有评分+约面+邮箱应 pending:{b['items']}"
            assert st[c_nomail] == "exception" and st[c_wrong] == "exception"
            exc = {i["candidate_id"]: (i["exception_reason"] or "") for i in b["items"] if i["status"] == "exception"}
            assert "邮箱" in exc[c_nomail] and "状态" in exc[c_wrong], f"例外须带人话解释:{exc}"
            pend = next(i for i in b["items"] if i["status"] == "pending")
            assert (pend["material_source"] or "").startswith("rubric:"), f"T1-C:material_source 结构须 rubric:*:{pend['material_source']}"
            assert "Python" in pend["body"], "个性化句应含 house 维度名"

            # SMTP 送信路径(fake):confirm → pending→sent(过 _send_pending 的 T2-E 快照重验 + T2-A on_result)
            class FakeSMTP:   # 灵活签名:465→SMTP_SSL、非465→SMTP,两路都吃
                def __init__(self, *a, **k): pass
                def ehlo(self): pass
                def has_extn(self, name): return True
                def starttls(self, *a, **k): pass
                def login(self, *a, **k): pass
                def send_message(self, msg): pass
                def quit(self): pass
            old_conf, old_smtp, old_ssl = mailer.smtp_configured, smtplib.SMTP, smtplib.SMTP_SSL
            mailer.smtp_configured = lambda: True
            smtplib.SMTP = FakeSMTP
            smtplib.SMTP_SSL = FakeSMTP
            try:
                # 🟡B:建批 B(cid3 pending)→ 确认前把 cid3 漂到淘汰 → 发送时状态门复检应拦下,不发
                bidB = c.post("/api/send-batches", headers=hr, json={"mail_type": "约面", "job_id": rubric_jid, "candidate_ids": [cid3]}).json()["id"]
                talent_store._tconn.execute("UPDATE candidates SET status='淘汰' WHERE id=?", (cid3,)); talent_store._tconn.commit()
                cb = c.post("/api/send-batches/" + bidB + "/confirm", headers=hr)
                assert cb.json()["sent"] == 0, "确认窗口内漂到淘汰,发送时状态门应拦下:" + cb.text
                itB = c.get("/api/send-batches/" + bidB, headers=hr).json()["items"][0]
                assert itB["status"] == "failed" and "状态门" in (itB["fail_reason"] or ""), itB
                talent_store._tconn.execute("UPDATE candidates SET status='约面' WHERE id=?", (cid3,)); talent_store._tconn.commit()
                # 主流程:确认批 A → cid3 正常发出
                cr = c.post("/api/send-batches/" + bid + "/confirm", headers=hr)
                assert cr.status_code == 200 and cr.json()["mode"] == "smtp" and cr.json()["sent"] == 1, cr.text
                b2 = c.get("/api/send-batches/" + bid, headers=hr).json()
                assert {i["candidate_id"]: i["status"] for i in b2["items"]}[cid3] == "sent"
                # 🔴 双发竞态:已确认批再确认 → 409(批 CAS 领取失败=防重复发送)
                assert c.post("/api/send-batches/" + bid + "/confirm", headers=hr).status_code == 409, "已确认批再确认应 409"
            finally:
                mailer.smtp_configured = old_conf
                smtplib.SMTP = old_smtp
                smtplib.SMTP_SSL = old_ssl

            # 防重:cid3 已 sent,同岗同类再建批 → duplicate 例外
            r2 = c.post("/api/send-batches", headers=hr, json={"mail_type": "约面", "job_id": rubric_jid, "candidate_ids": [cid3]})
            it2 = r2.json()["items"][0]; bid2 = r2.json()["id"]
            assert it2["status"] == "exception" and "收到过" in (it2["exception_reason"] or ""), it2
            # 导出 CSV:sent 行入表 + 落审计
            ex = c.get("/api/send-batches/" + bid + "/export.csv", headers=hr)
            assert ex.status_code == 200 and "c3@x.com" in ex.text, ex.text[:200]
            # F-CSV-1 回归:把已 sent 项的 to_name/body 换成公式载体(夹具,同本文件既有直改 DB 风格),
            # 导出后任一单元格都不得以电子表格公式引导符起头(否则 HR 打开即被求值 → HYPERLINK/IMPORTDATA 外传 PII)
            import csv as _csv, io as _io
            talent_store._tconn.execute("UPDATE send_items SET to_name=?, body=? WHERE batch_id=? AND candidate_id=?",
                                        ('=HYPERLINK("http://evil","x")', '=cmd|\'/c calc\'!A1', bid, cid3))
            talent_store._tconn.commit()
            ex2 = c.get("/api/send-batches/" + bid + "/export.csv", headers=hr)
            rows_csv = list(_csv.reader(_io.StringIO(ex2.text.lstrip("﻿"))))
            for row in rows_csv[1:]:
                for cell in row:
                    assert not cell or cell[0] not in ("=", "+", "-", "@", "\t", "\r"), f"CSV 公式注入未中和: {cell!r}"
            assert any("HYPERLINK" in cell for row in rows_csv for cell in row), "公式载体应作为文本保留在导出中"
            # resolve:草稿批的例外 send_skeleton → 400(未确认不得直发);skip 任意时刻 → 200
            assert c.post("/api/send-items/" + it2["id"] + "/resolve", headers=hr, json={"action": "send_skeleton"}).status_code == 400
            assert c.post("/api/send-items/" + it2["id"] + "/resolve", headers=hr, json={"action": "skip"}).status_code == 200
        finally:
            pass  # 发信路径无 LLM,无 chat_raw 需还原;SMTP 打桩由内层 try/finally 各自还原
        con2 = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "talent.sqlite"))
        acts = {a[0] for a in con2.execute("SELECT action FROM audit").fetchall()}
        con2.close()
        assert {"create_batch", "confirm_batch", "send_item", "export_batch"} <= acts, f"群发审计缺失:{acts}"
        print("block16 群发端点 OK")

    print("talent 端点集成自检通过 ✅(非 hr 全 403,hr CRUD+摄取+确认走通,审计正确)")


if __name__ == "__main__":
    main()
