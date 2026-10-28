"""一键重跑 examples/ 下全部运行记录，并在最后核对每个用例的状态。

    python scripts/gen_examples.py

会先清空 data/saved_plans 与 examples/case-*，再按固定顺序执行，
保证幂等去重（case-07）与续跑（case-03 / case-12）依赖的顺序是稳定的。
任何用例的实际状态与 expect 不符，脚本以退出码 1 结束，避免"证据漂移"。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
NOW = "2026-10-28 15:00"   # 固定"现在"，让"可用分钟"可复现

CASES = [
    dict(name="case-01-normal", input="01-normal.json", expect="COMPLETED",
         args=["run"], extra=["--run-id", "case-01"]),
    dict(name="case-02-missing-input", input="02-missing-budget.json", expect="NEED_INPUT",
         args=["run"], extra=["--run-id", "case-02"]),
    dict(name="case-03-resume-after-input", input=None, expect="COMPLETED",
         args=["resume"], extra=["--run-id", "case-03",
                                 "--state", "examples/case-02-missing-input/state.json",
                                 "--set", "budget=10"]),
    dict(name="case-04-tool-failure-degrade", input="04-tool-failure.json", expect="COMPLETED",
         args=["run"], extra=["--run-id", "case-04",
                              "--fail-tool", "generate_script",
                              "--fail-times", "2", "--fail-mode", "error"]),
    dict(name="case-05-partial-materials", input="05-partial-materials.json", expect="COMPLETED",
         args=["run"], extra=["--run-id", "case-05",
                              "--fail-tool", "search_materials",
                              "--fail-times", "1", "--fail-mode", "partial"]),
    dict(name="case-06-budget-exceeded", input="06-budget-exceeded.json",
         expect="INCOMPLETE_BUDGET", args=["run"], extra=["--run-id", "case-06"]),
    dict(name="case-07-repeated-save", input="01-normal.json", expect="COMPLETED",
         args=["run"], extra=["--run-id", "case-07"]),
    dict(name="case-08-step-limit", input="01-normal.json", expect="INCOMPLETE_LIMIT",
         args=["run"], extra=["--run-id", "case-08", "--max-steps", "3"]),
    dict(name="case-09-tight-deadline", input="07-tight-deadline.json", expect="COMPLETED",
         args=["run"], extra=["--run-id", "case-09"]),
    # 真实模型那条代码路径：起一个本地 OpenAI 兼容端点，走真 HTTP + 真 JSON 解析。
    # 用独立 plan-dir，避免与 case-01 的幂等指纹撞车，让"落盘"这一步也真实发生。
    dict(name="case-10-llm-endpoint", input="01-normal.json", expect="COMPLETED",
         args=["run"], extra=["--run-id", "case-10", "--planner", "llm",
                              "--plan-dir", "data/saved_plans/case-10"],
         llm=True),
    # 同一 LLM 代码路径下的工具失败：观察它是否重试 → 降级 → 保留已成功结果
    dict(name="case-13-llm-tool-failure", input="09-llm-tool-failure.json", expect="COMPLETED",
         args=["run"], extra=["--run-id", "case-13", "--planner", "llm",
                              "--fail-tool", "generate_script",
                              "--fail-times", "2", "--fail-mode", "error",
                              "--plan-dir", "data/saved_plans/case-13"],
         llm=True),
    dict(name="case-11-save-failure", input="08-save-failure.json", expect="INCOMPLETE_SAVE",
         args=["run"], extra=["--run-id", "case-11",
                              "--fail-tool", "save_plan", "--fail-times", "99"]),
    dict(name="case-12-resume-after-save-failure", input=None, expect="COMPLETED",
         args=["resume"], extra=["--run-id", "case-12",
                                 "--state", "examples/case-11-save-failure/state.json"]),
]

EXIT_CODE = {"COMPLETED": 0, "NEED_INPUT": 3, "INCOMPLETE_PLANNER": 5}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_fake_llm(port: int) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "scripts/fake_llm_server.py",
                             "--port", str(port)],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc
        except OSError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("fake llm server 未就绪")


def main() -> int:
    plans = ROOT / "data" / "saved_plans"
    plans.mkdir(parents=True, exist_ok=True)
    # 清空运行产物，但保留 .gitkeep（目录本身要入库）
    for child in plans.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for d in EX.glob("case-*"):
        shutil.rmtree(d)

    failures: list[str] = []
    for case in CASES:
        out = EX / case["name"]
        out.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "src.cli", *case["args"]]
        if case["input"]:
            cmd += ["--input", f"examples/inputs/{case['input']}"]
        cmd += case["extra"] + ["--out", f"examples/{case['name']}", "--now", NOW]

        # 把输入另存一份，方便单看某个用例
        if case["input"]:
            shutil.copyfile(EX / "inputs" / case["input"], out / "input.json")
        (out / "command.txt").write_text(" ".join(cmd[1:]) + "\n", encoding="utf-8")

        env = dict(os.environ)
        server = None
        if case.get("llm"):
            port = _free_port()
            server = _start_fake_llm(port)
            env.update({"OPENAI_API_KEY": "dummy-local-endpoint",
                        "OPENAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
                        "OPENAI_MODEL": "fake-rule-model"})
            (out / "env.txt").write_text(
                "OPENAI_API_KEY=dummy-local-endpoint\n"
                f"OPENAI_BASE_URL=http://127.0.0.1:{port}/v1\n"
                "OPENAI_MODEL=fake-rule-model\n", encoding="utf-8")
        try:
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env)
        finally:
            if server:
                server.terminate()
                server.wait(timeout=5)
        (out / "stdout.txt").write_text(proc.stdout, encoding="utf-8")

        result_path = out / "result.json"
        if not result_path.exists():
            failures.append(f"{case['name']}: 没有 result.json，rc={proc.returncode}")
            print(f"{case['name']:<32} <无 result.json> rc={proc.returncode}")
            continue
        r = json.loads(result_path.read_text(encoding="utf-8"))
        want_rc = EXIT_CODE.get(case["expect"], 4)
        ok = r["status"] == case["expect"] and proc.returncode == want_rc
        print(f"{case['name']:<32} {r['status']:<24} "
              f"steps={r['usage']['steps']:<3} "
              f"cost={(r.get('plan') or {}).get('cost_total')}  "
              f"{'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(
                f"{case['name']}: 期望 {case['expect']}/rc={want_rc}，"
                f"实际 {r['status']}/rc={proc.returncode}")

    if failures:
        print("\n[失败] 实施例与预期不一致：")
        for f in failures:
            print(" -", f)
        return 1
    print(f"\n[完成] {len(CASES)} 个实施例全部符合预期。")  # 数量随 CASES 自动变，避免文档漂移
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
