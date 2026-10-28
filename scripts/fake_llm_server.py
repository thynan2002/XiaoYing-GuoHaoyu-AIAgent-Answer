"""本地 OpenAI 兼容端点，用来端到端验证 `--planner llm` 那条代码路径。

它做两件事：
1. 真起一个 HTTP 服务，真走 /v1/chat/completions、真返回 `choices[0].message.content`，
   让 LLMPlanner 的组包、鉴权头、超时、JSON 解析都被真实执行一次；
2. 用一个最简规则充当"模型"，保证无密钥环境也能把循环跑完。

**它不能证明什么**：这里的规则是人写的，跑通只说明"协议与循环接得上"，
不说明真实模型的决策质量。真实模型要换成真 key + 真 base_url，代码一行不改。

用法：
    python scripts/fake_llm_server.py --port 8765
    # 另开一个终端：
    set OPENAI_API_KEY=dummy OPENAI_BASE_URL=http://127.0.0.1:8765/v1 OPENAI_MODEL=fake
    python -m src.cli run --input examples/inputs/01-normal.json --planner llm --out /tmp/llm
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402


def decide(state: dict) -> dict:
    """最简"模型"：只依赖 state，不持有会话状态。"""
    task = state.get("task") or {}
    budget = task.get("budget")
    avail = task.get("available_minutes")
    last = state.get("last_observation")
    progress = state.get("progress") or {}
    rejected = state.get("rejected") or []

    missing = task.get("missing_fields") or []
    if missing:
        return {"action": "request_input", "args": {"missing_fields": missing},
                "decision_basis": f"缺 {','.join(missing)}，先要信息"}

    variants = config.load_costs().get("preference_order") or []
    rejected_ids = {r.get("variant") for r in rejected}

    if not last:
        return {"action": "list_plan_variants", "args": {"topic": task.get("topic", "")},
                "decision_basis": "先拿候选方案，费用需现算"}

    tool = last.get("tool")
    data = last.get("data") or {}

    if tool == "list_plan_variants":
        pick = next((v for v in variants if v not in rejected_ids), None)
        if not pick:
            return {"action": "finish",
                    "args": {"status": "INCOMPLETE_BUDGET", "reason": "没有候选方案"},
                    "decision_basis": "候选为空"}
        return {"action": "estimate_cost", "args": {"variant_id": pick},
                "decision_basis": f"按完整度优先核算 {pick}"}

    if tool == "estimate_cost":
        total = float(data.get("total", 0))
        minutes = data.get("estimated_minutes", 0)
        within_budget = budget is None or total <= float(budget)
        within_time = avail is None or minutes <= float(avail)
        if within_budget and within_time:
            return {"action": "search_materials",
                    "args": {"queries": [task.get("topic", "")], "limit": data.get("material_items", 2)},
                    "decision_basis": f"{data.get('label')} {total} 在预算 {budget} 内、{minutes} 分钟可交付，选它"}
        why = (f"费用 {total} 超预算 {budget}" if not within_budget
               else f"耗时 {minutes} 分钟超过可用 {avail} 分钟")
        pick = next((v for v in variants if v not in rejected_ids and v != data.get("variant_id")), None)
        if not pick:
            return {"action": "finish",
                    "args": {"status": "INCOMPLETE_BUDGET",
                             "reason": f"所有候选都不可行，最后一条：{why}"},
                    "decision_basis": "无可行方案，报缺口"}
        return {"action": "estimate_cost", "args": {"variant_id": pick},
                "decision_basis": f"{data.get('label')} 被否（{why}），换 {pick} 再算"}

    if tool == "search_materials":
        items = [m["id"] for m in data.get("items") or []]
        est = (progress.get("estimate_cost") or {})
        if not items:
            return {"action": "finish",
                    "args": {"status": "INCOMPLETE_TOOL_FAILURE", "reason": "没有可用素材"},
                    "decision_basis": "无素材无法写脚本"}
        return {"action": "generate_script",
                "args": {"variant_id": est.get("variant_id"), "material_ids": items,
                         "tone": task.get("tone", "轻快"), "topic": task.get("topic", "")},
                "decision_basis": f"素材 {len(items)} 条就绪，写脚本"}

    if tool == "generate_script":
        est = progress.get("estimate_cost") or {}
        return {"action": "save_plan",
                "args": {"topic": task.get("topic", ""),
                         "variant_id": est.get("variant_id"),
                         "material_ids": [m["id"] for m in (progress.get("search_materials", {}).get("items") or [])],
                         "cost_total": est.get("total"),
                         "script": data.get("script")},
                "decision_basis": "脚本已生成，保存方案"}

    if tool == "save_plan":
        return {"action": "finish",
                "args": {"status": "COMPLETED",
                         "reason": f"方案已保存 {data.get('plan_id')}"},
                "decision_basis": "落盘成功"}

    return {"action": "finish", "args": {"status": "INCOMPLETE_PLANNER",
                                        "reason": f"无法处理 {tool}"},
            "decision_basis": "未知观察"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            body = json.loads(raw)
            user_msg = next(m["content"] for m in body["messages"] if m["role"] == "user")
            state = json.loads(user_msg.split("当前状态：", 1)[1].strip())
            decision = decide(state)
        except Exception as e:  # noqa: BLE001
            decision = {"action": "finish",
                        "args": {"status": "INCOMPLETE_PLANNER", "reason": f"fake model error: {e}"},
                        "decision_basis": "服务端解析失败"}

        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": body.get("model", "fake") if isinstance(body, dict) else "fake",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant",
                            "content": json.dumps(decision, ensure_ascii=False)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 静音访问日志
        return


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake OpenAI-compatible endpoint on http://{args.host}:{args.port}/v1", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
