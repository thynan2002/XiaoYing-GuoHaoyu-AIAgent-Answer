"""一键重跑 examples/ 下所有运行记录。

    python scripts/gen_examples.py

会先清空 data/saved_plans 与 examples/case-*，再按固定顺序执行，
保证幂等去重（case-07）与续跑（case-03）依赖的顺序是稳定的。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
NOW = "2026-10-28 15:00"   # 固定"现在"，让"可用分钟"可复现

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
]


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

        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
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
