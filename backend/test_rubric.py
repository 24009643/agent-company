"""岗位评分纯函数自检:脱敏分析视图、固定公式与复核队列。运行:python test_rubric.py"""
import asyncio
import rubric


def main():
    # 同一冻结权重和证据等级必须得到同一直接匹配分，模型不得参与公式。
    items = [{"weight": 60, "evidence_level": 4}, {"weight": 40, "evidence_level": 2}]
    assert rubric.direct_fit_score(items) == 80
    # 取整必须发生在总和之后，不能逐维截断而少算 1 分。
    assert rubric.direct_fit_score([
        {"weight": 49, "evidence_level": 2}, {"weight": 51, "evidence_level": 2},
    ]) == 50

    # 可迁移能力提升复核优先级，不给直接匹配分暗中加分。
    assert rubric.review_queue({"status": "assessed", "score": 60,
                                "transferable": True, "key_review": False}, 75) == "必须复核"
    assert rubric.review_queue({"status": "assessed", "score": 80,
                                "transferable": False, "key_review": False}, 75) == "优先评估"
    assert rubric.review_queue({"status": "unassessed", "score": None,
                                "transferable": False, "key_review": False}, 75) == "未评估"

    # 所有模型输入都经统一分析视图:不含结构化学校档次或原文年龄。
    view = rubric.analysis_view(
        {"name": "王五", "education": "某985大学 硕士", "years": 5,
         "skills": "Python, SQL", "expected_salary": "30k"},
        "王五，32岁，已婚。主导数据平台和 Python ETL。", [])
    assert "985" not in view and "32岁" not in view and "已婚" not in view, f"分析视图泄露受保护信息:{view}"
    assert "Python" in view and "数据平台" in view, f"岗位相关证据被误删:{view}"

    # Step 1 §5.3/§5.4:AI 能力候选池——不可验证/受保护候选剔除,jd_explicit 贴不回则降级,招聘重点来源保留。
    old_chat = rubric.rag.chat_raw
    async def fake_pool(messages, model, format=None):
        return {"content": '{"candidates":['
            '{"group":"岗位专业能力","name":"SQL 经营分析","source_type":"jd_explicit","source_quote":"懂 SQL","importance_reason":"直接产出","importance_level":"priority","rank":1,"assessment_mode":"resume_evidence","evidence_rule":"简历有 SQL","interview_probe":"现场解释异常","confidence":"高"},'
            '{"group":"学习适应与发展潜力","name":"综合素质","source_type":"jd_inferred","source_quote":"","importance_reason":"泛","importance_level":"important","rank":1,"assessment_mode":"resume_evidence","evidence_rule":"","interview_probe":"","confidence":"中"},'
            '{"group":"岗位专业能力","name":"年龄卡控","source_type":"jd_explicit","source_quote":"年龄30岁以下","importance_reason":"x","importance_level":"important","rank":2,"assessment_mode":"resume_evidence","evidence_rule":"x","interview_probe":"x","confidence":"高"},'
            '{"group":"业务理解与问题判断","name":"业务判断","source_type":"jd_explicit","source_quote":"这句JD里没有","importance_reason":"判断力","importance_level":"important","rank":1,"assessment_mode":"resume_evidence","evidence_rule":"简历有业务","interview_probe":"举例","confidence":"中"},'
            '{"group":"协作沟通与组织影响","name":"跨部门协调","source_type":"hiring_focus","source_quote":"更偏协调","importance_reason":"团队缺协调","importance_level":"important","rank":1,"assessment_mode":"self_reported","evidence_rule":"简历有协调","interview_probe":"举例","confidence":"中"}'
            ']}' }
    rubric.rag.chat_raw = fake_pool
    try:
        pool = asyncio.run(rubric.generate_capability_pool("懂 SQL 与产品", "更偏协调", "qwen"))
    finally:
        rubric.rag.chat_raw = old_chat
    names = [c["name"] for c in pool["candidates"]]
    assert "综合素质" not in names, f"不可验证名词应剔除:{names}"
    assert "年龄卡控" not in names, f"受保护候选应剔除:{names}"
    byname = {c["name"]: c for c in pool["candidates"]}
    assert byname["SQL 经营分析"]["source_type"] == "jd_explicit" and byname["SQL 经营分析"]["preselect"] is True, f"高置信 JD 明文应预选:{byname.get('SQL 经营分析')}"
    assert byname["业务判断"]["source_type"] == "jd_inferred" and byname["业务判断"]["source_quote"] == "", "jd_explicit 贴不回 JD 应降级并清空引用"
    assert byname["跨部门协调"]["source_type"] == "hiring_focus", "招聘重点来源应保留"

    # 模型把另一位候选人的文字伪装成证据时，不能生成一份看似低分或高分的结果。
    async def fake_other_candidate(messages, model, format=None):
        return {"content": '{"items":['
            '{"dimension_id":"py","evidence_status":"confirmed","note":"有 Python","evidence":["李四熟悉 Python"],"transferable":false},'
            '{"dimension_id":"platform","evidence_status":"partial","note":"有平台经验","evidence":["李四做过数据平台"],"transferable":true}'
            ']}' }
    frozen = {"high_priority_threshold": 75, "dimensions": [
        {"id": "py", "name": "Python", "jd_requirement": "熟悉 Python", "kind": "核心", "weight": 60},
        {"id": "platform", "name": "数据平台", "jd_requirement": "数据平台经验", "kind": "重要", "weight": 40},
    ]}
    rubric.rag.chat_raw = fake_other_candidate
    try:
        forged = asyncio.run(rubric.evaluate_candidate(
            {"years": 5, "skills": "Python"}, "王五熟悉 Python，做过数据平台。", frozen, "qwen"))
    finally:
        rubric.rag.chat_raw = old_chat
    assert forged["status"] == "unassessed" and forged["direct_fit_score"] is None, f"他人伪造证据必须使整次评分失效:{forged}"

    # Step 0 止血(§2.1/§8):单个维度证据贴不回,不再让整份评分作废——保留可取证维度,坏维度记 needs_review。
    async def fake_partial(messages, model, format=None):
        return {"content": '{"items":['
            '{"dimension_id":"py","evidence_status":"confirmed","note":"有 Python","evidence":["王五主导 Python 数据平台"],"transferable":false},'
            '{"dimension_id":"platform","evidence_status":"confirmed","note":"平台","evidence":["这句话简历里根本没有"],"transferable":false}'
            ']}' }
    rubric.rag.chat_raw = fake_partial
    try:
        partial = asyncio.run(rubric.evaluate_candidate(
            {"years": 5, "skills": "Python"}, "王五主导 Python 数据平台。", frozen, "qwen"))
    finally:
        rubric.rag.chat_raw = old_chat
    assert partial["status"] != "unassessed", f"单维证据失败不应作废整份评分:{partial}"
    # py 维 confirmed(60×4);platform 维证据贴不回→needs_review 不计分。暂定分=60×4//4=60,覆盖率=60。
    assert partial["direct_fit_score"] == 60, f"暂定分只来自可取证维度:{partial}"
    assert partial["coverage"] == 60, f"覆盖率=可取证维度权重之和:{partial}"
    by_id = {it["dimension_id"]: it for it in partial["items"]}
    assert by_id["platform"]["evidence_status"] == "needs_review", f"贴不回的维度记 needs_review 而非静默 0 分:{partial}"
    assert partial["queue"] == "必须复核", f"覆盖率<100 的暂定结果不进优先自动分组,必须复核:{partial}"

    # 阶段C 根因③:部分维度 id 容错——模型只返回部分维度(长/复杂简历常见),不再整份丢弃。
    # 匹配到的照常评;缺失维度机械判 needs_review(不计覆盖率、不编分);一个都没对上才 unassessed。
    async def fake_missing_dim(messages, model, format=None):
        return {"content": '{"items":['
            '{"dimension_id":"py","evidence_status":"confirmed","note":"有 Python","evidence":["王五主导 Python 数据平台"],"transferable":false}'
            ']}' }  # 只返回 py,漏了 platform
    rubric.rag.chat_raw = fake_missing_dim
    try:
        miss = asyncio.run(rubric.evaluate_candidate(
            {"years": 5, "skills": "Python"}, "王五主导 Python 数据平台。", frozen, "qwen"))
    finally:
        rubric.rag.chat_raw = old_chat
    assert miss["status"] != "unassessed", f"部分维度缺失不应整份丢弃:{miss}"
    bm = {it["dimension_id"]: it for it in miss["items"]}
    assert bm["py"]["evidence_status"] == "confirmed", f"返回的维度照常评:{miss}"
    assert bm["platform"]["evidence_status"] == "needs_review", f"缺失维度机械判 needs_review:{miss}"
    assert bm["platform"]["failure_code"] == "model_omitted_dimension", f"缺维失败码可区分:{miss}"
    assert miss["coverage"] == 60 and miss["direct_fit_score"] == 60, f"覆盖率/分只算已评维度 py=60:{miss}"

    # 一个维度都没对上(全错 id)→ 诚实 unassessed,绝不编 0 分。
    async def fake_all_wrong_ids(messages, model, format=None):
        return {"content": '{"items":['
            '{"dimension_id":"不存在的维度","evidence_status":"confirmed","note":"x","evidence":["王五主导 Python 数据平台"],"transferable":false}'
            ']}' }
    rubric.rag.chat_raw = fake_all_wrong_ids
    try:
        allwrong = asyncio.run(rubric.evaluate_candidate(
            {"years": 5, "skills": "Python"}, "王五主导 Python 数据平台。", frozen, "qwen"))
    finally:
        rubric.rag.chat_raw = old_chat
    assert allwrong["status"] == "unassessed", f"零维度匹配必须诚实未评估,绝不编低分:{allwrong}"

    # 覆盖率<100 即使达阈值也不给「优先评估」;覆盖率 100 达阈值不回归。
    assert rubric.review_queue({"status": "assessed", "score": 90, "coverage": 60,
                                "transferable": False, "key_review": False}, 75) == "必须复核"
    assert rubric.review_queue({"status": "assessed", "score": 90, "coverage": 100,
                                "transferable": False, "key_review": False}, 75) == "优先评估"

    # Step 1 §6.2:权重由代码按 5 点步进确定性生成——同输入同输出、合计 100、单位≥5。
    def L(imp, mode="resume_evidence"):
        return {"importance_level": imp, "assessment_mode": mode}
    clean = [L("priority"), L("priority"), L("important"), L("important"), L("bonus")]
    w = rubric.initial_weights(clean)
    assert w == [25, 25, 20, 20, 10], f"清晰组合权重应确定:{w}"
    assert rubric.initial_weights(clean) == w, "同输入必须同输出（确定性）"
    assert sum(w) == 100 and all(x % 5 == 0 and x >= 5 for x in w), f"合计100/5点步进/单项≥5:{w}"
    assert w[4] <= 10, f"附加维度封顶 10:{w}"
    # self_reported 维度封顶 10 分（即使标为优先）。
    sr = [L("priority", "self_reported"), L("priority"), L("important"), L("important"), L("bonus")]
    wsr = rubric.initial_weights(sr)
    assert sum(wsr) == 100 and wsr[0] <= 10, f"self_reported 维度封顶 10:{wsr}"
    # 容量预检:普通8+普通8+附加2=90<100,直接提示 HR 调整而非产出违规权重。
    try:
        rubric.initial_weights([L("priority"), L("important"), L("bonus")])
        assert False, "容量不足应报错"
    except ValueError as e:
        assert "维度" in str(e) or "容量" in str(e) or "100" in str(e), f"容量报错文案不清晰:{e}"

    # Step 1 §7.2:self_reported 维度证据系数封顶 0.5(即使 confirmed);resume_evidence confirmed=1.0。
    async def fake_two_axis(messages, model, format=None):
        return {"content": '{"items":['
            '{"dimension_id":"sr","evidence_status":"confirmed","note":"自述","evidence":["我精通谈判"],"transferable":false},'
            '{"dimension_id":"re","evidence_status":"confirmed","note":"有据","evidence":["主导数据平台建设"],"transferable":false}'
            ']}' }
    two_axis = {"high_priority_threshold": 75, "dimensions": [
        {"id": "sr", "name": "沟通", "jd_requirement": "沟通", "importance_level": "important", "assessment_mode": "self_reported", "weight": 10},
        {"id": "re", "name": "平台", "jd_requirement": "平台", "importance_level": "priority", "assessment_mode": "resume_evidence", "weight": 90},
    ]}
    rubric.rag.chat_raw = fake_two_axis
    try:
        r2 = asyncio.run(rubric.evaluate_candidate({"years": 5, "skills": "x"}, "我精通谈判。主导数据平台建设。", two_axis, "qwen"))
    finally:
        rubric.rag.chat_raw = old_chat
    assert r2["direct_fit_score"] == 95, f"自述封顶半档:10×0.5 + 90×1.0 = 95:{r2}"
    assert r2["coverage"] == 100, f"两维都取证覆盖率 100:{r2}"

    # §7.4:优先自述维度考核点待核验 → 强制必须复核(即使分数达阈值);重要自述不强制。
    async def fake_pri_sr(messages, model, format=None):
        return {"content": '{"items":['
            '{"dimension_id":"a","evidence_status":"confirmed","note":"自述","evidence":["我擅长带队"],"transferable":false},'
            '{"dimension_id":"b","evidence_status":"confirmed","note":"有据","evidence":["交付三个项目"],"transferable":false}'
            ']}' }
    pri_sr = {"high_priority_threshold": 50, "dimensions": [
        {"id": "a", "name": "带队", "jd_requirement": "带队", "importance_level": "priority", "assessment_mode": "self_reported", "weight": 10},
        {"id": "b", "name": "交付", "jd_requirement": "交付", "importance_level": "priority", "assessment_mode": "resume_evidence", "weight": 90},
    ]}
    rubric.rag.chat_raw = fake_pri_sr
    try:
        rp = asyncio.run(rubric.evaluate_candidate({"years": 5}, "我擅长带队。交付三个项目。", pri_sr, "qwen"))
    finally:
        rubric.rag.chat_raw = old_chat
    assert rp["queue"] == "必须复核", f"优先自述维度强制复核:{rp}"

    # §7.4 反向:可迁移证据出现在"重要"维度只作面试提醒,不强制改队列(否则整列都被染成必须复核)。
    async def fake_imp_transfer(messages, model, format=None):
        return {"content": '{"items":['
            '{"dimension_id":"a","evidence_status":"confirmed","note":"有据","evidence":["交付三个项目"],"transferable":false},'
            '{"dimension_id":"b","evidence_status":"confirmed","note":"相关","evidence":["带过跨部门项目"],"transferable":true}'
            ']}' }
    imp_t = {"high_priority_threshold": 50, "dimensions": [
        {"id": "a", "name": "交付", "jd_requirement": "交付", "importance_level": "priority", "assessment_mode": "resume_evidence", "weight": 70},
        {"id": "b", "name": "协作", "jd_requirement": "协作", "importance_level": "important", "assessment_mode": "resume_evidence", "weight": 30},
    ]}
    rubric.rag.chat_raw = fake_imp_transfer
    try:
        ri = asyncio.run(rubric.evaluate_candidate({"years": 5}, "交付三个项目。带过跨部门项目。", imp_t, "qwen"))
    finally:
        rubric.rag.chat_raw = old_chat
    assert ri["queue"] != "必须复核", f"重要维度的可迁移证据不强制复核:{ri}"

    # B-1 §8/§12.2:定向重试只重跑 needs_review 维度,成功维度原样保留,attempt_count 递增。
    retry_rubric = {"high_priority_threshold": 50, "dimensions": [
        {"id": "ok", "name": "工程能力", "jd_requirement": "工程能力", "importance_level": "priority", "assessment_mode": "resume_evidence", "weight": 60},
        {"id": "bad", "name": "数据平台", "jd_requirement": "数据平台经验", "importance_level": "important", "assessment_mode": "resume_evidence", "weight": 40},
    ]}
    prev_items = [
        {"dimension_id": "ok", "name": "工程能力", "weight": 60, "evidence_status": "confirmed", "evidence_level": 4, "evidence_coefficient": 1.0, "importance_level": "priority", "assessment_mode": "resume_evidence", "note": "", "evidence": ["主导后端平台"], "transferable": False, "checkpoint_status": "not_applicable", "failure_code": None, "attempt_count": 1},
        {"dimension_id": "bad", "name": "数据平台", "weight": 40, "evidence_status": "needs_review", "evidence_level": 0, "evidence_coefficient": 0.0, "importance_level": "important", "assessment_mode": "resume_evidence", "note": "", "evidence": [], "transferable": False, "checkpoint_status": "pending", "failure_code": "evidence_not_in_resume", "attempt_count": 1},
    ]
    seen = {}
    async def fake_retry(messages, model, format=None):
        seen["m"] = messages[-1]["content"]
        return {"content": '{"items":[{"dimension_id":"bad","evidence_status":"confirmed","note":"重试命中","evidence":["做过 5 年数据平台"],"transferable":false}]}'}
    rubric.rag.chat_raw = fake_retry
    try:
        retried = asyncio.run(rubric.retry_failed_dimensions({"years": 5}, "主导后端平台。做过 5 年数据平台。", retry_rubric, prev_items, "qwen"))
    finally:
        rubric.rag.chat_raw = old_chat
    assert "数据平台" in seen["m"] and "工程能力" not in seen["m"], f"重试只把失败维度送进模型(不重算成功维度):{seen['m']}"
    rb = {it["dimension_id"]: it for it in retried["items"]}
    assert rb["ok"]["evidence_status"] == "confirmed" and rb["ok"]["attempt_count"] == 1, f"成功维度原样保留:{rb['ok']}"
    assert rb["bad"]["evidence_status"] == "confirmed" and rb["bad"]["attempt_count"] == 2, f"失败维度重试命中且 attempt+1:{rb['bad']}"
    assert retried["direct_fit_score"] == 100 and retried["coverage"] == 100, f"重试后全维取证:{retried}"

    # B-2 §7.4:优先自述维度考核点标注后,只重算队列不回写分;verified/not_applicable 解除强制复核,concern 仍需复核。
    cp_items = [
        {"importance_level": "priority", "assessment_mode": "self_reported", "evidence_status": "confirmed", "transferable": False, "checkpoint_status": "pending"},
        {"importance_level": "priority", "assessment_mode": "resume_evidence", "evidence_status": "confirmed", "transferable": False, "checkpoint_status": "not_applicable"},
    ]
    assert rubric.recompute_queue(cp_items, 90, 100, 50) == "必须复核", "考核点 pending 时仍必须复核"
    cp_items[0]["checkpoint_status"] = "verified"
    assert rubric.recompute_queue(cp_items, 90, 100, 50) == "优先评估", "考核点 verified 后回到优先评估"
    cp_items[0]["checkpoint_status"] = "concern"
    assert rubric.recompute_queue(cp_items, 90, 100, 50) == "必须复核", "concern 仍需复核"

    print("rubric 自检通过 ✅(脱敏视图 + 受保护 JD + 固定公式 + 复核队列 + 证据归属 + 局部失败恢复 + 5点权重算法 + 双轴系数 + 定向重试 + 考核点)")


if __name__ == "__main__":
    main()
