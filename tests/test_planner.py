"""规划器测试：真实模型的输出解析（离线）/ mock 策略的关键分支。"""

from __future__ import annotations

import pytest

from src.planner import LLMPlanner, MockPlanner, PlannerError, build_planner


class TestLLMDecisionParsing:
    """解析层不联网，用固定字符串验证：模型输出脏了会怎样。"""

    def test_plain_json(self):
        a = LLMPlanner._parse_decision(
            '{"action": "estimate_cost", "args": {"variant_id": "lite"}, '
            '"decision_basis": "full 12 超预算"}')
        assert a.action == "estimate_cost"
        assert a.args == {"variant_id": "lite"}
        assert "12" in a.decision_basis

    def test_fenced_json(self):
        a = LLMPlanner._parse_decision(
            '```json\n{"action": "finish", "args": {"status": "COMPLETED"}}\n```')
        assert a.action == "finish"
        assert a.args["status"] == "COMPLETED"

    def test_json_with_prose_around(self):
        a = LLMPlanner._parse_decision(
            '我先想一下。{"action": "search_materials", "args": {"queries": ["校园"], "limit": 2}}')
        assert a.action == "search_materials"

    def test_unknown_action_rejected(self):
        with pytest.raises(PlannerError, match="未知动作"):
            LLMPlanner._parse_decision('{"action": "rm_rf_everything", "args": {}}')

    def test_non_json_rejected(self):
        with pytest.raises(PlannerError, match="不含 JSON"):
            LLMPlanner._parse_decision("我觉得应该先估算一下费用。")

    def test_args_must_be_object(self):
        with pytest.raises(PlannerError, match="args"):
            LLMPlanner._parse_decision('{"action": "estimate_cost", "args": ["lite"]}')

    def test_missing_basis_gets_explicit_placeholder(self):
        a = LLMPlanner._parse_decision('{"action": "finish", "args": {}}')
        assert a.decision_basis == "模型未给出决策依据"

    def test_missing_api_key_fails_fast(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(PlannerError, match="OPENAI_API_KEY"):
            build_planner("llm")


class TestMockPolicy:
    def _ctx(self, **kw):
        from src.models import Task
        from src.planner import DecisionContext

        task = Task(topic="校园咖啡推广", budget=kw.pop("budget", 10),
                    deadline="2026-11-02 18:00",
                    available_minutes=kw.pop("available_minutes", 7000))
        return DecisionContext(task=task, scratch={}, last_action=kw.pop("last_action", None),
                               last_obs=kw.pop("last_obs", None), step=1)

    def test_first_action_is_list_variants(self):
        a = MockPlanner().decide(self._ctx())
        assert a.action == "list_plan_variants"

    def test_over_budget_moves_to_next_variant(self):
        from src.models import Observation
        p = MockPlanner()
        ctx = self._ctx()
        ctx.scratch["queue"] = ["full", "lite"]
        ctx.scratch["labels"] = {"full": "完整版", "lite": "精简版"}
        ctx.last_action = "estimate_cost"
        ctx.last_obs = Observation("ok", "12.0", data={
            "variant_id": "full", "label": "完整版", "total": 12.0,
            "estimated_minutes": 12, "material_items": 4})
        a = p.decide(ctx)
        assert a.action == "estimate_cost"
        assert a.args["variant_id"] == "lite"
        assert "12.0" in a.decision_basis and "10" in a.decision_basis

    def test_queue_exhausted_finishes_incomplete(self):
        from src.models import Observation
        p = MockPlanner()
        ctx = self._ctx(budget=2)
        ctx.scratch["queue"] = []
        ctx.scratch["rejected"] = [{"variant": "micro", "reason": "费用 5.0 超预算 2.0", "cost": 5.0}]
        ctx.last_action = "estimate_cost"
        ctx.last_obs = Observation("ok", "5.0", data={
            "variant_id": "micro", "label": "极简版", "total": 5.0,
            "estimated_minutes": 3, "material_items": 1})
        # 直接把 queue 清空后再次决策
        ctx.scratch["queue"] = []
        a = p.decide(ctx)
        assert a.action == "finish"
        assert a.args["status"] == "INCOMPLETE_BUDGET"
        assert "缺口" in a.args["reason"]
