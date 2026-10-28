# examples · 实施例索引

每个目录都是**真实运行产物**（`python scripts/gen_examples.py` 可原样重跑），
不是手写的预期结果。目录结构统一：

| 文件 | 内容 |
| --- | --- |
| `command.txt` | 该用例的实际命令（不含 `python -m`） |
| `input.json` | 任务输入（`resume` 用例没有，因为它继承上一个检查点） |
| `stdout.txt` | 终端人类可读输出：每步动作 / 依据 / 观察 / 后续 |
| `result.json` | 机读结果：状态、任务计划、费用、被否方案、步骤状态、落盘路径、用量 |
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
| `case-06-budget-exceeded` | 预算 4，最低档 5 | INCOMPLETE_BUDGET | 三档全否，缺口 1.0，`plan` 为 null |
| `case-07-repeated-save` | 与 case-01 完全同输入 | COMPLETED | `deduped: true`，未写第二份 |
| `case-08-step-limit` | `--max-steps 3` | INCOMPLETE_LIMIT | 3/3 步，理由含已否方案 |
| `case-09-tight-deadline` | 可用 10 分钟 | COMPLETED | full 12 min 超时 → lite 7 min 交付 |
| `case-10-llm-endpoint` | `--planner llm` 接本地兼容端点 | COMPLETED | 真 HTTP + 真解析，7 步，12.0 → 8.0 |
| `case-11-save-failure` | 落盘持续 503 | INCOMPLETE_SAVE | 8 步；素材/脚本/费用都保留 |
| `case-12-resume-after-save-failure` | 承接 11 的检查点续跑 | COMPLETED | 第 9 步直接 save_plan，检索/脚本不重跑 |

## 快速自查

```bash
# 预算被否后改方案（看第 3 步依据里的数字）
python -c "import json;t=json.load(open('examples/case-01-normal/trace.json'));[print(s['step'],s['action'],s['observation']['status'],'|',s['decision_basis']) for s in t['steps'][:4]]"

# 工具失败后是否调整计划
python -c "import json;t=json.load(open('examples/case-04-tool-failure-degrade/trace.json'));[print(s['step'],s['action'],s['observation']['status'],'|',s['decision_basis']) for s in t['steps'][4:8]]"

# 未完成原因与缺口
python -c "import json;r=json.load(open('examples/case-06-budget-exceeded/result.json'));print(r['status'],r['status_reason'])"

# 续跑是不是真的只补失败那步
python -c "import json;t=json.load(open('examples/case-12-resume-after-save-failure/trace.json'));print([(s['step'],s['action']) for s in t['steps'] if s['step']>=8])"

# 任务计划与费用是否自洽
python -c "import json;r=json.load(open('examples/case-01-normal/result.json'));print([(o['task'],o['minutes'],o['cost']) for o in r['plan']['outline']], r['plan']['cost_total'])"
```

`tests-output.txt` 是 `python -m pytest tests -q` 的完整输出（49 passed）。
