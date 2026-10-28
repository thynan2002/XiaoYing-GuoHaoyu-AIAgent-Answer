"""测试公共工具：把一次完整运行封装成函数，全部落在 tmp 目录，互不影响。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agent import Agent                       # noqa: E402
from src.models import Task                       # noqa: E402
from src.planner import MockPlanner               # noqa: E402
from src.tools import ToolRegistry                # noqa: E402


@pytest.fixture()
def plan_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plans"
    d.mkdir()
    return d


def run_agent(tmp_path: Path, plan_dir: Path, *, topic: str = "秋季校园咖啡新品推广",
              budget: float | None = 10, deadline: str = "2026-11-02 18:00",
              available_minutes: float | None = 7000,
              inject: dict | None = None, max_steps: int = 12,
              max_tool_calls: int = 12, planner=None, out: str = "run") -> dict:
    registry = ToolRegistry(plan_dir=plan_dir, inject=inject)
    agent = Agent(planner or MockPlanner(), registry, out_dir=tmp_path / out,
                  run_id="test", max_steps=max_steps, max_tool_calls=max_tool_calls)
    task = Task(topic=topic, budget=budget, deadline=deadline,
                available_minutes=available_minutes)
    return agent.run(task)


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
