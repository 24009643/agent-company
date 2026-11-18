"""AskState:一次问答的显式状态对象(=未来 LangGraph 的 StateGraph schema,定稿 §10)。
每个 driver 节点是 (state)->state;字段按"输入/路由/执行/审核/出口"分区,对应主 agent 五框。"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AskState:
    # ---- 输入(app 层组装:权限咽喉 depts、服务端会话 history 都在进 driver 前就位)----
    question: str
    user: dict
    model_id: str
    conversation_id: str
    depts: Optional[list]            # 可见部门(None=全部),权限咽喉的产物
    history: list = field(default_factory=list)
    mode: str = "fast"              # fast|deep 占位,机制后期随前端做(定稿 §2.2),暂不参与逻辑

    # ---- ① 路由结果 ----
    domain: str = "general"         # 命中的域;≥2 域时由路由分类填,并产出多域 plan
    cfg: dict = field(default_factory=dict)  # 该域的闸门参数(domains/*.yaml)

    # ---- ② 执行结果 ----
    hits: list = field(default_factory=list)
    rewritten: Optional[str] = None  # L1 改写句,落审计;None=未触发改写
    answer_mode: str = "refused"     # direct|boundary|refused,三值闸门产出(代码决策权)
    tool_trace: list = field(default_factory=list)  # L2 工具调用轨迹(审计 + L6 数字白名单来源)

    # ---- ③ 审核结果 ----
    refused: bool = False

    # ---- ⑤ 出口 ----
    answer: str = ""
    qid: Optional[str] = None
