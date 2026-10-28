"""命令行入口。

    python -m src.cli run    --input examples/inputs/01-normal.json --out examples/case-01-normal
    python -m src.cli resume --state examples/case-02-missing-input/state.json --set budget=10 --out examples/case-03-resume
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import config
from .agent import Agent
from .models import Task
from .planner import PlannerError, build_planner
from .tools import ToolRegistry

EXIT = {"COMPLETED": 0, "NEED_INPUT": 3}


def _read_task(args) -> Task:
    data: dict = {}
    if args.input:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    for key in ("topic", "budget", "deadline", "tone", "audience", "notes"):
        val = getattr(args, key, None)
        if val not in (None, ""):
            data[key] = val
    return _make_task(data, args.now)


def _make_task(data: dict, now: str | None) -> Task:
    task = Task(
        topic=str(data.get("topic") or ""),
        budget=None if data.get("budget") in (None, "") else float(data["budget"]),
        deadline=str(data.get("deadline") or ""),
        tone=str(data.get("tone") or "轻快"),
        audience=str(data.get("audience") or "泛用户"),
        notes=str(data.get("notes") or ""),
    )
    task.available_minutes = _available_minutes(task.deadline, now)
    return task


def _available_minutes(deadline: str, now: str | None) -> float | None:
    """可用工时 = 距截止时间 − 缓冲。截止时间为空则返回 None。"""
    if not deadline:
        return None
    fmt = "%Y-%m-%d %H:%M"
    try:
        dl = datetime.strptime(deadline.strip()[:16], fmt)
    except ValueError:
        return None
    cur = datetime.strptime(now, fmt) if now else datetime.now().replace(second=0, microsecond=0)
    minutes = (dl - cur).total_seconds() / 60
    if minutes <= 0:
        return 0.0
    buffer = config.load_costs()["tool_time"]["hour_buffer_minutes"]
    usable = minutes - buffer if minutes > buffer else minutes
    return round(usable, 1)


def _print_run(result: dict, trace_path: Path | None = None) -> None:
    print("=" * 78)
    print(f"run_id : {result['run_id']}     planner: {result['planner']}")
    t = result["task"]
    print(f"需求   : {t.get('topic')}｜预算 {t.get('budget')}｜截止 {t.get('deadline')}"
          f"｜可用 {t.get('available_minutes')} 分钟")
    print("-" * 78)
    for step in _steps_for_print(result, trace_path):
        print(f"[{step['step']:>2}] {step['action']}({_fmt_args(step['args'])})")
        print(f"     依据 : {step['decision_basis']}")
        obs = step.get("observation") or {}
        print(f"     观察 : {obs.get('status')} | {obs.get('summary', obs)}")
        print(f"     之后 : {step.get('follow_up')}")
    print("-" * 78)
    plan = result.get("plan")
    if plan:
        print(f"选定方案: {plan['label']}（{plan['variant_id']}）"
              f"费用 {plan['cost_total']} {plan['currency']}，约 {plan['estimated_minutes']} 分钟")
        for b in plan["breakdown"]:
            print(f"   - {b['item']:<8} {b['amount']:>6}  ({b['detail']})")
    if result.get("rejected"):
        print("被否方案:")
        for r in result["rejected"]:
            print(f"   - {r['variant']}: {r['reason']}")
    if result.get("saved_plan"):
        sp = result["saved_plan"]
        print(f"落盘方案: {sp['plan_id']} -> {sp['path']}"
              + ("（幂等命中，未重复写入）" if sp.get("deduped") else ""))
    u = result["usage"]
    print(f"用量   : {u['steps']}/{u['max_steps']} 步，{u['tool_calls']}/{u['max_tool_calls']} 次工具调用")
    print(f"状态   : {result['status']} — {result['status_reason']}")
    print("=" * 78)


def _steps_for_print(result: dict, trace_path: Path | None = None) -> list[dict]:
    if trace_path and Path(trace_path).exists():
        return json.loads(Path(trace_path).read_text(encoding="utf-8"))["steps"]
    return result.get("steps", [])


def _fmt_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        if isinstance(v, (list, dict)):
            v = f"<{type(v).__name__}:{len(v)}>"
        if isinstance(v, str) and len(v) > 28:
            v = v[:28] + "…"
        parts.append(f"{k}={v}")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    p = argparse.ArgumentParser(prog="src.cli", description="内容活动方案 Agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--planner", choices=["mock", "llm"], default="mock",
                        help="mock=离线策略规划器（默认）；llm=真实模型，需要 OPENAI_API_KEY")
        sp.add_argument("--max-steps", type=int, default=config.DEFAULT_MAX_STEPS)
        sp.add_argument("--max-tool-calls", type=int, default=config.DEFAULT_MAX_TOOL_CALLS)
        sp.add_argument("--fail-tool", action="append", default=[],
                        help="注入故障的工具名，可重复")
        sp.add_argument("--fail-times", type=int, default=1, help="每个注入工具失败次数")
        sp.add_argument("--fail-mode", choices=["error", "timeout", "partial"], default="error")
        sp.add_argument("--out", required=True, help="输出目录（result.json / trace.json / state.json）")
        sp.add_argument("--run-id", default="")
        sp.add_argument("--plan-dir", default=str(config.DEFAULT_PLAN_DIR),
                        help="方案落盘目录（幂等去重按此目录判重）")
        sp.add_argument("--now", default="", help="把'现在'固定为指定时间，便于复现（YYYY-MM-DD HH:MM）")
        sp.add_argument("--json", action="store_true", help="额外把 result.json 打印到 stdout")
        sp.add_argument("--quiet", action="store_true")

    r = sub.add_parser("run", help="执行一次任务")
    r.add_argument("--input", help="任务输入 JSON")
    r.add_argument("--topic"); r.add_argument("--budget", type=float)
    r.add_argument("--deadline"); r.add_argument("--tone"); r.add_argument("--audience")
    add_common(r)

    s = sub.add_parser("resume", help="从检查点续跑（补齐缺失信息 / 工具恢复后）")
    s.add_argument("--state", required=True, help="上一次运行的 state.json")
    s.add_argument("--set", action="append", default=[], help="覆盖字段，如 --set budget=10")
    add_common(s)

    args = p.parse_args(argv)

    if args.cmd == "run":
        task = _read_task(args)
        state = None
    else:
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
        data = dict(state["task"])
        for item in args.set:
            if "=" not in item:
                raise SystemExit(f"--set 需要 key=value，收到 {item!r}")
            k, v = item.split("=", 1)
            data[k.strip()] = v.strip()
        task = _make_task(data, args.now)
        # 续跑时清掉上一次的终态，保留 scratch / 已成功产物
        state["status"] = None
        state["status_reason"] = ""

    inject = {name: {"times": args.fail_times, "mode": args.fail_mode} for name in args.fail_tool}
    registry = ToolRegistry(plan_dir=Path(args.plan_dir), inject=inject)

    try:
        planner = build_planner(args.planner)
    except PlannerError as e:
        print(f"[退出] {e}", file=sys.stderr)
        return 5

    run_id = args.run_id or Path(args.out).name or "run"
    agent = Agent(planner, registry, out_dir=Path(args.out), run_id=run_id,
                  max_steps=args.max_steps, max_tool_calls=args.max_tool_calls)
    result = agent.run(task, resume_state=state)
    trace_path = Path(args.out) / "trace.json"

    if not args.quiet:
        _print_run(result, trace_path)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT.get(result["status"], 4)


if __name__ == "__main__":
    raise SystemExit(main())
