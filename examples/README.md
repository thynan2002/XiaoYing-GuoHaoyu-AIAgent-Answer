# examples · 实施例索引

每个目录都是**真实运行产物**（`python scripts/gen_examples.py` 可原样重跑），
不是手写的预期结果。目录结构统一：

| 文件 | 内容 |
| --- | --- |
| `command.txt` | 该用例的实际命令（不含 `python -m`） |
| `input.json` | 任务输入（`resume` 用例没有，因为它继承上一个检查点） |
| `stdout.txt` | 终端人类可读输出：每步动作 / 依据 / 观察 / 后续 |
| `result.json` | 机读结果：状态、选定方案、费用明细、被否方案、落盘路径、用量 |
| `trace.json` | 逐步轨迹：`step / action / args / decision_basis / observation / follow_up` |
| `state.json` | 该次运行的检查点（可被 `resume` 读取） |
| `saved_plan.json` | 仅成功用例：落盘方案副本（含脚本与费用明细） |

## 用例表

| 目录 | 场景 | 状态 | 关键数字 |
| --- | --- | --- | --- |
| `case-01-normal` | 预算 10，完整版 12 超支 | COMPLETED | full 12.0 否决 → lite 8.0 交付 |
| `case-02-missing-input` | 没给预算 | NEED_INPUT | 1 步 0 次工具调用，退出码 3 |
| `case-03-resume-after-input` | 补预算后续跑 | COMPLETED | 8 步（含前次 1 步），复用已检索素材 |
| `case-04-tool-failure-degrade` | 脚本工具连续 2 次 503 | COMPLETED | full 10.8 → 失败 ×2 → lite 8.0，11 步 |
| `case-05-partial-materials` | 检索只返回 1/4 条 | COMPLETED | 12.0 按 1 条重算 10.2 后交付 |
| `case-06-budget-exceeded` | 预算 4，最低档 5 | INCOMPLETE_BUDGET | 三档全否，缺口 1.0，无落盘 |
| `case-07-repeated-save` | 与 case-01 完全同输入 | COMPLETED | `deduped: true`，未写第二份 |
| `case-08-step-limit` | `--max-steps 3` | INCOMPLETE_LIMIT | 3/3 步，理由含已否方案 |
| `case-09-tight-deadline` | 可用 10 分钟 | COMPLETED | full 12 min 超时 → lite 7 min 交付 |

## 快速自查

```bash
# 任选一个用例复核关键字段
python -c "import json;r=json.load(open('examples/case-06-budget-exceeded/result.json'));print(r['status'],r['status_reason'])"
python -c "import json;t=json.load(open('examples/case-04-tool-failure-degrade/trace.json'));[print(s['step'],s['action'],s['observation']['status'],'|',s['decision_basis']) for s in t['steps']]"
```

`tests-output.txt` 是 `python -m pytest tests -q` 的完整输出（38 passed）。
