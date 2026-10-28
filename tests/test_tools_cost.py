"""成本与素材工具的单测：费用公式必须能被独立复算。"""

from __future__ import annotations

import pytest

from conftest import load
from src import config
from src.tools import ToolRegistry, compute_cost

ROOT_COSTS = config.load_costs()


class TestCostFormula:
    def test_variant_totals_are_12_8_5(self):
        assert compute_cost("full")["total"] == 12.0
        assert compute_cost("lite")["total"] == 8.0
        assert compute_cost("micro")["total"] == 5.0

    @pytest.mark.parametrize("vid", ["full", "lite", "micro"])
    def test_breakdown_sums_to_total(self, vid):
        est = compute_cost(vid)
        assert round(sum(b["amount"] for b in est["breakdown"]), 2) == est["total"]

    def test_manual_recompute_matches(self):
        """按单价表手算一遍，和工具结果对齐（防止公式实现漂移）。"""
        u = ROOT_COSTS["unit_costs"]
        spec = ROOT_COSTS["variants"]["full"]
        manual = (u["material_license"]["per_item"] * spec["material_items"]
                  + u["script"]["base"] + u["script"]["per_30s"] * 2
                  + u["voice"]["base"] + u["voice"]["per_30s"] * 2
                  + u["edit"]["base"] + u["edit"]["per_shot"] * spec["shots"]
                  + u["review"]["flat"])
        assert round(manual, 2) == compute_cost("full")["total"]

    def test_fewer_materials_is_cheaper(self):
        full4 = compute_cost("full")["total"]
        full2 = compute_cost("full", item_count=2)["total"]
        assert full2 < full4
        assert round(full4 - full2, 2) == round(2 * 0.6, 2)

    def test_unknown_variant_raises(self):
        from src.tools import ToolError
        with pytest.raises(ToolError):
            compute_cost("does-not-exist")


class TestMaterialSearch:
    def test_search_returns_hits_and_marks_requested(self, tmp_path):
        reg = ToolRegistry(plan_dir=tmp_path)
        obs = reg.call("search_materials", {"queries": ["校园", "咖啡"], "limit": 3})
        assert obs.status in ("ok", "partial")
        assert obs.data["requested"] == 3
        assert obs.data["returned"] == len(obs.data["items"]) >= 1
        assert all({"id", "title", "tags"} <= set(m) for m in obs.data["items"])

    def test_empty_queries_is_business_error(self, tmp_path):
        reg = ToolRegistry(plan_dir=tmp_path)
        obs = reg.call("search_materials", {"queries": [], "limit": 2})
        assert obs.status == "error"
        assert "queries" in obs.error

    def test_no_hit_is_partial_not_crash(self, tmp_path):
        reg = ToolRegistry(plan_dir=tmp_path)
        obs = reg.call("search_materials", {"queries": ["不存在的标签xyz"], "limit": 2})
        assert obs.status == "partial"
        assert obs.data["returned"] == 0


class TestSavePlanGuard:
    def test_rejects_cost_mismatch(self, tmp_path):
        reg = ToolRegistry(plan_dir=tmp_path)
        reg.call("search_materials", {"queries": ["校园"], "limit": 2})
        obs = reg.call("save_plan", {
            "topic": "t", "variant_id": "lite", "material_ids": ["m003", "m014"],
            "cost_total": 3.0, "script": {"title": "x"}})
        assert obs.status == "error"
        assert "费用不一致" in obs.error
        assert list(tmp_path.glob("*.json")) == []

    def test_same_content_is_idempotent(self, tmp_path):
        reg = ToolRegistry(plan_dir=tmp_path)
        args = {"topic": "t", "variant_id": "micro", "material_ids": ["m003"],
                "cost_total": compute_cost("micro", 1)["total"], "script": {"title": "x"}}
        first = reg.call("save_plan", args)
        second = reg.call("save_plan", args)
        assert first.status == second.status == "ok"
        assert first.data["deduped"] is False
        assert second.data["deduped"] is True
        assert first.data["plan_id"] == second.data["plan_id"]
        assert len(list(tmp_path.glob("*.json"))) == 1
        saved = load(tmp_path / f"{first.data['plan_id']}.json")
        assert saved["cost_total"] == args["cost_total"]


class TestTaskPlanOutline:
    """outline 是题目要求的"任务计划"：工序、工时、归属费用必须自洽。"""

    @pytest.mark.parametrize("vid", ["full", "lite", "micro"])
    def test_outline_sums_match_estimate(self, vid):
        est = compute_cost(vid)
        assert round(sum(o["cost"] for o in est["outline"]), 2) == est["total"]
        seconds = sum(o["seconds"] for o in est["outline"])
        import math
        assert math.ceil(seconds / 60) == est["estimated_minutes"]   # 工时口径一致
        assert [o["no"] for o in est["outline"]] == [1, 2, 3, 4]

    def test_micro_has_no_review_stage(self):
        stages = {o["task"]: o for o in compute_cost("micro")["outline"]}
        assert stages["内容审核"]["seconds"] == 0
        assert stages["内容审核"]["cost"] == 0


class TestLongQueryRetrieval:
    """运营给的是一整句主题，不是关键词；检索不能因此 0 命中。"""

    def test_full_sentence_query_hits(self, tmp_path):
        reg = ToolRegistry(plan_dir=tmp_path)
        obs = reg.call("search_materials",
                       {"queries": ["秋季校园咖啡新品推广"], "limit": 2})
        assert obs.data["returned"] >= 1
        assert any("咖啡" in m["title"] or "校园" in m["title"] for m in obs.data["items"])

    def test_noise_query_still_returns_nothing(self, tmp_path):
        reg = ToolRegistry(plan_dir=tmp_path)
        obs = reg.call("search_materials", {"queries": ["量子计算芯片封装"], "limit": 2})
        assert obs.data["returned"] == 0
        assert obs.status == "partial"


class TestToolRobustness:
    """模型给错参数时，工具要返回 error 观察，而不是把 Agent 打崩。"""

    def test_unknown_variant_is_error_not_exception(self, tmp_path):
        reg = ToolRegistry(plan_dir=tmp_path)
        obs = reg.call("generate_script", {"variant_id": "ultra",
                                           "material_ids": ["m003"], "topic": "t"})
        assert obs.status == "error"
        assert "未知方案" in obs.error

    def test_duplicate_material_ids_not_double_charged(self, tmp_path):
        reg = ToolRegistry(plan_dir=tmp_path)
        cost = compute_cost("micro", 1)["total"]
        obs = reg.call("save_plan", {
            "topic": "t", "variant_id": "micro",
            "material_ids": ["m003", "m003"], "cost_total": cost,
            "script": {"title": "x"}})
        assert obs.status == "ok", obs.error
        assert load(tmp_path / f"{obs.data['plan_id']}.json")["cost_total"] == cost
