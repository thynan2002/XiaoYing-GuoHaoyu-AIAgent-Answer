"""Agent 主循环：决策 → 执行工具 → 观察 → 再决策，直到完成 / 缺口 / 上限。

工程侧的三道闸门（模型绕不过去）：
1. 步数与工具调用次数硬上限，到顶就返回 INCOMPLETE_LIMIT；
2. 收尾校验：声称 COMPLETED 但没有落盘方案、或费用超预算，一律降级为未完成；
3. 每步落检查点（state.json），中断后可从断点续跑，已成功的步骤不重跑。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import config
from .models import Task
from .planner import DecisionContext, PlannerError
from .tools import ToolRegistry

ALLOWED_STATUS = {
    "COMPLETED",
    "NEED_INPUT",
    "INCOMPLETE_BUDGET",
    "INCOMPLETE_TOOL_FAILURE",
    "INCOMPLETE_SAVE",
    "INCOMPLETE_LIMIT",
    "INCOMPLETE_PLANNER",
    "INCOMPLETE_INCONSISTENT",
}


class Agent:
    def __init__(self, planner, registry: ToolRegistry, out_dir: Path,
                 run_id: str = "run", max_steps: int = config.DEFAULT_MAX_STEPS,
                 max_tool_calls: int = config.DEFAULT_MAX_TOOL_CALLS):
        self.planner = planner
        self.registry = registry
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls

    # ------------------------------------------------------------ 状态
    def _new_state(self, task: Task) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "planner": self.planner.name,
            "task": task.to_dict(),
            "step": 0,
            "tool_calls": 0,
            "scratch": {},          # 规划器工作区（含 rejected / chosen / materials ...）
            "status": None,
            "status_reason": "",
            "steps": [],
        }

    def _checkpoint(self, state: dict) -> None:
        path = self.out_dir / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------ 主循环
    def run(self, task: Task, resume_state: dict | None = None) -> dict[str, Any]:
        state = resume_state or self._new_state(task)
        if resume_state:
            # 续跑：以调用方给的（已合并 --set 的）任务为准，其余进度原样保留
            state["task"] = task.to_dict()
            state["run_id"] = self.run_id
            state["planner"] = self.planner.name
        else:
            state["task"] = {**state["task"], "available_minutes": task.available_minutes}
        started = time.time()
        last_action: str | None = None
        last_obs = None
        same_fail = 0
        resumed_from = state["step"]

        while True:
            # 闸门 1：步数上限
            if state["step"] >= self.max_steps:
                return self._finish(state, started,
                                    "INCOMPLETE_LIMIT",
                                    f"达到最大步数 {self.max_steps}，任务未完成；{self._kept(state)}",
                                    basis="步数用尽，按约定返回未完成")

            ctx = DecisionContext(task=task, scratch=state["scratch"],
                                  last_action=last_action, last_obs=last_obs,
                                  step=state["step"] + 1, same_tool_failures=same_fail)
            t0 = time.time()
            try:
                action = self.planner.decide(ctx)
            except PlannerError as e:
                return self._finish(state, started, "INCOMPLETE_PLANNER",
                                    f"规划器异常：{e}", basis="模型不可用，安全收尾")

            state["step"] += 1
            record = {
                "step": state["step"],
                "action": action.action,
                "args": action.args,
                "decision_basis": action.decision_basis,
                "observation": None,
                "elapsed_ms": 0,
                "follow_up": "",
            }

            # 终止型动作
            if action.action == "finish":
                status = str(action.args.get("status", "")).upper()
                reason = str(action.args.get("reason", ""))
                record["observation"] = {"status": "terminal", "summary": f"{status}：{reason}"}
                state["steps"].append(record)
                if status not in ALLOWED_STATUS:
                    return self._finish(state, started, "INCOMPLETE_PLANNER",
                                        f"模型给出非法状态 {status!r}", basis="状态非法，安全收尾")
                return self._finish(state, started, status, reason, basis=action.decision_basis)

            if action.action == "request_input":
                missing = action.args.get("missing_fields") or []
                record["observation"] = {"status": "terminal",
                                         "summary": f"向运营索要：{', '.join(missing)}"}
                state["steps"].append(record)
                return self._finish(state, started, "NEED_INPUT",
                                    f"缺少必要信息：{', '.join(missing)}；"
                                    f"补齐后可用 resume 从检查点续跑",
                                    basis=action.decision_basis)

            # 闸门 2：工具调用次数上限
            if state["tool_calls"] >= self.max_tool_calls:
                record["observation"] = {"status": "blocked", "summary": "工具调用次数已达上限"}
                state["steps"].append(record)
                return self._finish(state, started, "INCOMPLETE_LIMIT",
                                    f"达到最大工具调用次数 {self.max_tool_calls}，任务未完成",
                                    basis="工具调用配额用尽")

            # 执行工具
            obs = self.registry.call(action.action, action.args)
            state["tool_calls"] += 1
            last_action, last_obs = action.action, obs
            same_fail = same_fail + 1 if obs.status == "error" else 0

            record["observation"] = obs.to_dict()
            state["last_observation"] = obs.to_dict()
            record["elapsed_ms"] = int((time.time() - t0) * 1000)
            state["steps"].append(record)
            self._checkpoint(state)

        # 不会到这里
        return self._finish(state, started, "INCOMPLETE_PLANNER", "循环异常退出",
                            basis="兜底")

    # ------------------------------------------------------------ 收尾
    @staticmethod
    def _kept(state: dict) -> str:
        """尚未完成时，把"已经拿到的成果"讲清楚，避免看起来像什么都没做。"""
        sc = state["scratch"]
        parts: list[str] = []
        est = sc.get("estimate") or {}
        if est:
            parts.append(f"已选定{est.get('label')}、估算 {est.get('total')}")
        mats = sc.get("materials") or []
        if mats:
            parts.append(f"已检索 {len(mats)} 条素材")
        if sc.get("script"):
            parts.append("已生成脚本")
        if sc.get("saved"):
            parts.append(f"已保存 {sc['saved'].get('plan_id')}")
        if sc.get("rejected"):
            parts.append(f"已否 {len(sc['rejected'])} 个方案")
        return "已保留：" + "、".join(parts) if parts else "尚无可用中间产物"

    def _finish(self, state: dict, started: float, status: str, reason: str,
                basis: str = "") -> dict[str, Any]:
        scratch = state["scratch"]
        task = state["task"]

        # 闸门 3：声称完成必须真的完成
        if status == "COMPLETED":
            saved = scratch.get("saved") or {}
            est = scratch.get("estimate") or {}
            problems = []
            if not saved.get("plan_id"):
                problems.append("没有落盘方案")
            if not scratch.get("script"):
                problems.append("没有脚本")
            budget = task.get("budget")
            if budget is not None and est and float(est.get("total", 0)) > float(budget) + 1e-9:
                problems.append(f"费用 {est.get('total')} 超预算 {budget}")
            if problems:
                status = "INCOMPLETE_INCONSISTENT"
                reason = "模型声称完成但校验不通过：" + "；".join(problems)

        if status not in ("COMPLETED", "NEED_INPUT") and "已保留" not in reason:
            reason = f"{reason}；{self._kept(state)}"

        # 回填 follow_up，便于阅读"观察→下一步"
        steps = state["steps"]
        for i, s in enumerate(steps[:-1]):
            s["follow_up"] = steps[i + 1]["action"]
        if steps:
            steps[-1]["follow_up"] = "结束" if status != "NEED_INPUT" else "等待运营补充信息"

        state["status"] = status
        state["status_reason"] = reason
        state["steps"] = steps
        self._checkpoint(state)

        estimate = scratch.get("estimate") or {}
        saved = scratch.get("saved") or {}
        materials = scratch.get("materials") or []
        elapsed_ms = int((time.time() - started) * 1000)

        plan = None
        if estimate:
            plan = {
                "variant_id": estimate.get("variant_id"),
                "label": estimate.get("label"),
                "cost_total": estimate.get("total"),
                "currency": estimate.get("currency"),
                "estimated_minutes": estimate.get("estimated_minutes"),
                "breakdown": estimate.get("breakdown", []),
                "material_ids": scratch.get("material_ids") or [],
                "script_title": (scratch.get("script") or {}).get("title"),
            }

        result = {
            "run_id": state["run_id"],
            "planner": state["planner"],
            "status": status,
            "status_reason": reason,
            "task": task,
            "plan": plan,
            "rejected": scratch.get("rejected", []),
            "last_observation": state.get("last_observation"),
            "saved_plan": {
                "plan_id": saved.get("plan_id"),
                "path": saved.get("path"),
                "deduped": bool(saved.get("deduped")),
            } if saved else None,
            "artifacts": {
                "materials": [{"id": m["id"], "title": m["title"]} for m in materials],
                "script": scratch.get("script"),
                "script_status": scratch.get("script_status"),
            },
            "usage": {
                "steps": state["step"],
                "max_steps": self.max_steps,
                "tool_calls": state["tool_calls"],
                "max_tool_calls": self.max_tool_calls,
                "elapsed_ms": elapsed_ms,
            },
        }

        trace = {
            "run_id": state["run_id"],
            "planner": state["planner"],
            "status": status,
            "status_reason": reason,
            "task": task,
            "limits": {"max_steps": self.max_steps, "max_tool_calls": self.max_tool_calls},
            "steps": steps,
        }
        (self.out_dir / "trace.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.out_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        # 把落盘的方案复制一份到输出目录，方便评审直接查看
        path = saved.get("path")
        if path and Path(path).exists():
            (self.out_dir / "saved_plan.json").write_text(
                Path(path).read_text(encoding="utf-8"), encoding="utf-8")

        return result


def _task_from_dict(d: dict) -> Task:
    return Task(
        topic=d.get("topic", ""),
        budget=d.get("budget"),
        deadline=d.get("deadline", ""),
        tone=d.get("tone", "轻快"),
        audience=d.get("audience", "泛用户"),
        notes=d.get("notes", ""),
        available_minutes=d.get("available_minutes"),
    )
