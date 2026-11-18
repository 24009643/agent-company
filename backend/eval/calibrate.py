"""MIN_SCORE 校准:对 golden 全量用例跑检索(直接调用 store,不经 LLM,秒级),
扫描候选阈值,报告每个阈值下 answer/refuse/forbidden 三类的通过率。
用法: .venv/bin/python eval/calibrate.py
retrieval 逻辑(chunk 切法、hybrid 权重等)改了之后,重跑一遍看 MIN_SCORE 是否还成立。

判定逻辑直接调用 store.evidence_gate 本体——画线的规则和守门的规则永远同一份,
闸门将来怎么改(kw 门槛、IDF 加权),这里自动跟着变,校准结果不会骗人。
"""
import os, sys, json, asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store, rag
from config import role_depts, TOP_K

HERE = os.path.dirname(os.path.abspath(__file__))
THRESHOLDS = [0.35, 0.40, 0.45, 0.50, 0.55, 0.58, 0.60, 0.62, 0.65]


async def main():
    store.init()
    cases = [json.loads(l) for l in open(os.path.join(HERE, "golden.jsonl")) if l.strip()]
    cases = [c for c in cases if c["type"] != "multiturn" and not c.get("skip")
             and not c.get("inject_history")]
    depts_by_role = {r: role_depts(r) for r in ("admin", "staff", "finance")}

    rows = []  # (id, type, question, hits)
    for c in cases:
        qemb = (await rag.embed([c["question"]]))[0]
        hits = store.hybrid_search(qemb, c["question"], TOP_K, depts_by_role[c["role"]])
        rows.append((c["id"], c["type"], c["question"], hits))

    print(f"{'id':6} {'type':10} {'top_score':10}")
    for cid, typ, q, hits in sorted(rows, key=lambda x: (x[1], -max((h["score"] or 0 for h in x[3]), default=0))):
        top = max((h["score"] or 0 for h in hits), default=0)
        print(f"{cid:6} {typ:10} {round(top, 3):<10}")

    print("\n===== 阈值扫描(直接调用 store.evidence_gate) =====")
    for thresh in THRESHOLDS:
        ok = {"answer": 0, "refuse": 0, "forbidden": 0}
        tot = {"answer": 0, "refuse": 0, "forbidden": 0}
        for cid, typ, q, hits in rows:
            tot[typ] += 1
            # 用通用域配置、只扫 min_score 维度(boundary 关闭以复刻旧的二值 direct/refuse 判定)
            mode, ph = store.evidence_gate(hits, q, {"min_score": thresh, "boundary_answer": False,
                                                     "extra_stopwords": []})
            passed = mode == "direct"
            if passed == (typ == "answer"):
                ok[typ] += 1
        print(f"MIN_SCORE={thresh:.2f}  answer {ok['answer']}/{tot['answer']}  "
              f"refuse {ok['refuse']}/{tot['refuse']}  forbidden {ok['forbidden']}/{tot['forbidden']}")


if __name__ == "__main__":
    asyncio.run(main())
