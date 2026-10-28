"""Agent 主循环：决策 → 执行工具 → 观察 → 再决策，直到完成 / 缺口 / 上限。

工程侧的四道闸门（模型绕不过去）：
1. 步数与工具调用次数硬上限，到顶就返回 INCOMPLETE_LIMIT；
2. 收尾校验：声称 COMPLETED 但没有落盘方案、没有脚本、没有有效报价，
   或费用/工时超限，一律降级为未完成；
3. 状态白名单：模型给出未知状态直接判 INCOMPLETE_PLANNER；
4. 每步落检查点（state.json），中断后可从断点续跑，已成功的步骤不重跑。

另外，Agent 自己维护 `progress`（每类工具最近一次成功返回）与 `rejected_auto`
（从真实工具返回值推出的被否候选）两个台账。它们不依赖规划器实现，所以 MockPlanner
与 LLMPlanner 的交付口径一致：换模型不会把"被否方案"和"已完成工序"弄丢。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import config
from .models import Observation, Task
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
            "scratch": {},          # 规划器工作区（含 queue / chosen / materials ...）
            "rejected_auto": [],    # Agent 从工具返回值推出的被否候选，跨规划器通用
            "status": None,
            "status_reason": "",
            "steps": [],
        }

    def _checkpoint(self, state: dict) -> None:
        path = self.out_dir / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------ 台账
    @staticmethod
    def _merged_rejected(state: dict) -> list[dict]:
        """合并两本账：规划器自己记的（含工具失败原因）+ Agent 自动记的（含费用/工时数字）。

        规划器记的优先：同一档位它知道更具体的原因（如"脚本连续失败"），
        Agent 不再用"费用超预算"覆盖。
        """
        sc = state.get("scratch") or {}
        manual = [dict(r) for r in (sc.get("rejected") or []) if r.get("variant")]
        seen = {r["variant"] for r in manual}
        auto = [dict(r) for r in (state.get("rejected_auto") or [])
                if r.get("variant") and r["variant"] not in seen]
        return manual + auto

    @staticmethod
    def _budget_avail(task) -> tuple[float | None, float | None]:
        if isinstance(task, dict):
            return task.get("budget"), task.get("available_minutes")
        return task.budget, task.available_minutes

    @classmethod
    def _estimate_feasible(cls, est: dict, task) -> bool:
        if not est or not est.get("variant_id"):
            return False
        budget, avail = cls._budget_avail(task)
        total = float(est.get("total", 0))
        minutes = int(est.get("estimated_minutes", 0))
        return ((budget is None or total <= float(budget) + 1e-9)
                and (avail is None or minutes <= float(avail)))

    @staticmethod
    def _record_estimate_rejection(state: dict, est: dict, task) -> None:
        """估算结果一出来就按预算/工时判一次，不依赖模型自己记账。"""
        if not est or not est.get("variant_id"):
            return
        total = float(est.get("total", 0))
        minutes = int(est.get("estimated_minutes", 0))
        budget, avail = Agent._budget_avail(task)
        why: list[str] = []
        if budget is not None and total > float(budget) + 1e-9:
            why.append(f"费用 {total} 超预算 {budget}")
        if avail is not None and minutes > float(avail):
            why.append(f"耗时 {minutes} 分钟超过可用 {avail} 分钟")
        if not why:
            return
        entry = {"variant": est["variant_id"], "reason": "；".join(why),
                 "cost": total, "minutes": minutes}
        auto = state.setdefault("rejected_auto", [])
        if not any(e.get("variant") == entry["variant"] and e.get("reason") == entry["reason"]
                   for e in auto):
            auto.append(entry)
        # 如果被否的正是当前"已选定"的档位（例如素材变少后重算反而超限），
        # 取消选定，避免交付结果里残留一个不可行的 plan。
        if state.get("chosen_variant") == entry["variant"]:
            state["chosen_variant"] = None

    @staticmethod
    def _record_tool_failure(state: dict, action_name: str, same_fail: int) -> None:
        """同一工具连续失败 2 次时，把当前档位记为被否——换成 LLM 规划器也有同样留痕。"""
        if same_fail < 2:
            return
        variant = state.get("chosen_variant")
        if not variant:
            return
        progress = (state.get("scratch") or {}).get("progress") or {}
        est = progress.get("estimate_cost") or {}
        entry = {
            "variant": variant,
            "reason": f"{action_name} 连续失败 {same_fail} 次，放弃该方案",
            "cost": est.get("total") if est.get("variant_id") == variant else None,
        }
        auto = state.setdefault("rejected_auto", [])
        if not any(e.get("variant") == entry["variant"] and e.get("reason") == entry["reason"]
                   for e in auto):
            auto.append(entry)

    # ------------------------------------------------------------ 主循环
    def run(self, task: Task, resume_state: dict | None = None) -> dict[str, Any]:
        state = resume_state or self._new_state(task)
        if resume_state:
            # 续跑：以调用方给的（已合并 --set 的）任务为准，其余进度原样保留
            state["task"] = task.to_dict()
            state["run_id"] = self.run_id
            state["planner"] = self.planner.name
            state.setdefault("rejected_auto", [])
            # 人工介入 = 新的重试窗口：把"同一工具连续失败"计数清零，
            # 否则续跑会立刻再次进入"降级/收尾"，等于没续跑。
            scratch = state.setdefault("scratch", {})
            for key in [k for k in scratch if k.endswith("_fail")]:
                scratch[key] = 0
        else:
            state["task"] = {**state["task"], "available_minutes": task.available_minutes}
        state["resumed_from"] = state["step"]

        started = time.time()
        # 续跑时恢复"上一次动作/观察"，让规划器从断点接着判断，而不是从头再来
        last_action: str | None = state.get("last_action")
        last_obs = Observation(**state["last_observation"]) if state.get("last_observation") else None
        same_fail = 0

        while True:
            # 闸门 1：步数上限
            if state["step"] >= self.max_steps:
                return self._finish(state, started,
                                    "INCOMPLETE_LIMIT",
                                    f"达到最大步数 {self.max_steps}，任务未完成；{self._kept(state)}",
                                    basis="步数用尽，按约定返回未完成")

            ctx = DecisionContext(task=task, scratch=state["scratch"],
                                  last_action=last_action, last_obs=last_obs,
                                  step=state["step"] + 1, same_tool_failures=same_fail,
                                  chosen_variant=state.get("chosen_variant"),
                                  rejected=self._merged_rejected(state))
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
            state["last_action"] = action.action

            # 与规划器无关的"已完成工序"台账：谁做规划都能读到，也便于续跑与出报告
            if obs.status in ("ok", "partial"):
                state.setdefault("scratch", {}).setdefault("progress", {})[action.action] = obs.data

            # 模型"选定"的方案：优先从工具参数里记；search_materials 的契约里没有
            # variant_id，就从上一次"预算/工时都可行"的估算结果推，否则 LLM 路径
            # 在脚本失败时不知道自己在做哪一档。
            # 只有工具成功/部分成功才算选定；失败调用不能把 chosen 改到别的档位上。
            vid = (action.args or {}).get("variant_id")
            if action.action == "search_materials":
                last_est = (state["scratch"].get("progress") or {}).get("estimate_cost") or {}
                if self._estimate_feasible(last_est, task):
                    vid = vid or last_est.get("variant_id")
            if (vid and obs.status in ("ok", "partial")
                    and action.action in ("search_materials", "generate_script", "save_plan")):
                state["chosen_variant"] = vid

            # 费用/工时越界由 Agent 自己记账；工具连续失败也留一条否决原因
            if action.action == "estimate_cost" and obs.status == "ok":
                self._record_estimate_rejection(state, obs.data or {}, task)
            if obs.status == "error":
                self._record_tool_failure(state, action.action, same_fail)

            record["elapsed_ms"] = int((time.time() - t0) * 1000)
            state["steps"].append(record)
            self._checkpoint(state)

        # 不会到这里
        return self._finish(state, started, "INCOMPLETE_PLANNER", "循环异常退出",
                            basis="兜底")

    # ------------------------------------------------------------ 收尾
    @staticmethod
    def _artifacts(state: dict) -> dict[str, Any]:
        """统一口径读取"已拿到的成果"。

        进度有两个来源：规划器自己的 scratch（mock 用）与 Agent 维护的 progress 台账
        （任何规划器都有）。收尾校验、报告、续跑都从这里取，避免"换规划器就判不出完成"。
        """
        sc = state.get("scratch") or {}
        prog = sc.get("progress") or {}
        script = sc.get("script") or (prog.get("generate_script") or {}).get("script")
        materials = sc.get("materials") or (prog.get("search_materials") or {}).get("items") or []
        material_ids = sc.get("material_ids") or [m["id"] for m in materials if "id" in m]
        # 只有"被选中的方案"的估算才算方案；被预算否掉的候选不能冒充交付方案
        chosen = state.get("chosen_variant") or sc.get("chosen")
        est_all = prog.get("estimate_cost") or {}
        estimate = sc.get("estimate") or (est_all if est_all.get("variant_id") == chosen else {})
        # 被否清单里去掉最终选中的那一档（它后来可能重新核算并可行）
        rejected = [r for r in Agent._merged_rejected(state) if r.get("variant") != chosen]
        return {
            "estimate": estimate,
            "saved": sc.get("saved") or (prog.get("save_plan") or {}),
            "script": script,
            "materials": materials,
            "material_ids": material_ids,
            "rejected": rejected,
        }

    @classmethod
    def _kept(cls, state: dict) -> str:
        """尚未完成时，把"已经拿到的成果"讲清楚，避免看起来像什么都没做。"""
        a = cls._artifacts(state)
        parts: list[str] = []
        est = a["estimate"]
        if est:
            parts.append(f"已选定{est.get('label')}、估算 {est.get('total')}")
        if a["materials"]:
            parts.append(f"已检索 {len(a['materials'])} 条素材")
        if a["script"]:
            parts.append("已生成脚本")
        if a["saved"]:
            parts.append(f"已保存 {a['saved'].get('plan_id')}")
        if a["rejected"]:
            parts.append(f"已否 {len(a['rejected'])} 个方案")
        return "已保留：" + "、".join(parts) if parts else "尚无可用中间产物"

    def _finish(self, state: dict, started: float, status: str, reason: str,
                basis: str = "") -> dict[str, Any]:
        scratch = state["scratch"]
        task = state["task"]
        a = self._artifacts(state)

        # 闸门 3：声称完成必须真的完成，而且费用/工时都落在约束内
        if status == "COMPLETED":
            est = a["estimate"]
            problems: list[str] = []
            if not a["saved"].get("plan_id"):
                problems.append("没有落盘方案")
            if not a["script"]:
                problems.append("没有脚本")
            if not est:
                problems.append("没有有效的费用估算")
            budget = task.get("budget")
            if budget is not None and est and float(est.get("total", 0)) > float(budget) + 1e-9:
                problems.append(f"费用 {est.get('total')} 超预算 {budget}")
            avail = task.get("available_minutes")
            if (avail is not None and est
                    and int(est.get("estimated_minutes", 0)) > float(avail)):
                problems.append(f"工时 {est.get('estimated_minutes')} 分钟超过可用 {avail} 分钟")
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

        estimate = a["estimate"]
        saved = a["saved"]
        materials = a["materials"]
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
                "outline": estimate.get("outline", []),
                "material_ids": a["material_ids"],
                "script_title": (a["script"] or {}).get("title"),
            }

        result = {
            "run_id": state["run_id"],
            "planner": state["planner"],
            "status": status,
            "status_reason": reason,
            "task": task,
            "plan": plan,
            "rejected": a["rejected"],
            "last_observation": state.get("last_observation"),
            "saved_plan": {
                "plan_id": saved.get("plan_id"),
                "path": saved.get("path"),
                "deduped": bool(saved.get("deduped")),
            } if saved else None,
            "artifacts": {
                "materials": [{"id": m["id"], "title": m["title"]} for m in materials],
                "script": a["script"],
                "script_status": scratch.get("script_status"),
            },
            "usage": {
                "steps": state["step"],
                "max_steps": self.max_steps,
                "tool_calls": state["tool_calls"],
                "max_tool_calls": self.max_tool_calls,
                "elapsed_ms": elapsed_ms,
            },
            # 逐步状态摘要：交付结果里要能直接看到"哪一步成功/失败/被拒"
            "steps_brief": [
                {"step": s["step"], "action": s["action"],
                 "status": (s.get("observation") or {}).get("status"),
                 "summary": (s.get("observation") or {}).get("summary")}
                for s in steps
            ],
            "resume": {
                "resumed": bool(state.get("resumed_from")),
                "from_step": state.get("resumed_from", 0),
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
