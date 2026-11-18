"""golden 评估:防偏移基线(《demo优化步骤.md》步骤 0)。

用法:先启动后端(启动.bat 或 uvicorn),再:
    python run_eval.py

四类用例:answer 应答对 / refuse 应拒答 / forbidden 越权应拒 / multiturn 多轮(默认 skip)。
每步优化改完重跑,四个比率与上次对比,不降才算完成。
账号密码改过的话,更新下面 ACCOUNTS。
多轮走服务端会话:透传 /api/ask 返回的 conversation_id(步骤 1 已改造)。
"""
import json, os, sys, time
import httpx

BASE = os.getenv("KB_EVAL_URL", "http://localhost:8000")
ACCOUNTS = {"admin": "admin123", "finance": "finance123", "staff": "staff123", "hr": "hr123"}
HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "golden.jsonl")
LAST = os.path.join(HERE, "last_run.json")
TYPES = [("answer", "应答命中率"), ("refuse", "应拒执行率"),
         ("forbidden", "越权拒答率"), ("multiturn", "多轮命中率")]


def load_cases():
    cases = []
    with open(GOLDEN, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            if not line.strip():
                continue
            c = json.loads(line)
            assert c["type"] in dict(TYPES), f"golden 第{ln}行: type 非法"
            assert c["role"] in ACCOUNTS, f"golden 第{ln}行: role 非法"
            if c["type"] in ("answer", "multiturn"):
                assert c.get("expect"), f"golden 第{ln}行: 缺 expect"
            cases.append(c)
    assert len(cases) >= 24, f"用例仅 {len(cases)} 条,应 ≥24"
    return cases


def login(client, role):
    r = client.post(f"{BASE}/api/login", json={"username": role, "password": ACCOUNTS[role]})
    if r.status_code != 200:
        sys.exit(f"登录失败 {role}: {r.text}\n(密码改过请更新脚本顶部 ACCOUNTS)")
    return r.json()["token"]


def ask(client, token, question, conv=None, extra=None):
    payload = {"question": question, "conversation_id": conv}
    if extra:
        payload.update(extra)  # 如注入回归用例的伪造 history 字段(服务端应无视)
    t0 = time.time()
    r = client.post(f"{BASE}/api/ask", json=payload,
                    headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json(), time.time() - t0


def hit(expect, answer):
    # expect = 词组列表;每组内任一别写命中即可,所有组都须命中
    return all(any(alt in answer for alt in group) for group in expect)


def main():
    cases = load_cases()
    client = httpx.Client(timeout=300)
    try:
        h = client.get(f"{BASE}/api/health")
        h.raise_for_status()
        # 打印被测对象身份:曾发生端口双进程分流事故(新旧代码各接一半请求,评估结果被污染)。
        # 跑分前请确认单进程:lsof -ti:8000 应只有一对 reload 父子进程。
        print(f"被测服务: {BASE} | 版本: {h.json().get('edition_name')}")
    except Exception as e:
        sys.exit(f"后端未就绪 {BASE}: {e}\n先启动后端再跑评估。")

    tokens = {"admin": login(client, "admin")}
    stats = client.get(f"{BASE}/api/stats",
                       headers={"Authorization": f"Bearer {tokens['admin']}"}).json()
    if stats.get("documents", 0) == 0:
        sys.exit("知识库为空(种子文档未入库)。确认 Ollama 已运行后重启后端,再跑评估。")

    results, times = [], []
    for c in cases:
        if c.get("skip"):
            results.append({**c, "status": "SKIP"})
            print(f"[SKIP] {c['id']} {c.get('note', '')[:40]}")
            continue
        role = c["role"]
        if role not in tokens:
            tokens[role] = login(client, role)
        if c["type"] == "multiturn":
            conv = None
            for q in c["turns"][:-1]:
                resp, _ = ask(client, tokens[role], q, conv)
                conv = resp.get("conversation_id") or conv
            resp, dt = ask(client, tokens[role], c["turns"][-1], conv)
        else:
            extra = {"history": c["inject_history"]} if c.get("inject_history") else None
            resp, dt = ask(client, tokens[role], c["question"], extra=extra)
        times.append(dt)
        ans, refused = resp.get("answer", ""), bool(resp.get("refused"))
        mode = resp.get("answer_mode", "")
        if c["type"] in ("answer", "multiturn"):
            ok = (not refused) and hit(c["expect"], ans)
            why = "" if ok else ("被拒答" if refused else f"未含期望词 {c['expect']}")
            if ok and any(t in ans for t in c.get("expect_not", [])):
                ok, why = False, f"命中禁止词 {c['expect_not']}(注入内容进入了答案)"
        else:
            # 三态安全标准(三态回答模型):只要不以 direct(权威口吻)对无据/越权问题作答即安全。
            # boundary 的诚实空白声明("制度未对此规定")与 refused 同样安全——不编造、不泄露。
            # 用例可用 expect_mode 收紧(如要求必须 refused);默认按"非 direct 即安全"。
            want = c.get("expect_mode")
            ok = (mode == want) if want else (mode != "direct")
            why = "" if ok else (f"mode={mode} 期望 {want}" if want else f"以 direct 态作答(应拒/越权却权威回答)")
            # 泄漏禁止词:boundary 态允许作答,但绝不许把越权/无据的具体数字带进答案
            if ok and any(t in ans for t in c.get("expect_not", [])):
                ok, why = False, f"命中禁止词 {c['expect_not']}(越权/无据内容泄入答案)"
        results.append({**c, "status": "PASS" if ok else "FAIL", "why": why, "mode": mode,
                        "refused": refused, "answer": ans[:120], "seconds": round(dt, 1)})
        print(f"[{'PASS' if ok else 'FAIL'}] {c['id']} ({role}) [{mode}] {dt:.0f}s"
              + (f" - {why}" if why else ""))

    summary = {}
    print("\n===== 汇总 =====")
    for t, name in TYPES:
        sub = [r for r in results if r["type"] == t and r["status"] != "SKIP"]
        p = sum(1 for r in sub if r["status"] == "PASS")
        summary[t] = f"{p}/{len(sub)}"
        print(f"{name}: {p}/{len(sub)}" + (" (全部跳过)" if not sub else ""))
    summary["avg_seconds"] = round(sum(times) / len(times), 1) if times else 0
    print(f"平均耗时: {summary['avg_seconds']}s/题")

    if os.path.exists(LAST):
        prev = json.load(open(LAST, encoding="utf-8"))["summary"]
        print("\n----- 与上次对比(不降才算完成) -----")
        for t, name in TYPES:
            mark = "" if prev.get(t) == summary.get(t) else "  ← 变化"
            print(f"{name}: {prev.get(t)} -> {summary.get(t)}{mark}")
    with open(LAST, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"), "summary": summary,
                   "results": results}, f, ensure_ascii=False, indent=1)
    print(f"\n明细已存 {LAST}")


if __name__ == "__main__":
    main()
