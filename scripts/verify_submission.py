"""提交前自检：不跑模型、不联网，只核对仓库里的交付物是否自洽。

    python scripts/verify_submission.py

检查四类东西：
1. 规范要求的目录/文件是否齐全；
2. 每个实施例的 result.json / trace.json / state.json / stdout.txt 是否齐全；
3. 每个 trace 是否真的包含"步骤、工具参数、返回摘要、后续动作、决策依据"；
4. 题目必验场景（正常、缺信息、工具失败、预算超限、重复执行、上限、续跑）的
   状态与关键数字是否与文档一致。

任何一项不符都以退出码 1 结束。评审也可以只跑这一条命令做快速核对。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"

REQUIRED = [
    "README.md", "requirements.txt", ".env.example", ".gitignore",
    "src/agent.py", "src/planner.py", "src/tools.py", "src/cli.py", "src/models.py",
    "data/costs.json", "data/materials.json",
    "docs/01-requirements.md", "docs/02-solution.md", "docs/03-evaluation.md",
    "docs/04-ai-usage.md", "docs/05-retrospective.md",
    "examples/README.md", "examples/tests-output.txt",
    "tests/test_agent_loop.py", "tests/test_tools_cost.py",
    "tests/test_planner.py", "tests/test_integration.py",
    "scripts/gen_examples.py", "scripts/fake_llm_server.py",
]

# 每个用例只声明"必须成立"的最小事实，避免把文档里的每个数字都复制一遍
EXPECT = {
    "case-01-normal": dict(status="COMPLETED", cost=8.0, saved=True, deduped=False,
                           rejected="full", min_steps=7),
    "case-02-missing-input": dict(status="NEED_INPUT", saved=False, steps=1, calls=0),
    "case-03-resume-after-input": dict(status="COMPLETED", cost=8.0, saved=True, resumed=True),
    "case-04-tool-failure-degrade": dict(status="COMPLETED", cost=8.0, saved=True,
                                         errors_at_least=2),
    "case-05-partial-materials": dict(status="COMPLETED", cost=10.2, saved=True,
                                      partial_at_least=1),
    "case-06-budget-exceeded": dict(status="INCOMPLETE_BUDGET", saved=False,
                                    rejected_count=3, reason_has="缺口"),
    "case-07-repeated-save": dict(status="COMPLETED", saved=True, deduped=True),
    "case-08-step-limit": dict(status="INCOMPLETE_LIMIT", saved=False, steps=3,
                               max_steps=3),
    "case-09-tight-deadline": dict(status="COMPLETED", cost=8.0, saved=True, minutes=7),
    "case-10-llm-endpoint": dict(status="COMPLETED", cost=8.0, saved=True,
                                 planner="llm", rejected="full"),
    "case-13-llm-tool-failure": dict(status="COMPLETED", cost=8.0, saved=True,
                                     planner="llm", rejected="full", errors_at_least=2),
    "case-11-save-failure": dict(status="INCOMPLETE_SAVE", saved=False, keep_artifacts=True),
    "case-12-resume-after-save-failure": dict(status="COMPLETED", saved=True, resumed=True),
}


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes = 0

    def check(self, ok: bool, message: str) -> None:
        if ok:
            self.passes += 1
        else:
            self.failures.append(message)

    def finish(self) -> int:
        if self.failures:
            print(f"[FAIL] {len(self.failures)} 项不通过，{self.passes} 项通过")
            for f in self.failures:
                print(" -", f)
            return 1
        print(f"[PASS] 提交物自检通过：{self.passes} 项检查，{len(EXPECT)} 个实施例。")
        return 0


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_case(c: Checker, name: str, expect: dict) -> None:
    d = EX / name
    for f in ("command.txt", "stdout.txt", "result.json", "trace.json", "state.json"):
        c.check((d / f).exists(), f"{name}: 缺少 {f}")
    if not (d / "result.json").exists() or not (d / "trace.json").exists():
        return

    r = load(d / "result.json")
    t = load(d / "trace.json")
    steps = t.get("steps") or []

    c.check(r.get("status") == expect["status"],
            f"{name}: 状态 {r.get('status')} != {expect['status']}")
    c.check(r.get("usage", {}).get("steps") == len(steps),
            f"{name}: usage.steps 与 trace 步数不一致")
    c.check(len(steps) > 0, f"{name}: trace 为空")
    c.check(len(r.get("steps_brief") or []) == len(steps),
            f"{name}: steps_brief 与 trace 步数不一致")

    # 题目要求 trace 必须能回答：做了什么、参数、结果、下一步、为什么
    for s in steps:
        c.check(isinstance(s.get("args"), dict), f"{name} step{s.get('step')}: 缺少 args")
        c.check(bool(s.get("decision_basis")), f"{name} step{s.get('step')}: 缺少决策依据")
        c.check(bool(s.get("follow_up")), f"{name} step{s.get('step')}: 缺少后续动作")
        obs = s.get("observation") or {}
        c.check(bool(obs.get("status")) and bool(obs.get("summary")),
                f"{name} step{s.get('step')}: observation 缺 status/summary")

    # 通用：完成了就必须有报价、落盘、脚本，且不越预算/工时
    plan = r.get("plan")
    task = r.get("task") or {}
    if expect["status"] == "COMPLETED":
        c.check(plan is not None, f"{name}: COMPLETED 但没有 plan")
        c.check(bool((r.get("saved_plan") or {}).get("plan_id")),
                f"{name}: COMPLETED 但没有 saved_plan")
        c.check((d / "saved_plan.json").exists(), f"{name}: 缺少 saved_plan.json 副本")
        if plan:
            budget = task.get("budget")
            avail = task.get("available_minutes")
            c.check(budget is None or float(plan["cost_total"]) <= float(budget) + 1e-9,
                    f"{name}: plan 超预算")
            c.check(avail is None or int(plan["estimated_minutes"]) <= float(avail),
                    f"{name}: plan 超工时")
    else:
        c.check(r.get("saved_plan") is None, f"{name}: 未完成却报告了 saved_plan")

    # 逐用例的硬事实
    if "cost" in expect:
        c.check(plan and float(plan["cost_total"]) == expect["cost"],
                f"{name}: cost 期望 {expect['cost']}，实际 {plan and plan.get('cost_total')}")
    if "minutes" in expect:
        c.check(plan and int(plan["estimated_minutes"]) == expect["minutes"],
                f"{name}: minutes 期望 {expect['minutes']}")
    if "saved" in expect:
        c.check(bool(r.get("saved_plan")) is expect["saved"],
                f"{name}: saved_plan 期望 {expect['saved']}")
    if "deduped" in expect:
        c.check(bool((r.get("saved_plan") or {}).get("deduped")) is expect["deduped"],
                f"{name}: deduped 期望 {expect['deduped']}")
    if "rejected" in expect:
        c.check(any(x.get("variant") == expect["rejected"] for x in r.get("rejected") or []),
                f"{name}: rejected 里没有 {expect['rejected']}")
    if "rejected_count" in expect:
        c.check(len(r.get("rejected") or []) == expect["rejected_count"],
                f"{name}: rejected 数量不符")
    if "reason_has" in expect:
        c.check(expect["reason_has"] in (r.get("status_reason") or ""),
                f"{name}: status_reason 缺少 {expect['reason_has']}")
    if "planner" in expect:
        c.check(r.get("planner") == expect["planner"], f"{name}: planner 不符")
    if "resumed" in expect:
        c.check(bool((r.get("resume") or {}).get("resumed")) is expect["resumed"],
                f"{name}: resume 标记不符")
    if "steps" in expect:
        c.check(r.get("usage", {}).get("steps") == expect["steps"],
                f"{name}: 步数期望 {expect['steps']}")
    if "calls" in expect:
        c.check(r.get("usage", {}).get("tool_calls") == expect["calls"],
                f"{name}: 工具调用数期望 {expect['calls']}")
    if "max_steps" in expect:
        c.check((t.get("limits") or {}).get("max_steps") == expect["max_steps"],
                f"{name}: max_steps 不符")
    if "min_steps" in expect:
        c.check(len(steps) >= expect["min_steps"], f"{name}: 步数少于 {expect['min_steps']}")
    if "errors_at_least" in expect:
        n = sum(1 for s in steps if (s.get("observation") or {}).get("status") == "error")
        c.check(n >= expect["errors_at_least"], f"{name}: 失败步骤少于 {expect['errors_at_least']}")
    if "partial_at_least" in expect:
        n = sum(1 for s in steps if (s.get("observation") or {}).get("status") == "partial")
        c.check(n >= expect["partial_at_least"], f"{name}: partial 步骤少于 {expect['partial_at_least']}")
    if expect.get("keep_artifacts"):
        a = r.get("artifacts") or {}
        c.check(bool(a.get("materials")) and bool(a.get("script")),
                f"{name}: 未完成时没有保留素材/脚本")

    if name == "case-12-resume-after-save-failure":
        start = (r.get("resume") or {}).get("from_step", 0)
        after = [s["action"] for s in steps if s["step"] > start]
        c.check(after == ["save_plan", "finish"],
                f"{name}: 续跑后动作应为 save_plan, finish，实际 {after}")
        c.check(sum(1 for s in steps if s["action"] == "search_materials") == 1,
                f"{name}: 续跑重检了素材")


def main() -> int:
    c = Checker()

    for rel in REQUIRED:
        c.check((ROOT / rel).exists(), f"缺少规范文件 {rel}")

    # 文档占位符 / 失效引用
    for doc in (ROOT / "docs").glob("*.md"):
        text = doc.read_text(encoding="utf-8")
        c.check("[待本人确认]" not in text, f"{doc.name}: 仍有 [待本人确认] 占位符")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    c.check("https://github.com/thynan2002/XiaoYing-GuoHaoyu-AIAgent-Answer" in readme,
            "README 缺少仓库 URL")
    c.check("内容任务规划 Agent" in readme, "README 缺少题号")

    test_out = (EX / "tests-output.txt").read_text(encoding="utf-8")
    m = re.search(r"(\d+) passed", test_out)
    c.check(bool(m), "examples/tests-output.txt 没有 passed 记录")
    if m:
        c.check(int(m.group(1)) >= 40, f"测试数量异常：{m.group(1)}")

    for name, expect in EXPECT.items():
        check_case(c, name, expect)

    c.check(len(list(EX.glob("case-*"))) == len(EXPECT),
            f"examples 下用例数不是 {len(EXPECT)}")

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(main())
