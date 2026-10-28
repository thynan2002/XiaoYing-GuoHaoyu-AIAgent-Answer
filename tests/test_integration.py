"""端到端集成测试：真实 HTTP 的模型路径、失败步骤续跑、交付字段完整性。"""

from __future__ import annotations

import importlib.util
import threading
from http.server import ThreadingHTTPServer

import pytest

from conftest import ROOT, load, run_agent
from src import config
from src.agent import Agent
from src.models import Task
from src.planner import LLMPlanner, MockPlanner
from src.tools import ToolRegistry


def _load_fake_server():
    path = ROOT / "scripts" / "fake_llm_server.py"
    spec = importlib.util.spec_from_file_location("fake_llm_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_llm():
    """在随机端口起一个 OpenAI 兼容端点（真 HTTP，不是打桩）。"""
    mod = _load_fake_server()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        srv.shutdown()
        srv.server_close()


class TestLLMHttpPath:
    def test_planner_completes_over_real_http(self, tmp_path, plan_dir, fake_llm):
        planner = LLMPlanner(model="fake-rule-model", base_url=fake_llm, api_key="dummy")
        agent = Agent(planner, ToolRegistry(plan_dir=plan_dir),
                      out_dir=tmp_path / "llm", run_id="llm")
        task = Task(topic="秋季校园咖啡新品推广", budget=10,
                    deadline="2026-11-02 18:00", available_minutes=7000)
        r = agent.run(task)

        assert r["status"] == "COMPLETED"
        assert r["planner"] == "llm"
        # 模型走了真 HTTP，参数由它自己给：8.0 是在 12.0 被否之后选的
        assert r["plan"]["cost_total"] == 8.0
        trace = load(tmp_path / "llm" / "trace.json")
        assert [s["action"] for s in trace["steps"]][:3] == [
            "list_plan_variants", "estimate_cost", "estimate_cost"]

    def test_unreachable_endpoint_ends_cleanly(self, tmp_path, plan_dir):
        planner = LLMPlanner(model="x", base_url="http://127.0.0.1:9/v1", api_key="dummy",
                             timeout=1)
        agent = Agent(planner, ToolRegistry(plan_dir=plan_dir),
                      out_dir=tmp_path / "dead", run_id="dead")
        r = agent.run(Task(topic="t", budget=10, deadline="2026-11-02 18:00",
                           available_minutes=7000))
        assert r["status"] == "INCOMPLETE_PLANNER"
        assert "不可达" in r["status_reason"]


class TestResumeFromFailure:
    def test_save_failure_then_resume_does_not_redo_earlier_steps(self, tmp_path, plan_dir):
        first = run_agent(tmp_path, plan_dir, topic="校园咖啡快闪店预热",
                          inject={"save_plan": {"times": 99, "mode": "error"}}, out="run1")
        assert first["status"] == "INCOMPLETE_SAVE"
        assert first["saved_plan"] is None
        # 失败前拿到的素材/脚本/估算必须还在
        assert first["artifacts"]["materials"] and first["artifacts"]["script"]

        state = load(tmp_path / "run1" / "state.json")
        task = Task(topic=state["task"]["topic"], budget=state["task"]["budget"],
                    deadline=state["task"]["deadline"],
                    available_minutes=state["task"]["available_minutes"])
        agent = Agent(MockPlanner(), ToolRegistry(plan_dir=plan_dir),
                      out_dir=tmp_path / "run2", run_id="resume")
        r = agent.run(task, resume_state=state)

        assert r["status"] == "COMPLETED"
        assert r["resume"] == {"resumed": True, "from_step": 8}
        trace = load(tmp_path / "run2" / "trace.json")
        after = [s["action"] for s in trace["steps"] if s["step"] > 8]
        assert after == ["save_plan", "finish"]          # 只补失败那步，不重跑检索与脚本
        assert [s["action"] for s in trace["steps"]].count("search_materials") == 1
        assert [s["action"] for s in trace["steps"]].count("generate_script") == 1


class TestDeliveryFields:
    def test_result_carries_plan_steps_and_cost(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir)
        # 任务计划：工序清单，费用与工时都能自洽
        outline = r["plan"]["outline"]
        assert len(outline) == 4
        assert round(sum(o["cost"] for o in outline), 2) == r["plan"]["cost_total"]
        # 步骤状态：每一步都能看到成败
        assert r["steps_brief"][-1]["action"] == "finish"
        assert {s["status"] for s in r["steps_brief"]} <= {"ok", "partial", "error", "terminal"}
        # 费用合计 + 已保存方案
        assert r["plan"]["cost_total"] == 8.0
        assert r["saved_plan"]["plan_id"].startswith("plan_")

    def test_incomplete_run_has_no_fake_plan(self, tmp_path, plan_dir):
        r = run_agent(tmp_path, plan_dir, budget=4)
        assert r["status"] == "INCOMPLETE_BUDGET"
        assert r["plan"] is None                     # 被否的候选不能冒充交付方案
        assert r["rejected"] and r["status_reason"]
