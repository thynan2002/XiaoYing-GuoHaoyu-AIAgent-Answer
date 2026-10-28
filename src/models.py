"""数据结构定义：任务输入、动作、观察结果、单步轨迹。"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------- 任务输入

REQUIRED_FIELDS = ("topic", "budget", "deadline")


@dataclass
class Task:
    """运营人员提交的内容活动需求。

    budget / deadline 允许缺失：缺失时 Agent 必须停下要信息，而不是自己编一个。
    """

    topic: str = ""
    budget: float | None = None
    deadline: str = ""          # "YYYY-MM-DD HH:MM"（本地时间）
    tone: str = "轻快"
    audience: str = "泛用户"
    notes: str = ""
    available_minutes: float | None = None   # 由 CLI 依据 --now 与 deadline 算出

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not str(self.topic or "").strip():
            missing.append("topic")
        if self.budget is None:
            missing.append("budget")
        if not str(self.deadline or "").strip():
            missing.append("deadline")
        return missing

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["missing_fields"] = self.missing_fields()
        return d


# ---------------------------------------------------------------- 动作 / 观察

@dataclass
class Action:
    """模型输出的一步决策。"""

    action: str
    args: dict[str, Any] = field(default_factory=dict)
    decision_basis: str = ""      # 一句话决策依据，不是思维链

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "args": self.args,
                "decision_basis": self.decision_basis}


@dataclass
class Observation:
    """工具执行结果。status: ok / partial / error"""

    status: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    tool: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "summary": self.summary,
                "data": self.data, "error": self.error, "tool": self.tool}


# ---------------------------------------------------------------- 轨迹

@dataclass
class StepRecord:
    step: int
    action: str
    args: dict[str, Any]
    decision_basis: str
    observation: dict[str, Any] | None = None
    elapsed_ms: int = 0
    follow_up: str = ""          # 由 agent 在收尾时回填：本步的下一步动作
    state_after: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
