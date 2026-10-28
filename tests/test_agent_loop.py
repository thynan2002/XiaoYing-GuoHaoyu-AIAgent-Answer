"""Agent 主循环行为测试：正常、缺输入、工具失败、预算超限、重复执行、上限、收尾校验。"""

from __future__ import annotations

import json

import pytest

from conftest import load, run_agent
from src.models import Action, Observation
from src.planner import DecisionContext, MockPlanner


# ---------------------------------------------------------------- 正常链路

class TestHappyPath:
    def test_completes_within_budget_and_saves(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir)
        assert r["status"] == "COMPLETED"
        assert r["plan"]["cost_total"] == 8.0            # 12 超预算 → 退到 8
        assert r["plan"]["cost_total"] <= 10
        assert r["rejected"][0]["variant"] == "full"
        assert r["saved_plan"]["deduped"] is False

        saved = load(r["saved_plan"]["path"])
        assert saved["cost_total"] == 8.0
        assert saved["script"]["title"].startswith("秋季校园咖啡新品推广")
        assert saved["variant_id"] == "lite"

    def test_react_loop_has_decide_act_observe_per_step(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir)
        trace = load(tmp_path / "run" / "trace.json")
        steps = trace["steps"]
        assert [s["action"] for s in steps][:3] == ["list_plan_variants", "estimate_cost", "estimate_cost"]
        for s in steps[:-1]:
            assert s["observation"] and s["observation"]["summary"]  # 每步都有观察
            assert s["decision_basis"]
            assert s["follow_up"]
        # 决策确实引用了上一步的数字
        assert "12.0" in steps[2]["decision_basis"]

    def test_tool_call_budget_respected(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir)
        assert r["usage"]["tool_calls"] <= r["usage"]["max_tool_calls"]


# ---------------------------------------------------------------- 缺输入

class TestMissingInput:
    def test_missing_budget_asks_instead_of_guessing(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, budget=None)
        assert r["status"] == "NEED_INPUT"
        assert "budget" in r["status_reason"]
        assert r["usage"]["tool_calls"] == 0        # 没有乱调工具
        assert r["saved_plan"] is None

    def test_missing_deadline_also_asks(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, deadline="")
        assert r["status"] == "NEED_INPUT"
        assert "deadline" in r["status_reason"]

    def test_resume_after_filling_budget_completes(self, tmp_path, plan_dir):
        first = run_agent(tmp_path, plan_dir, budget=None, out="run1")
        assert first["status"] == "NEED_INPUT"

        state = load(tmp_path / "run1" / "state.json")
        state["task"]["budget"] = 10
        state["status"] = None
        from src.agent import Agent
        from src.models import Task
        from src.tools import ToolRegistry

        task = Task(topic=state["task"]["topic"], budget=10,
                    deadline=state["task"]["deadline"],
                    available_minutes=state["task"]["available_minutes"])
        agent = Agent(MockPlanner(), ToolRegistry(plan_dir=plan_dir),
                      out_dir=tmp_path / "run2", run_id="resume")
        r = agent.run(task, resume_state=state)
        assert r["status"] == "COMPLETED"
        assert r["usage"]["steps"] > 1               # 从断点继续，不是从零开始
        trace = load(tmp_path / "run2" / "trace.json")
        assert [s["step"] for s in trace["steps"]] == list(range(1, len(trace["steps"]) + 1))


# ---------------------------------------------------------------- 工具失败

class TestToolFailure:
    def test_script_failure_retries_then_degrades(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, budget=14,
                      inject={"generate_script": {"times": 2, "mode": "error"}})
        trace = load(tmp_path / "run" / "trace.json")
        actions = [s["action"] for s in trace["steps"]]
        obs = [s["observation"]["status"] for s in trace["steps"]]

        assert actions.count("generate_script") == 3          # 失败 2 次 + 成功 1 次
        assert obs[4] == obs[5] == "error"                    # 前两次都如实报错
        assert "降级" in trace["steps"][6]["decision_basis"]   # 降级决策有据可查
        assert r["status"] == "COMPLETED"
        assert r["plan"]["variant_id"] == "lite"              # 从 full 退到 lite
        # 失败前拿到的素材与估算没有被丢掉
        assert r["artifacts"]["materials"]
        assert r["rejected"] or r["plan"]

    def test_failure_never_marked_success(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, budget=14,
                      inject={"generate_script": {"times": 2, "mode": "error"}})
        trace = load(tmp_path / "run" / "trace.json")
        failed = [s for s in trace["steps"] if s["observation"]["status"] == "error"]
        assert len(failed) == 2
        for s in failed:
            assert s["observation"]["summary"].startswith("generate_script 失败")

    def test_all_tools_down_returns_incomplete_with_partial_state(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir,
                      inject={"generate_script": {"times": 99, "mode": "error"}})
        assert r["status"].startswith("INCOMPLETE")
        assert r["saved_plan"] is None
        assert r["status_reason"]


# ---------------------------------------------------------------- 预算超限

class TestBudget:
    def test_no_feasible_variant_reports_gap(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, budget=4)
        assert r["status"] == "INCOMPLETE_BUDGET"
        assert "缺口" in r["status_reason"]
        assert r["saved_plan"] is None                         # 不能假装保存成功
        assert len(r["rejected"]) == 3
        assert {x["variant"] for x in r["rejected"]} == {"full", "lite", "micro"}
        assert list(plan_dir.glob("*.json")) == []             # 目录里确实没落盘

    def test_exactly_at_budget_is_accepted(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, budget=8)
        assert r["status"] == "COMPLETED"
        assert r["plan"]["cost_total"] == 8.0


# ---------------------------------------------------------------- 重复执行

class TestRepeat:
    def test_second_run_hits_idempotent_save(self, tmp_path, plan_dir):
        first = run_agent(tmp_path, plan_dir, out="run1")
        second = run_agent(tmp_path, plan_dir, out="run2")
        assert first["status"] == second["status"] == "COMPLETED"
        assert first["saved_plan"]["plan_id"] == second["saved_plan"]["plan_id"]
        assert second["saved_plan"]["deduped"] is True
        assert len(list(plan_dir.glob("*.json"))) == 1


# ---------------------------------------------------------------- 上限

class TestLimit:
    def test_step_limit_stops_and_reports(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, max_steps=3)
        assert r["status"] == "INCOMPLETE_LIMIT"
        assert r["usage"]["steps"] == 3
        assert r["saved_plan"] is None
        trace = load(tmp_path / "run" / "trace.json")
        assert len(trace["steps"]) == 3
        assert trace["limits"] == {"max_steps": 3, "max_tool_calls": 12}

    def test_tool_call_limit_stops(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, max_tool_calls=2)
        assert r["status"] == "INCOMPLETE_LIMIT"
        assert r["usage"]["tool_calls"] <= 2


# ---------------------------------------------------------------- 收尾校验

class _ClaimSuccessWithoutDoingAnything:
    """假规划器：什么都没做就宣称完成，用来验证 Agent 侧的兜底校验。"""

    name = "fake-claim"
    is_mock = True

    def decide(self, ctx: DecisionContext) -> Action:
        return Action("finish", {"status": "COMPLETED", "reason": "我好了"}, "假装完成")


class TestFinishGuard:
    def test_claiming_done_without_artifacts_is_downgraded(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, planner=_ClaimSuccessWithoutDoingAnything())
        assert r["status"] == "INCOMPLETE_INCONSISTENT"
        assert "校验不通过" in r["status_reason"]
        assert "没有落盘方案" in r["status_reason"]

    def test_illegal_status_is_rejected(self, tmp_path, plan_dir):
        class Bad:
            name = "bad"
            is_mock = True

            def decide(self, ctx):
                return Action("finish", {"status": "DONE", "reason": "x"}, "x")

        r = run_agent(tmp_path, plan_dir, planner=Bad())
        assert r["status"] == "INCOMPLETE_PLANNER"
