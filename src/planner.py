"""规划器：把「上一步的观察」变成「下一步的动作」。

两个实现共享同一接口：
- LLMPlanner：真实模型（OpenAI 兼容 /chat/completions），默认不启用。
- MockPlanner：离线策略规划器，用于无密钥环境跑通流程与回归测试。
  **它读的是工具真实返回值**（费用、错误、部分成功），所以能演示"观察→调整"，
  但它不是大模型，不能用来证明真实模型的决策质量。这一点在 docs/03 里写明。
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import config
from .models import Action, Observation, Task
from .tools import tool_specs_text


# ------------------------------------------------------------------ 上下文

@dataclass
class DecisionContext:
    task: Task
    scratch: dict[str, Any]
    last_action: str | None
    last_obs: Observation | None
    step: int
    same_tool_failures: int = 0


class PlannerError(Exception):
    pass


# ------------------------------------------------------------------ 离线 mock

class MockPlanner:
    """确定性策略规划器：每一步都读取真实观察，再决定动作。

    策略优先级：缺输入 → 列方案 → 逐个估算（超预算/超时就换下一个）
    → 检索素材 → 生成脚本 → 保存；任一步失败则重试 1 次，再失败就换更便宜的方案或收尾。
    """

    name = "mock-policy"
    is_mock = True

    # -- 工具选择辅助 -------------------------------------------------
    @staticmethod
    def _finish(status: str, reason: str, basis: str) -> Action:
        return Action("finish", {"status": status, "reason": reason}, basis)

    def _next_estimate(self, ctx: DecisionContext, basis_prefix: str = "") -> Action:
        s = ctx.scratch
        queue: list[str] = s.get("queue", [])
        if not queue:
            rejected = s.get("rejected", [])
            cheapest = min([r["cost"] for r in rejected if r.get("cost") is not None],
                           default=None)
            gap = None
            if cheapest is not None and ctx.task.budget is not None:
                gap = round(cheapest - float(ctx.task.budget), 2)
            detail = "；".join(f"{r['variant']}:{r['reason']}" for r in rejected) or "无候选"
            return self._finish(
                "INCOMPLETE_BUDGET",
                (f"没有可行方案（预算 {ctx.task.budget}，"
                 f"最低方案 {cheapest}，缺口 {gap}）：{detail}"),
                f"所有候选都不满足预算/时间，返回未完成，缺口 {gap}",
            )
        vid = queue[0]
        args: dict[str, Any] = {"variant_id": vid}
        if s.get("material_ids") and s.get("chosen") == vid:
            args["material_ids"] = s["material_ids"]
        return Action("estimate_cost", args,
                      f"{basis_prefix}核算 {vid} 的费用看是否落在预算内")

    def decide(self, ctx: DecisionContext) -> Action:
        t, s, obs = ctx.task, ctx.scratch, ctx.last_obs

        # --- 1. 缺关键信息：不猜，直接要
        missing = t.missing_fields()
        if missing:
            s["requested_input"] = missing
            return Action("request_input", {"missing_fields": missing},
                          f"缺少 {', '.join(missing)}，无法核算预算与工期，先向运营确认")

        budget = float(t.budget) if t.budget is not None else math.inf
        avail = t.available_minutes if t.available_minutes is not None else math.inf

        # --- 2. 第一步：拿候选
        if ctx.last_action is None:
            return Action("list_plan_variants", {"topic": t.topic},
                          "先取候选方案列表，费用必须逐项现算，不凭印象报价")

        # --- 3. 观察后调整
        if ctx.last_action == "list_plan_variants":
            if obs is None or obs.status == "error":
                return self._finish("INCOMPLETE_TOOL_FAILURE",
                                    f"方案候选服务不可用：{obs.error if obs else '无返回'}",
                                    "拿不到候选方案，无法继续")
            variants = obs.data.get("variants", [])
            s["queue"] = [v["variant_id"] for v in variants]
            s["labels"] = {v["variant_id"]: v["label"] for v in variants}
            return self._next_estimate(ctx, "先按完整度优先，")

        if ctx.last_action == "estimate_cost":
            est = obs.data if obs else {}
            vid = est.get("variant_id") or (s.get("queue") or ["?"])[0]
            if obs is None or obs.status == "error":
                if s.get("queue"):
                    s["queue"].pop(0)
                s.setdefault("rejected", []).append(
                    {"variant": vid, "reason": "估算失败", "cost": None})
                return self._next_estimate(ctx, "估算失败，")
            total = float(est["total"])
            minutes = int(est["estimated_minutes"])
            s["last_estimate"] = est
            if total <= budget and minutes <= avail:
                if s.get("queue") and s["queue"][0] == vid:
                    s["queue"].pop(0)
                s["chosen"] = vid
                s["estimate"] = est
                if s.get("material_ids") and s.get("materials"):
                    # 素材已经被检索过（费用因素材变少而重算），直接写脚本
                    return Action("generate_script",
                                  {"variant_id": vid, "material_ids": s["material_ids"],
                                   "tone": t.tone, "topic": t.topic},
                                  f"重算后 {total} ≤ 预算 {budget}，用现有 {len(s['material_ids'])} 条素材写脚本")
                return Action("search_materials",
                              {"queries": self._queries(t), "limit": est["material_items"]},
                              f"{est['label']} {total} ≤ 预算 {budget}、约 {minutes} 分钟可交付，选它并检索素材")
            # 不满足：记录原因，换下一个
            why = (f"费用 {total} 超预算 {budget}" if total > budget
                   else f"耗时 {minutes} 分钟超过可用 {avail} 分钟")
            if s.get("queue") and s["queue"][0] == vid:
                s["queue"].pop(0)
            s.setdefault("rejected", []).append(
                {"variant": vid, "reason": why, "cost": total, "minutes": minutes})
            return self._next_estimate(ctx, f"{est['label']}被否（{why}），")

        if ctx.last_action == "search_materials":
            return self._after_search(ctx, obs)

        if ctx.last_action == "generate_script":
            if obs is not None and obs.status in ("ok", "partial"):
                s["script"] = obs.data.get("script")
                s["script_status"] = obs.status
                return self._save_action(ctx, f"脚本已就绪（{obs.status}），保存方案")
            # 失败：先原地重试 1 次，再降级
            fails = s.get("script_fail", 0) + 1
            s["script_fail"] = fails
            if fails <= config.MAX_SAME_TOOL_RETRY:
                return Action("generate_script",
                              {"variant_id": s["chosen"], "material_ids": s.get("material_ids", []),
                               "tone": t.tone, "topic": t.topic},
                              f"脚本工具失败（{obs.error if obs else '无返回'}），参数不变重试第 {fails} 次")
            if s.get("queue"):
                s["degrade_from_label"] = (s.get("estimate") or {}).get("label", s.get("chosen"))
                s.setdefault("rejected", []).append({
                    "variant": s.get("chosen"),
                    "reason": f"脚本工具连续失败 {fails} 次，放弃该方案",
                    "cost": (s.get("estimate") or {}).get("total"),
                })
                s["chosen"] = None
                s["material_ids"] = None
                s["materials"] = None
                s["script"] = None
                return self._next_estimate(
                    ctx, f"脚本连续失败 {fails} 次，放弃{s.get('degrade_from_label')}，降级到更简单方案并")
            return self._finish(
                "INCOMPLETE_TOOL_FAILURE",
                f"脚本生成连续失败 {fails} 次且无更简单方案可退：{obs.error if obs else ''}",
                "工具不可用且无可替代方案，保留已完成的估算与素材结果后收尾")

        if ctx.last_action == "save_plan":
            if obs is not None and obs.status == "ok":
                s["saved"] = obs.data
                note = "（幂等命中，未重复写入）" if obs.data.get("deduped") else ""
                return self._finish("COMPLETED",
                                    f"方案已保存 {obs.data.get('plan_id')}{note}",
                                    "方案落盘成功，任务完成")
            fails = s.get("save_fail", 0) + 1
            s["save_fail"] = fails
            if fails <= config.MAX_SAME_TOOL_RETRY:
                return self._save_action(ctx, f"保存失败（{obs.error if obs else ''}），重试 1 次")
            return self._finish(
                "INCOMPLETE_SAVE",
                f"方案保存失败 {fails} 次：{obs.error if obs else ''}；脚本与费用已保留",
                "落盘失败但前序成果有效，返回未完成并保留结果")

        return self._finish("INCOMPLETE_PLANNER",
                            f"规划器无法处理上一个动作 {ctx.last_action}",
                            "出现未预期状态，安全收尾")

    # -- 子流程 ------------------------------------------------------
    @staticmethod
    def _queries(t: Task) -> list[str]:
        """从主题里挑检索词：先取"主题里出现过的素材标签"，再补主题分词。

        mock 规划器没有语义理解能力，用标签命中来近似"关键词抽取"；
        真实模型路径由模型自己给 queries，这里不参与。
        """
        pool = config.load_materials()["materials"]
        topic = t.topic or ""
        tag_hits: list[str] = []
        for m in pool:
            for tag in m["tags"]:
                if tag in topic and tag not in tag_hits:
                    tag_hits.append(tag)
        # 命中素材越多的标签越靠前（更能代表主题）
        tag_hits.sort(key=lambda tag: -sum(1 for m in pool if tag in m["tags"]))
        split = [x for x in re.split(r"[\s，,、/｜|]+", topic) if x]
        qs = (tag_hits[:2] + split[:2]) or [topic]
        return qs

    def _after_search(self, ctx: DecisionContext, obs: Observation | None) -> Action:
        t, s = ctx.task, ctx.scratch
        if obs is None:
            return self._finish("INCOMPLETE_TOOL_FAILURE", "素材检索无返回", "无观察结果，安全收尾")

        if obs.status == "error":
            if not s.get("search_retried"):
                s["search_retried"] = True
                return Action("search_materials",
                              {"queries": [self._queries(t)[0]], "limit": s["estimate"]["material_items"]},
                              f"素材检索失败（{obs.error}），收窄为单关键词重试")
            return self._finish("INCOMPLETE_TOOL_FAILURE",
                                f"素材检索连续失败：{obs.error}；已保留费用估算 {s.get('estimate', {}).get('total')}",
                                "检索不可用，保留已完成估算收尾")

        items = obs.data.get("items", [])
        if not items:
            if not s.get("search_retried"):
                s["search_retried"] = True
                return Action("search_materials",
                              {"queries": [self._queries(t)[0]], "limit": s["estimate"]["material_items"]},
                              "素材 0 命中，改用主题首词扩大检索")
            return self._finish("INCOMPLETE_TOOL_FAILURE", "素材库无可用素材",
                                "没有素材无法生成脚本，返回未完成")

        ids = [m["id"] for m in items]
        s["materials"] = items
        s["material_ids"] = ids
        need = int(s["estimate"]["material_items"])
        if len(items) < need and not s.get("re_estimated"):
            # 素材不足 → 费用要按实际条数重算，不能沿用原报价
            s["re_estimated"] = True
            return Action("estimate_cost",
                          {"variant_id": s["chosen"], "material_ids": ids},
                          f"只拿到 {len(items)}/{need} 条素材，按实际条数重新核算费用再决定")
        return self._save_or_script(ctx, f"素材 {len(items)} 条就绪，")

    def _save_or_script(self, ctx: DecisionContext, prefix: str) -> Action:
        s = ctx.scratch
        if s.get("script"):
            return self._save_action(ctx, prefix + "保存方案")
        return Action("generate_script",
                      {"variant_id": s["chosen"], "material_ids": s.get("material_ids", []),
                       "tone": ctx.task.tone, "topic": ctx.task.topic},
                      prefix + f"用 {s['chosen']} 生成脚本")

    def _save_action(self, ctx: DecisionContext, basis: str) -> Action:
        s = ctx.scratch
        est = s.get("estimate", {})
        return Action("save_plan",
                      {"topic": ctx.task.topic, "variant_id": s["chosen"],
                       "material_ids": s.get("material_ids", []),
                       "cost_total": est.get("total"), "script": s.get("script")},
                      basis)


# ------------------------------------------------------------------ 真实模型

SYSTEM_PROMPT = """你是内容活动方案的执行 Agent，为运营人员在预算和截止时间内完成一份可交付方案。
每一步你只能输出一个 JSON 对象，不要输出解释文字：
{"action": "<动作名>", "args": {...}, "decision_basis": "<一句话依据，含关键数字>"}

可用动作（工具）：
{{tools}}
- request_input({"missing_fields": [...]})：缺少预算/截止时间/主题时，向运营要信息
- finish({"status": "...", "reason": "..."})：结束。status ∈ COMPLETED / INCOMPLETE_BUDGET /
  INCOMPLETE_TOOL_FAILURE / INCOMPLETE_SAVE / INCOMPLETE_LIMIT

硬性要求：
1. 费用必须来自 estimate_cost 的返回值，不要自己编数字。
2. 估算超预算或超可用时间时，换下一个更简单的方案，而不是直接放弃。
3. 工具报错时不要假装成功：可以重试一次或降级方案；实在做不了就 finish 并说明缺口。
4. 方案保存前必须已生成脚本；save_plan 会重新核算费用，数字对不上会被拒绝。
5. 不要重复保存已保存的方案。"""


class LLMPlanner:
    """真实模型规划器（OpenAI 兼容接口）。

    只做一件事：把 (任务 + 历史 + 上一步观察) 变成下一步 Action。
    工具执行、步数上限、落盘、状态校验都在 Agent 侧，模型无法绕过。
    """

    name = "llm"
    is_mock = False

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, timeout: float | None = None):
        self.model = model or config.LLM_MODEL
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.api_key = api_key or config.LLM_API_KEY
        self.timeout = timeout or config.LLM_TIMEOUT_S
        if not self.api_key:
            raise PlannerError(
                "未配置 OPENAI_API_KEY：真实模型路径不可用。"
                "请设置环境变量，或改用离线规划器 --planner mock")

    # -- 对外 -------------------------------------------------------
    def decide(self, ctx: DecisionContext) -> Action:
        history = ctx.scratch.get("history", [])
        payload_state = {
            "task": ctx.task.to_dict(),
            "available_minutes": ctx.task.available_minutes,
            "step": ctx.step,
            "rejected": ctx.scratch.get("rejected", []),
            "chosen_variant": ctx.scratch.get("chosen"),
            "materials": [m["id"] for m in (ctx.scratch.get("materials") or [])],
            "script_ready": bool(ctx.scratch.get("script")),
            "saved": ctx.scratch.get("saved"),
            # 每类工具最近一次成功返回（Agent 侧维护，与规划器实现无关）
            "progress": ctx.scratch.get("progress", {}),
            "last_observation": ctx.last_obs.to_dict() if ctx.last_obs else None,
            "history": history[-5:],
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.replace("{{tools}}", tool_specs_text())},
            {"role": "user", "content": "当前状态：\n" + json.dumps(payload_state, ensure_ascii=False)},
        ]
        content = self._chat(messages)
        action = self._parse_decision(content)
        history.append({"action": action.action, "basis": action.decision_basis})
        ctx.scratch["history"] = history
        return action

    # -- HTTP -------------------------------------------------------
    def _chat(self, messages: list[dict]) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        # 本地模型服务（ollama / vLLM / 本仓库的兼容端点）不能被系统 http_proxy 劫走
        host = urllib.parse.urlparse(self.base_url).hostname or ""
        if host in ("127.0.0.1", "localhost", "::1"):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            opener = urllib.request.build_opener()
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise PlannerError(f"模型接口 HTTP {e.code}：{e.read()[:300]!r}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise PlannerError(f"模型接口不可达：{e}") from e
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise PlannerError(f"模型返回结构异常：{data}") from e

    # -- 解析 -------------------------------------------------------
    @staticmethod
    def _parse_decision(content: str) -> Action:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"```\s*$", "", text).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise PlannerError(f"模型输出不含 JSON：{content[:200]!r}")
        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise PlannerError(f"模型 JSON 解析失败：{e}；原文 {text[:200]!r}") from e

        allowed = {"list_plan_variants", "search_materials", "estimate_cost",
                   "generate_script", "save_plan", "finish", "request_input"}
        action = str(obj.get("action", "")).strip()
        if action not in allowed:
            raise PlannerError(f"模型给出未知动作 {action!r}，仅允许 {sorted(allowed)}")
        args = obj.get("args") or {}
        if not isinstance(args, dict):
            raise PlannerError("args 必须是对象")
        return Action(action, args, str(obj.get("decision_basis", ""))[:200])


def build_planner(kind: str):
    if kind == "llm":
        return LLMPlanner()
    return MockPlanner()
