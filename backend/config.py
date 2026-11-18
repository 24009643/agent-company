import os

# ===== Ollama(本地私有模型) =====
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")

# 双模型(切换模式):两个都需先 `ollama pull`
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")          # 模型一:千问
LLM_MODEL_2 = os.getenv("LLM_MODEL_2", "deepseek-r1:7b")  # 模型二:DeepSeek
LLM_MODELS = [
    {"id": "qwen", "name": "千问 Qwen", "model": LLM_MODEL},
    {"id": "deepseek", "name": "DeepSeek", "model": LLM_MODEL_2},
]
DEFAULT_MODEL_ID = os.getenv("DEFAULT_MODEL_ID", "qwen")
# 节点级模型旋钮(架构定稿:模型按节点任务配置,非按 agent)。岗位评分节点可单独换更强模型,
# 默认跟随 DEFAULT_MODEL_ID;实测 deepseek-r1:7b 对该节点无增益,留旋钮待更大模型到位再换。
EVAL_MODEL_ID = os.getenv("EVAL_MODEL_ID", DEFAULT_MODEL_ID)

def model_tag(model_id):
    for m in LLM_MODELS:
        if m["id"] == model_id:
            return m["model"]
    return LLM_MODELS[0]["model"]

def model_name(model_id):
    for m in LLM_MODELS:
        if m["id"] == model_id:
            return m["name"]
    return LLM_MODELS[0]["name"]

# ===== 检索参数 =====
TOP_K = int(os.getenv("TOP_K", "4"))
# bge-m3 在小语料上,不相关的公司文档之间也常有 0.4~0.55 的余弦相似度(同域中文措辞导致),
# 0.35 太松会让泛泛相关的段落通过闸门。按 golden 数据校准(见 eval/calibrate 结果):
# 0.60 是两类之间的安全空当——真实命中最低 0.647,最高误报 0.565。
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.60"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "420"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "60"))
DATA_DIR = os.getenv("DATA_DIR", "./data")

# ===== 上传限制(🔴R1b:防 file.read() 无上限的内存 DoS)=====
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# ===== 版本与授权(商业化) =====
# EDITION: basic(基础版) / pro(专业版) / enterprise(企业版)
EDITION = os.getenv("EDITION", "enterprise").lower()
LICENSE_SEATS = int(os.getenv("LICENSE_SEATS", "50"))        # 席位上限(最多注册用户数)
LICENSE_EXPIRES = os.getenv("LICENSE_EXPIRES", "2099-12-31") # 授权有效期(YYYY-MM-DD),过期则禁止登录
LICENSED_TO = os.getenv("LICENSED_TO", "演示企业")

EDITION_NAMES = {"basic": "基础版", "pro": "专业版", "enterprise": "企业版"}

# 各版本解锁的功能 —— 已按《定价方案(优化版)》对齐
# 标志含义:True = 该版本启用且“已实现可用”;False = 未启用,或功能尚在规划中(未开发)。
# 注意:当前真正联动到程序行为的只有一项 —— analytics(数据分析)。
# 其余键(workflow / api / advanced_search / fine_tuning)是按定价表预留的“占位标志”,
# 功能本身尚未开发;等将来做出来后,把对应版本改成 True 即可,无需改动这里的结构。
# 部门隔离(原 dept_isolation)不再是版本特性:安全边界不应是付费项,已在 app.py 里对所有版本无条件启用(风险 #3)。
EDITION_FEATURES = {
    # 基础版:核心问答 / 文档管理 / 全文检索 / 双模型 —— 这些不分版本,所有版本都有,故此处不再列出
    "basic": {
        "analytics":       False,  # 数据分析
        "workflow":        False,  # 工作流自动化(规划中,未开发)
        "api":             False,  # 对外集成 API(规划中,未开发)
        "advanced_search": False,  # 高级搜索 / 混合检索重排(规划中,未开发)
        "fine_tuning":     False,  # 定制微调(规划中,未开发)
    },
    # 专业版(定价表卖点:工作流、API、高级搜索)—— 三项均在规划中、暂未开发
    "pro": {
        "analytics":       False,
        "workflow":        False,  # 规划中,未开发
        "api":             False,  # 规划中,未开发
        "advanced_search": True,   # ✅ 已实现(混合检索 + RRF 重排,专业版起解锁)
        "fine_tuning":     False,
    },
    # 企业版(定价表卖点:多部门隔离、定制微调、数据分析、7×24)
    "enterprise": {
        "analytics":       True,   # ✅ 已实现
        "workflow":        False,  # 规划中,未开发
        "api":             False,  # 规划中,未开发
        "advanced_search": True,   # ✅ 已实现(混合检索 + RRF 重排)
        "fine_tuning":     False,  # 规划中,未开发(企业版卖点,待开发)
        # 注:7×24 为服务承诺(SLA),由人工/合同保障,不在此处用代码控制
    },
}

def features():
    return EDITION_FEATURES.get(EDITION, EDITION_FEATURES["enterprise"])

# ===== 域配置(domains/*.yaml)=====
# 增长规则(定稿 §3):触发形状在引擎、参数在这些 YAML;新域=新文件,代码零改动。
# 不做热加载(改配置重启生效——demo优化步骤 9 的"不做"条款)。
import yaml

_DOMAINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domains")
DOMAINS = {}
for _f in sorted(os.listdir(_DOMAINS_DIR)):
    if _f.endswith(".yaml"):
        with open(os.path.join(_DOMAINS_DIR, _f), encoding="utf-8") as _fh:
            _d = yaml.safe_load(_fh)
            DOMAINS[_d["id"]] = _d


def domain_for(question: str) -> str:
    """① 路由的域判定:问题含专业域关键词 → 该域;否则默认通用域。
    只选闸门参数,绝不改变 visible_depts——权限永远由角色定,与路由无关。"""
    for did, d in DOMAINS.items():
        if did != "general" and any(k in (question or "") for k in (d.get("match_keywords") or [])):
            return did
    return "general"


def domain_cfg(domain_id: str) -> dict:
    return DOMAINS.get(domain_id, DOMAINS["general"])


# ===== 角色 -> 可见部门 =====
ROLES = {
    "admin":   {"name": "管理员", "all": True,  "depts": ["全员", "人事", "财务", "技术", "销售"]},
    "staff":   {"name": "普通员工", "all": False, "depts": ["全员"]},
    "finance": {"name": "财务部",   "all": False, "depts": ["全员", "财务"]},
    "hr":      {"name": "人事部",   "all": False, "depts": ["全员", "人事"]},
    "hr_rubric_editor": {"name": "人事评分配置", "all": False, "depts": ["全员", "人事"]},
    "tech":    {"name": "技术部",   "all": False, "depts": ["全员", "技术"]},
}

def role_depts(role: str):
    r = ROLES.get(role, ROLES["staff"])
    return None if r["all"] else r["depts"]

# ===== Step 2.4 群发 · SMTP(默认全空=关;未配置时前端发送键降级为导出 CSV) =====
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")   # 明文=单机部署既定边界(记债)
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
MAIL_THROTTLE_S = float(os.getenv("MAIL_THROTTLE_S", "0.5"))  # 封间节流,防企业邮箱限流
# 传输安全逃生阀(Task 5 fix,默认关=拒明文/拒未校验证书):打开后允许连接自签证书/内网 SMTP
# (TLS 不校验证书),且服务器完全不提供 STARTTLS 时才退回明文——仅供操作者显式承担风险时打开。
SMTP_INSECURE = os.getenv("SMTP_INSECURE", "").lower() in ("1", "true", "yes")
