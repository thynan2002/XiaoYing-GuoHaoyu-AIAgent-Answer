"""业务工具层：素材检索、方案候选、成本估算、脚本生成、方案保存。

设计要点
- 完全本地可跑，不依赖网络；工具失败可用 `--fail-tool` 注入，用于验证 Agent 的失败处理。
- 工具只负责"做事并如实返回"，不做业务决策：预算够不够、要不要换方案由模型判断。
- 成本一律由 estimate_cost 现算，save_plan 会重新核算，防止模型把算错的数字写进方案。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from . import config
from .config import DEFAULT_PLAN_DIR
from .models import Observation


class ToolError(Exception):
    """工具执行失败（业务错误或注入的故障）。"""


# ------------------------------------------------------------------ 成本公式

def compute_cost(variant_id: str, item_count: int | None = None) -> dict[str, Any]:
    """按 data/costs.json 的单价表现算一个方案的费用明细。

    item_count 允许覆盖方案默认素材条数（检索到的素材不足时会变少）。
    """
    costs = config.load_costs()
    unit = costs["unit_costs"]
    variants = costs["variants"]
    if variant_id not in variants:
        raise ToolError(f"未知方案 {variant_id}，可选：{', '.join(variants)}")

    spec = variants[variant_id]
    items = spec["material_items"] if item_count is None else int(item_count)
    ceil30 = math.ceil(spec["duration_s"] / 30)

    breakdown: list[dict[str, Any]] = []

    def add(label: str, amount: float, detail: str) -> None:
        breakdown.append({"item": label, "amount": round(amount, 2), "detail": detail})

    add(unit["material_license"]["label"],
        unit["material_license"]["per_item"] * items,
        f"{items} 条 × {unit['material_license']['per_item']}")

    add(unit["script"]["label"],
        unit["script"]["base"] + unit["script"]["per_30s"] * ceil30,
        f"基础 {unit['script']['base']} + {ceil30} 个 30 秒段 × {unit['script']['per_30s']}")

    if spec["voice"]:
        add(unit["voice"]["label"],
            unit["voice"]["base"] + unit["voice"]["per_30s"] * ceil30,
            f"基础 {unit['voice']['base']} + {ceil30} 个 30 秒段 × {unit['voice']['per_30s']}")

    add(unit["edit"]["label"],
        unit["edit"]["base"] + unit["edit"]["per_shot"] * spec["shots"],
        f"基础 {unit['edit']['base']} + {spec['shots']} 镜头 × {unit['edit']['per_shot']}")

    if spec["review"]:
        add(unit["review"]["label"], unit["review"]["flat"], "固定审核费")

    total = round(sum(b["amount"] for b in breakdown), 2)

    tt = costs["tool_time"]
    seconds = (tt["script_gen_seconds"]
               + items * tt["per_shot_render_seconds"]
               + spec["shots"] * tt["per_shot_render_seconds"]
               + (tt["review_seconds"] if spec["review"] else 0)
               + 15)
    minutes = math.ceil(seconds / 60)

    return {
        "variant_id": variant_id,
        "label": spec["label"],
        "duration_s": spec["duration_s"],
        "material_items": items,
        "shots": spec["shots"],
        "voice": spec["voice"],
        "review": spec["review"],
        "breakdown": breakdown,
        "total": total,
        "currency": costs["currency"],
        "estimated_minutes": minutes,
    }


# ------------------------------------------------------------------ 脚本生成

def build_script(variant_id: str, materials: list[dict], tone: str, topic: str) -> dict[str, Any]:
    costs = config.load_costs()
    spec = costs["variants"][variant_id]
    shots = spec["shots"]
    titles = [m["title"] for m in materials] or ["通用空镜"]
    lines = []
    for i in range(shots):
        src = titles[i % len(titles)]
        if i == 0:
            hook = f"用 3 秒把「{topic}」说清楚——从{src}开始。"
        elif i == shots - 1:
            hook = f"收尾落回「{topic}」，一句行动号召。"
        else:
            hook = f"第 {i + 1} 镜头承接{src}，推进情绪。"
        lines.append({
            "shot": i + 1,
            "visual": src,
            "seconds": round(spec["duration_s"] / shots, 1),
            "voiceover": "" if not spec["voice"] else hook,
            "on_screen_text": f"{topic} · {i + 1}",
        })
    return {
        "title": f"{topic}｜{spec['label']}脚本",
        "tone": tone,
        "duration_s": spec["duration_s"],
        "shots": lines,
    }


# ------------------------------------------------------------------ 工具注册表

TOOL_SPECS = [
    {
        "name": "list_plan_variants",
        "description": "列出可用于该主题的制作方案候选（不含费用，费用需用 estimate_cost 现算）。",
        "args": {"topic": "string", "duration_preference": "string, 可空"},
    },
    {
        "name": "search_materials",
        "description": "按关键词在素材库检索合成素材，返回命中的素材与数量。",
        "args": {"queries": "string[]", "limit": "int, 默认 4"},
    },
    {
        "name": "estimate_cost",
        "description": "估算某个方案的费用明细与耗时；material_ids 可覆盖素材条数（检索不足时用）。",
        "args": {"variant_id": "string", "material_ids": "string[], 可空"},
    },
    {
        "name": "generate_script",
        "description": "为选定方案生成分镜脚本，需要方案 id、素材 id 列表、语气。",
        "args": {"variant_id": "string", "material_ids": "string[]", "tone": "string"},
    },
    {
        "name": "save_plan",
        "description": "保存最终方案（幂等：相同内容不会重复写入）。会重新核算费用。",
        "args": {"topic": "string", "variant_id": "string", "material_ids": "string[]",
                 "cost_total": "number", "script": "object"},
    },
]


def tool_specs_text() -> str:
    lines = []
    for s in TOOL_SPECS:
        args = ", ".join(f"{k}: {v}" for k, v in s["args"].items())
        lines.append(f"- {s['name']}({args})：{s['description']}")
    return "\n".join(lines)


class ToolRegistry:
    """执行工具调用，并支持注入故障（用于演示失败处理）。"""

    def __init__(self, plan_dir: Path | None = None,
                 inject: dict[str, dict] | None = None):
        self.plan_dir = Path(plan_dir or DEFAULT_PLAN_DIR)
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        # inject: {"generate_script": {"times": 2, "mode": "error"}}
        self.inject = {k: dict(v) for k, v in (inject or {}).items()}
        self.calls: list[str] = []

    # -- 故障注入 ---------------------------------------------------
    def _maybe_fail(self, name: str) -> str | None:
        cfg = self.inject.get(name)
        if not cfg or cfg.get("times", 0) <= 0:
            return None
        cfg["times"] -= 1
        return cfg.get("mode", "error")

    # -- 入口 -------------------------------------------------------
    def call(self, name: str, args: dict) -> Observation:
        self.calls.append(name)
        mode = self._maybe_fail(name)

        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return Observation("error", f"未知工具 {name}", error="UNKNOWN_TOOL", tool=name)

        # 注入的硬失败不执行副作用，直接返回错误
        if mode == "timeout":
            return Observation("error", f"{name} 调用超时（注入）",
                               error="TIMEOUT: 工具超过 5s 未返回", tool=name)
        if mode == "error":
            return Observation("error", f"{name} 失败：{name} 返回 503（注入故障）",
                               error=f"{name} 返回 503（注入故障）", tool=name)

        try:
            data, summary, status = handler(**(args or {}))
            if mode == "partial":
                data, summary, status = self._degrade(name, data)
            return Observation(status, summary, data=data, tool=name)
        except ToolError as e:
            return Observation("error", f"{name} 失败：{e}", error=str(e), tool=name)
        except TypeError as e:
            return Observation("error", f"{name} 参数不合法：{e}", error=f"BAD_ARGS: {e}", tool=name)

    @staticmethod
    def _degrade(name: str, data: dict):
        """注入"部分成功"：只返回第一条素材。"""
        if name == "search_materials" and data.get("items"):
            items = data["items"][:1]
            data = {**data, "items": items, "returned": 1,
                    "degraded": True, "injected_partial": True}
            return data, f"（注入部分失败）只返回 1/{data['requested']} 条：{items[0]['title']}", "partial"
        return data, f"（注入部分失败）{name} 仅返回部分结果", "partial"

    # -- 具体工具 ---------------------------------------------------
    def _tool_list_plan_variants(self, topic: str = "", duration_preference: str = ""):
        costs = config.load_costs()
        order = costs.get("preference_order") or list(costs["variants"])
        variants = []
        for vid in order:
            spec = costs["variants"][vid]
            variants.append({
                "variant_id": vid,
                "label": spec["label"],
                "summary": spec["summary"],
                "duration_s": spec["duration_s"],
                "material_items": spec["material_items"],
            })
        return ({"variants": variants}, f"可用方案 {len(variants)} 个：" +
                " / ".join(f"{v['label']}({v['variant_id']})" for v in variants), "ok")

    def _tool_search_materials(self, queries: list[str] | None = None,
                               limit: int = 4):
        queries = [q for q in (queries or []) if str(q).strip()]
        if not queries:
            raise ToolError("queries 不能为空")
        pool = config.load_materials()["materials"]
        scored: list[tuple[int, dict]] = []
        for m in pool:
            hay = (m["title"] + " " + " ".join(m["tags"])).lower()
            score = sum(3 if q.lower() in m["title"].lower() else
                        (1 if q.lower() in hay else 0) for q in queries)
            if score:
                scored.append((score, m))
        scored.sort(key=lambda x: (-x[0], x[1]["id"]))
        hits = [m for _, m in scored][:max(1, int(limit))]
        if not hits:
            # 无命中也算合法结果，但要把"没素材"如实告诉模型
            return ({"items": [], "requested": limit, "returned": 0, "degraded": True,
                     "queries": queries},
                    "素材库无命中，可用素材 0 条", "partial")
        degraded = len(hits) < int(limit)
        status = "partial" if degraded else "ok"
        summary = (f"命中 {len(hits)}/{limit} 条：" +
                   "、".join(m["title"] for m in hits))
        return ({"items": hits, "requested": int(limit), "returned": len(hits),
                 "degraded": degraded, "queries": queries}, summary, status)

    def _tool_estimate_cost(self, variant_id: str = "", material_ids: list[str] | None = None):
        if not variant_id:
            raise ToolError("缺少 variant_id")
        item_count = len(material_ids) if material_ids else None
        est = compute_cost(variant_id, item_count)
        return (est, f"{est['label']}估算 {est['total']} {est['currency']}，"
                     f"约 {est['estimated_minutes']} 分钟", "ok")

    def _tool_generate_script(self, variant_id: str = "", material_ids: list[str] | None = None,
                              tone: str = "轻快", topic: str = ""):
        if not variant_id:
            raise ToolError("缺少 variant_id")
        if not material_ids:
            raise ToolError("material_ids 不能为空：先检索素材")
        pool = {m["id"]: m for m in config.load_materials()["materials"]}
        picked = [pool[i] for i in material_ids if i in pool]
        if not picked:
            raise ToolError("material_ids 全部无效")
        spec = config.load_costs()["variants"][variant_id]
        need = spec["material_items"]
        if len(picked) < need:
            # 素材不足时允许写短脚本，但必须显式返回 partial
            script = build_script(variant_id, picked, tone, topic or "未命名主题")
            return ({"script": script, "reused": False},
                    f"素材不足（{len(picked)}/{need}），已按现有素材生成缩减脚本", "partial")
        script = build_script(variant_id, picked, tone, topic or "未命名主题")
        return ({"script": script, "reused": False},
                f"生成脚本《{script['title']}》，{len(script['shots'])} 个镜头", "ok")

    def _tool_save_plan(self, topic: str = "", variant_id: str = "",
                        material_ids: list[str] | None = None,
                        cost_total: float | None = None, script: dict | None = None):
        if not topic or not variant_id or not material_ids:
            raise ToolError("保存方案缺少必填字段（topic / variant_id / material_ids）")
        if script is None:
            raise ToolError("保存方案缺少 script")
        if cost_total is None:
            raise ToolError("保存方案缺少 cost_total")

        # 重新核算：拒绝把算错的费用写进方案
        recomputed = compute_cost(variant_id, len(material_ids))
        if abs(float(cost_total) - recomputed["total"]) > 0.01:
            raise ToolError(f"费用不一致：提交 {cost_total}，实算 {recomputed['total']}，拒绝保存")

        fingerprint = json.dumps({
            "topic": topic.strip(),
            "variant_id": variant_id,
            "material_ids": sorted(material_ids),
            "cost": recomputed["total"],
        }, ensure_ascii=False, sort_keys=True)
        plan_id = "plan_" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:10]
        path = self.plan_dir / f"{plan_id}.json"

        if path.exists():
            return ({"plan_id": plan_id, "path": str(path), "deduped": True,
                     "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                    f"方案 {plan_id} 已存在，命中幂等保存，未重复写入", "ok")

        payload = {
            "plan_id": plan_id,
            "topic": topic,
            "variant_id": variant_id,
            "variant_label": recomputed["label"],
            "material_ids": material_ids,
            "cost_total": recomputed["total"],
            "currency": recomputed["currency"],
            "breakdown": recomputed["breakdown"],
            "estimated_minutes": recomputed["estimated_minutes"],
            "script": script,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return ({"plan_id": plan_id, "path": str(path), "deduped": False,
                 "saved_at": payload["saved_at"]},
                f"方案已保存 {plan_id}（{recomputed['total']} {recomputed['currency']}）", "ok")
