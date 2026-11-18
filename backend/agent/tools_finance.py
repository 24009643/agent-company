"""财务域四工具(定稿:财务 v1):纯函数 + JSON schema,共用一份政策模块。

政策模块 = "已确认政策库"的手工维护形态——抽取→确认管线属摄取板块(记债),此处只读。
L2 的意义(定稿铁律 2):金额只能来自确定性代码,LLM 只有"调哪个工具、传什么参"的选择权,
参数还要过 validate_args 容器(schema+值域),坏参数不执行、错误喂回模型自纠。
自检:python agent/tools_finance.py
"""
import json

# ponytail: 城市→档位是校准旋钮,一线名单/额度随《差旅与报销制度》修订同步维护
TIER1 = {"北京", "上海", "广州", "深圳"}
HOTEL_CAP = {"一线": 600, "其他": 400}   # 元/晚
MEAL_PER_DAY = 100                        # 元/人/天
BASIS = "《差旅与报销制度》"


def city_tier(city):
    c = (city or "").strip().replace("市", "")
    return "一线" if c in TIER1 else "其他"


def calc_budget(city, nights, days):
    """报销额度计算:住宿上限×晚数 + 餐补×天数,带完整分项(分项数字都进 L6 白名单)。"""
    tier = city_tier(city)
    cap = HOTEL_CAP[tier]
    hotel, meal = cap * int(nights), MEAL_PER_DAY * int(days)
    return {"城市": city, "档位": tier, "住宿上限每晚": cap, "晚数": int(nights), "住宿小计": hotel,
            "餐补每天": MEAL_PER_DAY, "天数": int(days), "餐补小计": meal,
            "预算上限合计": hotel + meal, "依据": BASIS}


def trip_days(start, end):
    """出差天数/晚数推算(ISO 日期,含首尾天)。"""
    from datetime import date
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if e < s:
        raise ValueError("结束日期早于开始日期")
    days = (e - s).days + 1
    return {"开始": start, "结束": end, "天数": days, "晚数": max(days - 1, 0)}


def check_claim(city, nightly_price):
    """限额合规校验:单晚房价是否超所在档位上限。"""
    tier = city_tier(city)
    cap = HOTEL_CAP[tier]
    over = max(0, round(float(nightly_price) - cap, 2))
    return {"城市": city, "档位": tier, "住宿上限每晚": cap, "单价": nightly_price,
            "合规": over == 0, "每晚超出": over, "依据": BASIS}


def prefill_claim(city, nights, days, nightly_price):
    """报销单结构化预填:超标部分按上限截断并注明自理(定稿契约的 structured 输出)。"""
    b = calc_budget(city, nights, days)
    cap, price = b["住宿上限每晚"], float(nightly_price)
    hotel_claim = round(min(price, cap) * int(nights), 2)
    over_total = round(max(0.0, price - cap) * int(nights), 2)
    rows = [
        {"项目": "住宿", "金额": hotel_claim,
         "政策依据": f"{b['档位']}城市≤{cap}元/晚({BASIS})",
         "状态": "合规" if over_total == 0 else f"单价超标,按上限计报,超出 {over_total} 元自理"},
        {"项目": "餐补", "金额": b["餐补小计"],
         "政策依据": f"{MEAL_PER_DAY}元/人/天({BASIS})", "状态": "合规"},
    ]
    return {"明细": rows, "可报合计": round(hotel_claim + b["餐补小计"], 2), "依据": BASIS}


# ---- JSON schema(Ollama/OpenAI function-calling 格式)----
def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}

_CITY = {"type": "string", "description": "城市名,如 北京/成都"}
_INT = {"type": "integer"}
SCHEMAS = [
    _fn("calc_budget", "按制度计算出差预算上限:住宿上限×晚数+餐补×天数",
        {"city": _CITY, "nights": {**_INT, "description": "住宿晚数"},
         "days": {**_INT, "description": "餐补天数"}}, ["city", "nights", "days"]),
    _fn("trip_days", "按开始/结束日期(YYYY-MM-DD)推算出差天数与晚数",
        {"start": {"type": "string"}, "end": {"type": "string"}}, ["start", "end"]),
    _fn("check_claim", "校验某城市单晚房价是否超过制度上限,并给出超出金额",
        {"city": _CITY, "nightly_price": {"type": "number", "description": "单晚房价(元)"}},
        ["city", "nightly_price"]),
    _fn("prefill_claim", "生成报销单结构化预填明细(超标部分按上限截断)",
        {"city": _CITY, "nights": _INT, "days": _INT,
         "nightly_price": {"type": "number"}}, ["city", "nights", "days", "nightly_price"]),
]

TOOLS = {"calc_budget": calc_budget, "trip_days": trip_days,
         "check_claim": check_claim, "prefill_claim": prefill_claim}


def validate_args(name, args):
    """L2 容器:白名单 + 类型/值域校验。坏参数不执行,错误信息喂回模型自纠。"""
    if name not in TOOLS:
        return f"未知工具 {name}"
    a = args or {}
    for k in ("nights", "days"):
        if k in a:
            try:
                v = int(a[k])
            except (TypeError, ValueError):
                return f"{k} 必须是整数"
            if not (1 <= v <= 365):
                return f"{k} 超出合理范围(1-365)"
            a[k] = v
    if "nightly_price" in a:
        try:
            v = float(a["nightly_price"])
        except (TypeError, ValueError):
            return "nightly_price 必须是数字"
        if not (0 <= v <= 100000):
            return "nightly_price 超出合理范围"
    if "city" in a and not (isinstance(a["city"], str) and 0 < len(a["city"].strip()) <= 20):
        return "city 必须是 1-20 字的城市名"
    return None


def run_tool(name, args):
    """执行入口:校验→执行→结构化返回;任何异常都变成 error 字段,不打断问答链路。"""
    err = validate_args(name, args)
    if err:
        return {"error": f"参数不合法:{err}"}
    try:
        return TOOLS[name](**args)
    except Exception as e:
        return {"error": f"工具执行失败:{e}"}


if __name__ == "__main__":
    assert city_tier("北京") == "一线" and city_tier("成都市") == "其他"
    assert calc_budget("成都", 3, 3)["预算上限合计"] == 1500      # 400×3 + 100×3
    assert calc_budget("北京", 2, 2)["预算上限合计"] == 1400      # 600×2 + 100×2
    assert trip_days("2026-07-01", "2026-07-03") == {"开始": "2026-07-01", "结束": "2026-07-03", "天数": 3, "晚数": 2}
    assert check_claim("上海", 750)["每晚超出"] == 150 and not check_claim("上海", 750)["合规"]
    assert check_claim("成都", 380)["合规"]
    p = prefill_claim("上海", 2, 3, 750)
    assert p["可报合计"] == 1500 and "自理" in p["明细"][0]["状态"]   # min(750,600)×2 + 100×3
    assert "参数不合法" in run_tool("calc_budget", {"city": "成都", "nights": -1, "days": 3})["error"]
    assert "未知工具" in validate_args("rm_rf", {})
    print("tools_finance 自检通过:", json.dumps(calc_budget("成都", 3, 3), ensure_ascii=False))
