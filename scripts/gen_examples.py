"""一键重跑 examples/ 下所有运行记录。

    python scripts/gen_examples.py

会先清空 data/saved_plans 与 examples/case-*，再按固定顺序执行，
保证幂等去重（case-07）与续跑（case-03）依赖的顺序是稳定的。
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
FAKE_LLM_PORT = 8765

CASES = [
    dict(name="case-01-normal", input="01-normal.json",
         args=["run"], extra=["--run-id", "case-01"]),
    dict(name="case-02-missing-input", input="02-missing-budget.json",
         args=["run"], extra=["--run-id", "case-02"]),
    dict(name="case-03-resume-after-input", input=None,
         args=["resume"], extra=["--run-id", "case-03",
                                 "--state", "examples/case-02-missing-input/state.json",
                                 "--set", "budget=10"]),
    dict(name="case-04-tool-failure-degrade", input="04-tool-failure.json",
         args=["run"], extra=["--run-id", "case-04",
                              "--fail-tool", "generate_script",
                              "--fail-times", "2", "--fail-mode", "error"]),
    dict(name="case-05-partial-materials", input="05-partial-materials.json",
         args=["run"], extra=["--run-id", "case-05",
                              "--fail-tool", "search_materials",
                              "--fail-times", "1", "--fail-mode", "partial"]),
    dict(name="case-06-budget-exceeded", input="06-budget-exceeded.json",
         args=["run"], extra=["--run-id", "case-06"]),
    dict(name="case-07-repeated-save", input="01-normal.json",
         args=["run"], extra=["--run-id", "case-07"]),
    dict(name="case-08-step-limit", input="01-normal.json",
         args=["run"], extra=["--run-id", "case-08", "--max-steps", "3"]),
    dict(name="case-09-tight-deadline", input="07-tight-deadline.json",
         args=["run"], extra=["--run-id", "case-09"]),
    # 真实模型那条代码路径：起一个本地 OpenAI 兼容端点，走真 HTTP + 真 JSON 解析
    dict(name="case-10-llm-endpoint", input="01-normal.json",
         args=["run"], extra=["--run-id", "case-10", "--planner", "llm"],
         llm=True),
    dict(name="case-11-save-failure", input="08-save-failure.json",
         args=["run"], extra=["--run-id", "case-11",
                              "--fail-tool", "save_plan", "--fail-times", "99"]),
    dict(name="case-12-resume-after-save-failure", input=None,
         args=["resume"], extra=["--run-id", "case-12",
                                 "--state", "examples/case-11-save-failure/state.json"]),
]


def _start_fake_llm() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "scripts/fake_llm_server.py",
                             "--port", str(FAKE_LLM_PORT)],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", FAKE_LLM_PORT), timeout=0.2):
                return proc
        except OSError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("fake llm server 未就绪")


def main() -> int:
    plans = ROOT / "data" / "saved_plans"
    if plans.exists():
        shutil.rmtree(plans)
    for d in EX.glob("case-*"):
        shutil.rmtree(d)

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
            server = _start_fake_llm()
            env.update({"OPENAI_API_KEY": "dummy-local-endpoint",
                        "OPENAI_BASE_URL": f"http://127.0.0.1:{FAKE_LLM_PORT}/v1",
                        "OPENAI_MODEL": "fake-rule-model"})
        try:
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env)
        finally:
            if server:
                server.terminate()
                server.wait(timeout=5)
        (out / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
        result_path = out / "result.json"
        if result_path.exists():
            r = json.loads(result_path.read_text(encoding="utf-8"))
            print(f"{case['name']:<32} {r['status']:<24} "
                  f"steps={r['usage']['steps']:<3} cost={(r.get('plan') or {}).get('cost_total')}")
        else:
            print(f"{case['name']:<32} <无 result.json> rc={proc.returncode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
