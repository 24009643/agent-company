"""Step 2.1 立墙自检:talent_store 物理分库 + schema 公平性 + 通用审计 + 墙完整性。

运行:python test_talent_store.py
被测对象唯一(临时库,不碰 data/)。RED 阶段应因缺 talent_store 模块而失败。
"""
import os, tempfile, sqlite3


def main():
    db = os.path.join(tempfile.mkdtemp(), "talent.sqlite")
    import talent_store as T
    T.init(db)

    # 1) 独立物理文件:talent.sqlite 自成一个文件(I1 物理分库)
    assert os.path.exists(db), "talent.sqlite 未创建"

    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # 2) 三表齐备
    assert {"candidates", "resumes", "audit"} <= tables, f"缺表:{tables}"

    # 3) 🟡Y1 公平性结构层:candidates 不得含性别/年龄/照片/婚育/籍贯字段
    cols = {r[1] for r in con.execute("PRAGMA table_info(candidates)")}
    banned = {"gender", "sex", "age", "birth", "photo", "marital", "marriage", "hometown",
              "性别", "年龄", "照片", "婚育", "籍贯"}
    assert not (cols & banned), f"schema 混入不公平字段:{cols & banned}"

    # 4) 建草稿候选人(actor 落审计)→ 默认状态'草稿' → 可读回(2.2 摄取确认后才流转)
    cid = T.create_candidate(name="张三", phone="13800000000", email="z@x.com",
                             years=3, skills="Python", education="本科",
                             expected_salary="20-25k", source_file="zhangsan.pdf", actor="hr")
    c = T.get_candidate(cid)
    assert c and c["name"] == "张三" and c["status"] == "草稿", f"读回异常:{c}"

    # 5) 状态机是允许迁移图,不是任意枚举:草稿不得跳 offer,只能经确认转待筛
    assert T.transition_candidate(cid, "offer", "hr", "跳过流程") is False
    assert T.confirm_candidate(cid, actor="hr", reason="已核对原文", phone="13900000000") is True
    assert T.get_candidate(cid)["status"] == "待筛"
    assert T.transition_candidate(cid, "约面", "hr", "安排首轮面试") is True
    assert T.get_candidate(cid)["status"] == "约面"

    # 6) 通用审计(🟡Y6):create、确认、迁移都落一行且记录可复原的前后状态/理由
    aud = con.execute("SELECT actor, action, target, payload FROM audit").fetchall()
    assert any(r[:3] == ("hr", "create", cid) for r in aud), f"缺 create 审计:{aud}"
    move = [r for r in aud if r[1] == "transition_status" and r[2] == cid]
    assert move and '"from": "待筛"' in move[0][3] and '"to": "约面"' in move[0][3], f"状态审计不可复原:{move}"

    # 7) 列表仅返回最小化摘要,支持分页;联系方式只允许详情端点获取
    assert any(x["id"] == cid for x in T.list_candidates()), "list_candidates 未含新建候选人"
    summary = T.list_candidate_summaries(limit=1, offset=0)
    assert summary["total"] == 1 and len(summary["candidates"]) == 1, f"分页异常:{summary}"
    assert "phone" not in summary["candidates"][0] and "email" not in summary["candidates"][0], f"列表泄露 PII:{summary}"

    # 8) resume 原文/解析文本存取(2.2 摄取用)
    T.save_resume(cid, raw_text="张三 简历原文若干", parsed_text='{"basics":{"name":"张三"}}')
    rz = T.get_resume(cid)
    assert rz and rz["raw_text"].startswith("张三") and "张三" in (rz["parsed_text"] or ""), f"resume 回读异常:{rz}"

    # 9) 草稿才可硬删:删除必须清掉岗位关联,其他状态只能归档
    jid = T.create_job("数据工程师", "Python", "Python", "hr")
    draft = T.create_candidate("待退回草稿", actor="hr")
    T.attach_candidate(jid, draft, "hr")
    # 即使历史异常留下派生评分，草稿删除也必须一起清理，不能留下可被误读的孤儿记录。
    T.save_resume(draft, raw_text="待退回草稿 Python", parsed_text=None)
    T.save_evaluation(draft, jid, "test-rubric", "fingerprint",
                      {"status": "unassessed", "direct_fit_score": None, "queue": "未评估", "items": []},
                      "test-model", "hr")
    assert T.delete_draft_candidate(draft, "hr") is True
    assert draft not in T.get_job(jid)["candidate_ids"], "删草稿后不应留下岗位悬空 ID"
    assert con.execute("SELECT COUNT(*) FROM resumes WHERE candidate_id=?", (draft,)).fetchone()[0] == 0, "删草稿后不得残留简历"
    assert con.execute("SELECT COUNT(*) FROM candidate_evaluations WHERE candidate_id=?", (draft,)).fetchone()[0] == 0, "删草稿后不得残留评分"
    assert T.delete_draft_candidate(cid, "hr") is False, "非草稿不得硬删"
    assert T.archive_candidate(cid, "hr", "招聘暂停") is True
    assert T.get_candidate(cid)["status"] == "归档", "归档迁移失败"

    # 11) Step 1 §12.1:v2 评分维度字段(双轴/来源/理由/面试探针)与冻结版本指纹/招聘重点端到端持久化。
    v2job = T.create_job("AIPM", "懂 SQL 与产品", "懂 SQL 与产品", "hr")
    v2dims = [
        {"name": "SQL 分析", "jd_requirement": "用 SQL 分析经营数据", "kind": "核心", "weight": 60,
         "importance_level": "priority", "assessment_mode": "resume_evidence", "group_name": "岗位专业能力",
         "source_type": "jd_explicit", "source_quote": "懂 SQL", "importance_reason": "直接决定岗位产出",
         "rank": 1, "interview_probe": "现场解释一次经营异常归因", "confidence": "高", "evidence_rule": "简历有 SQL 项目"},
        {"name": "跨部门沟通", "jd_requirement": "跨部门推动", "kind": "重要", "weight": 40,
         "importance_level": "important", "assessment_mode": "self_reported", "group_name": "协作沟通与组织影响",
         "source_type": "hiring_focus", "importance_reason": "本轮团队缺协调", "rank": 1,
         "interview_probe": "举一个推动跨部门的例子", "confidence": "中", "evidence_rule": "简历有跨部门职责"},
    ]
    rid = T.create_rubric_draft(v2job, 75, v2dims, "hr_editor", jd_fingerprint="fp-abc", hiring_focus="更偏数据分析")
    got = T.get_rubric(rid)
    assert got["jd_fingerprint"] == "fp-abc" and got["hiring_focus"] == "更偏数据分析", f"冻结版本应存指纹与招聘重点:{got}"
    d0 = got["dimensions"][0]
    assert d0["importance_level"] == "priority" and d0["assessment_mode"] == "resume_evidence", f"双轴字段未持久化:{d0}"
    assert d0["source_type"] == "jd_explicit" and d0["interview_probe"] and d0["importance_reason"], f"来源/探针/理由未持久化:{d0}"
    # 覆盖率与逐维失败码随评分持久化(§7.3/§8):局部失败可定位到具体维度。
    T.save_evaluation("cand-x", v2job, rid, "fp1",
                      {"status": "partial", "direct_fit_score": 60, "queue": "必须复核", "coverage": 60,
                       "items": [{"dimension_id": d0["id"], "name": "SQL 分析", "evidence_status": "needs_review",
                                  "evidence_level": 0, "evidence_coefficient": 0.0, "importance_level": "priority",
                                  "assessment_mode": "resume_evidence", "note": "", "evidence": [],
                                  "transferable": False, "interview_checkpoint_pending": True}]},
                      "qwen", "hr")
    ev = T.latest_evaluation("cand-x", v2job)
    assert ev["coverage"] == 60, f"覆盖率未持久化:{ev}"
    assert ev["items"][0]["evidence_coefficient"] == 0.0 and ev["items"][0]["failure_code"], f"逐维系数/失败码未持久化:{ev['items'][0]}"

    # 12) Step 1 §6.3:冻结硬校验——维度数/优先数/5点步进/单项上下限/附加合计/合计100/证据规则/无受保护;
    #     §12.2:草稿层宽松(字段级),冻结层才跑完整硬条件。
    def _vdim(name, imp, w, mode="resume_evidence", rule="简历有对应经历", jd="要求"):
        return {"name": name, "jd_requirement": jd, "importance_level": imp, "assessment_mode": mode, "weight": w, "evidence_rule": rule}
    good5 = [_vdim("A", "priority", 30), _vdim("B", "priority", 25), _vdim("C", "important", 20),
             _vdim("D", "important", 15), _vdim("E", "bonus", 10, mode="self_reported")]
    grid = T.create_rubric_draft(T.create_job("岗位X", "jd", "jd", "hr"), 75, good5, "hr_editor")
    assert T.freeze_rubric(grid, "hr_editor") is True, "合法 5 维 v2 规则应可冻结"

    def _expect_freeze_fail(dims, why):
        try:
            r = T.create_rubric_draft(T.create_job("岗位-" + why, "jd", "jd", "hr"), 75, dims, "hr_editor")
        except ValueError:
            return  # 字段级即被拦也算拦住
        try:
            T.freeze_rubric(r, "hr_editor")
        except ValueError:
            return
        assert False, f"冻结应因『{why}』被拒"
    _expect_freeze_fail(good5[:4] + [_vdim("F", "priority", 30)], "合计不为100")       # 30+25+20+15+30=120
    _expect_freeze_fail([_vdim("A", "priority", 50), _vdim("B", "important", 50)], "维度过少")
    _expect_freeze_fail([_vdim("A", "priority", 20), _vdim("B", "priority", 20), _vdim("C", "priority", 20),
                         _vdim("D", "priority", 20), _vdim("E", "important", 20)], "优先过多")
    _expect_freeze_fail([_vdim("A", "priority", 30), _vdim("B", "priority", 25), _vdim("C", "important", 20),
                         _vdim("D", "important", 10), _vdim("E", "important", 15, mode="self_reported")], "自述超10分")
    no_rule = [dict(d) for d in good5]; no_rule[2] = _vdim("C", "important", 20, rule="")
    _expect_freeze_fail(no_rule, "缺证据规则")
    prot = [dict(d) for d in good5]; prot[0] = _vdim("年龄", "priority", 30, jd="年龄 30 岁以下")
    _expect_freeze_fail(prot, "受保护属性")
    # 草稿层不强制维度数:2 维草稿可保存,只是不能冻结。
    drid = T.create_rubric_draft(T.create_job("草稿岗位", "jd", "jd", "hr"), 75,
                                 [_vdim("A", "priority", 50), _vdim("B", "important", 50)], "hr_editor")
    assert T.get_rubric(drid)["status"] == "草稿", "2 维草稿应可保存(字段合法)"

    # 13) Step 1 §13:v1→v2 复制迁移——kind-only 规则可冻结(kind 回填 importance_level),冻结版复制为新草稿,原版只读。
    v1style = [{"name": "A", "jd_requirement": "x", "kind": "核心", "weight": 30, "evidence_rule": "r"},
               {"name": "B", "jd_requirement": "x", "kind": "核心", "weight": 25, "evidence_rule": "r"},
               {"name": "C", "jd_requirement": "x", "kind": "重要", "weight": 20, "evidence_rule": "r"},
               {"name": "D", "jd_requirement": "x", "kind": "重要", "weight": 15, "evidence_rule": "r"},
               {"name": "E", "jd_requirement": "x", "kind": "加分", "weight": 10, "evidence_rule": "r"}]
    v1rid = T.create_rubric_draft(T.create_job("v1岗位", "jd", "jd", "hr"), 75, v1style, "hr_editor")
    assert T.freeze_rubric(v1rid, "hr_editor") is True, "kind-only 规则应能冻结(importance_level 由 kind 回填)"
    v1frozen = T.get_rubric(v1rid)
    assert v1frozen["dimensions"][0]["importance_level"] == "priority", "核心应回填为 priority"
    copy_id = T.copy_rubric_as_draft(v1rid, "hr_editor")
    cp = T.get_rubric(copy_id)
    assert cp["status"] == "草稿" and cp["version"] > v1frozen["version"], f"复制应产出自增版本的新草稿:{cp}"
    assert cp["dimensions"][0]["importance_level"] == "priority", "复制后双轴字段应保留"
    assert T.get_rubric(v1rid)["status"] == "已冻结", "原冻结版必须只读不变"

    # 10) I1 墙的完整性:检索层源码永不 import 人才库(过滤会有 bug,不在场没有 bug)
    here = os.path.dirname(os.path.abspath(__file__))
    for f in ("rag.py", "store.py"):
        src = open(os.path.join(here, f), encoding="utf-8").read()
        assert "talent_store" not in src and "talent.sqlite" not in src, f"{f} 触到人才库,墙破了"

    # ===== Step 2.4 群发: send_batches / send_items =====
    bid = T.create_send_batch("hr", "约面", "job1", "周五 14:00", "3F 会议室", "")
    b = T.get_send_batch(bid)
    assert b and b["status"] == "草稿" and b["mail_type"] == "约面" and b["items"] == []
    iid = T.add_send_item(bid, {"candidate_id": "c1", "to_name": "张三", "to_email": "z@x.com",
        "subject": "s", "body": "b", "material_id": "skill:0", "material_value": "Python",
        "material_source": "intake:skills", "wording": "llm", "template_version": "约面v1",
        "machine_checks": "{}", "status": "pending"})
    b = T.get_send_batch(bid)
    assert len(b["items"]) == 1 and b["items"][0]["to_email"] == "z@x.com"
    # 防重:sent/exported 算发过,pending 不算
    assert T.has_prior_send("c1", "job1", "约面") is False
    T.set_send_item(iid, "hr", status="sent", sent_at=1.0)
    assert T.has_prior_send("c1", "job1", "约面") is True
    assert T.has_prior_send("c1", "job1", "拒信") is False
    T.set_batch_status(bid, "完成", "hr", confirmed_by="hr")
    b2 = T.get_send_batch(bid)
    assert b2["status"] == "完成" and b2["confirmed_by"] == "hr" and b2["confirmed_at"]
    # 验证批状态写入落了审计
    aud = con.execute("SELECT action FROM audit ORDER BY ts DESC LIMIT 50").fetchall()
    assert any(r[0] == "set_batch_status" for r in aud), f"缺 set_batch_status 审计:{aud}"
    # 列白名单防注入:未知列直接拒
    try:
        T.set_send_item(iid, "hr", evil="x"); assert False, "未知列应拒"
    except (KeyError, ValueError):
        pass
    # add_send_item 也要拒未知列
    try:
        T.add_send_item(bid, {"evil": "x"}); assert False, "add_send_item 未知列应拒"
    except (KeyError, ValueError):
        pass
    # 审计落了 create_batch
    aud = con.execute("SELECT action FROM audit ORDER BY ts DESC LIMIT 50").fetchall()
    assert any(r[0] == "create_batch" for r in aud), f"缺 create_batch 审计:{aud}"
    # ===== 🔴 fix:CAS 领取(防并发双发)+ has_prior 计 'sending'(🟡A)=====
    # claim_batch:仅当 expect 匹配时原子转移(bid 现为"完成")
    assert T.claim_batch(bid, "完成", "部分失败", "hr") is True, "expect 匹配应赢得转移"
    assert T.claim_batch(bid, "完成", "已确认", "hr") is False, "expect 不匹配(已被上一步改)应失败=防并发双确认"
    assert T.get_send_batch(bid)["status"] == "部分失败"
    # claim_send_item:原子领取,第二次(状态已变)领不到
    assert T.claim_send_item(iid, "sent", "sending") is True, "expect 匹配应领取成功"
    assert T.claim_send_item(iid, "sent", "sending") is False, "已被领取(现 'sending')再领应失败=防双发同一封"
    # 🟡A:'sending'(已领取待发 / 已发但 sent 未落库)须计入 has_prior_send,防经新批重发
    assert T.has_prior_send("c1", "job1", "约面") is True, "'sending' 须计入 has_prior_send"
    print("send tables OK")

    con.close()
    print("talent_store 自检通过 ✅")


if __name__ == "__main__":
    main()
