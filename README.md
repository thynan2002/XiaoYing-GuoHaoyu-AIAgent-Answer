# 内容活动方案 Agent

> 小影校招 · AI Agent 开发岗考核作业（题号：**内容任务规划 Agent**）

给一个主题、预算和截止时间，Agent 在一个循环里自己决定下一步：拉候选方案 → 逐个算钱 →
检索素材 → 写脚本 → 落盘。预算不够就换更小的方案，工具挂了就重试或降级，
实在做不完就**如实报未完成并保留已有成果**，不会把失败步骤记成成功。

- **运行方式**：CLI（`python -m src.cli`），Python 3.10+，**零第三方运行时依赖**
- **离线可跑**：默认 `--planner mock`（离线策略规划器），不需要任何密钥
- **两条规划路径都端到端跑通过**：`--planner mock` 与 `--planner llm`（OpenAI 兼容接口）。
  后者在 `case-10` 里接的是仓库内 `scripts/fake_llm_server.py` 起的本地兼容端点——真 HTTP、
  真 JSON 解析、真循环，但那个"模型"是规则模拟的，**不代表真实模型能力**；
  真实模型只需换三个环境变量，边界见 [docs/03 的未验证项](docs/03-evaluation.md#52-未验证项明确边界)

---

## 0. 提交信息

| 项 | 内容 |
| --- | --- |
| 题号 | 内容任务规划 Agent |
| 仓库 | https://github.com/thynan2002/XiaoYing-GuoHaoyu-AIAgent-Answer |
| 最终提交 | `main` 分支 HEAD；提交表单里的 SHA 用 `git rev-parse HEAD` 取同一个值 |
| 评审入口 | 先跑 `python scripts/verify_submission.py`，再按下面第 1 节看证据 |

README 不写死 SHA 的原因：SHA 在提交后才产生，写死会随最后一次提交立刻失效；
仓库 HEAD 与提交表单保持一致即可。

---

## 1. 评审入口（5 分钟路径）

| 想确认什么 | 直接看 |
| --- | --- |
| 有没有"模型决策 → 执行工具 → 观察 → 调整"的循环 | `src/agent.py` 的 `Agent.run`；`examples/case-01-normal/trace.json` 逐步记录 |
| 超预算会不会改方案 | `examples/case-01-normal/`（12 超预算 → 改 8）；`case-06`（全都超 → 报缺口） |
| 工具失败怎么处理 | `examples/case-04-tool-failure-degrade/`（重试 1 次 → 降级 → 仍交付） |
| 缺预算/截止时间会不会瞎猜 | `examples/case-02-missing-input/`（停下要信息）、`case-03`（补齐后续跑） |
| 部分成功会不会丢结果 | `examples/case-05-partial-materials/`（只回 1 条 → 按 1 条重算 → 交付） |
| 重复执行会不会写脏数据 | `examples/case-07-repeated-save/`（幂等命中，未重复写入） |
| 有没有步数上限 | `examples/case-08-step-limit/`（3/3 步停下，状态 `INCOMPLETE_LIMIT`） |
| 真实模型那条代码路径能不能跑 | `examples/case-10-llm-endpoint/`（走真 HTTP + 真解析，7 步完成，独立落盘） |
| 同一 LLM 路径遇到工具失败会不会调整 | `examples/case-13-llm-tool-failure/`（脚本失败 ×2 → 降级 lite → 保留素材） |
| 失败步骤能不能续跑（加分项） | `case-11` 落盘失败 → `case-12` 从第 8 步续跑，只补失败那步 |
| 交付结果是否齐全 | `result.json` 的 `plan.outline`（任务计划）、`steps_brief`（步骤状态）、`plan.cost_total`（费用合计）、`saved_plan` / `status_reason` |
| 测试是否真跑过 | `examples/tests-output.txt`（56 passed）、`tests/` |
| 提交物是否自洽 | `python scripts/verify_submission.py`（不联网，核对 13 个实施例与规范文件） |
| 取舍与限制 | `docs/02-solution.md`、`docs/03-evaluation.md` |

验收信息（题号 / URL / SHA）见第 0 节。所有实施例都是 `python scripts/gen_examples.py`
的真实运行产物，不是手写预期；重跑后 `elapsed_ms`、`saved_at`、随机端口会变，
动作序列、参数、费用、状态、决策依据应当逐条一致。

---

## 2. 环境

```bash
python --version      # 3.10+，开发环境 3.13.14
pip install -r requirements.txt   # 只装测试用的 pytest；运行本身无依赖
```

Windows / macOS / Linux 均可。命令都在仓库根目录执行。

> Windows 控制台如果出现中文乱码，先执行 `chcp 65001`；代码已对 stdout 做 UTF-8 兜底。
> README 的命令统一带 `--now "2026-10-28 15:00"`：它把"现在"固定住，否则过了输入里的
> 截止时间，"可用分钟"会变成 0，实施例就无法复现。这是刻意设计，不是多余参数。

## 3. 运行

### 3.1 正常跑一单

```bash
python -m src.cli run --input examples/inputs/01-normal.json \
  --now "2026-10-28 15:00" --plan-dir runs/plans --out runs/run1
```

预期能看到：`estimate_cost(full)=12.0` 被预算 10 否决 → `estimate_cost(lite)=8.0` 通过 →
检索 2 条素材 → 生成脚本 → 保存，最后 `状态: COMPLETED`。

### 3.2 注入工具失败（脚本服务挂两次）

```bash
python -m src.cli run --input examples/inputs/04-tool-failure.json \
  --fail-tool generate_script --fail-times 2 --fail-mode error \
  --now "2026-10-28 15:00" --plan-dir runs/plans --out runs/run2
```

预期：脚本连续失败 2 次 → 放弃完整版 → 降级精简版 → 重新估算 8.0 → 仍能交付。

### 3.3 预算不够

```bash
python -m src.cli run --input examples/inputs/06-budget-exceeded.json \
  --now "2026-10-28 15:00" --plan-dir runs/plans --out runs/run3
```

预期：三个方案都被否，`状态: INCOMPLETE_BUDGET`，给出缺口 1.0，不落盘。

### 3.4 缺预算 → 停下来问 → 补齐后续跑

```bash
python -m src.cli run --input examples/inputs/02-missing-budget.json \
  --now "2026-10-28 15:00" --plan-dir runs/plans --out runs/run4
# 状态 NEED_INPUT，退出码 3
python -m src.cli resume --state runs/run4/state.json --set budget=10 \
  --now "2026-10-28 15:00" --plan-dir runs/plans --out runs/run5
# 从第 2 步继续跑完
```

> 上面的 `--plan-dir runs/plans` 只是把演示产物放在临时目录，避免和你之前的试跑撞幂等指纹。
> 想看默认落盘位置就删掉这个参数（默认 `data/saved_plans`）。

### 3.5 一次生成全部实施例（自带状态核对）

```bash
python scripts/gen_examples.py
# 13 个用例全部 OK 才返回 0；任一状态/退出码与预期不符会列出差异并返回 1
```

### 3.6 测试

```bash
python -m pytest tests -q
# 56 passed
```

### 3.7 提交物自检（不跑模型、不联网）

```bash
python scripts/verify_submission.py
# [PASS] 提交物自检通过
```

### 3.8 走真实模型那条路径（无需真密钥也能先验证协议）

先用仓库内的本地兼容端点把链路跑一遍（两个终端）：

```bash
# 终端 A：起本地 OpenAI 兼容端点
python scripts/fake_llm_server.py --port 8765
```

```bash
# 终端 B（macOS / Linux）
export OPENAI_API_KEY=dummy
export OPENAI_BASE_URL=http://127.0.0.1:8765/v1
export OPENAI_MODEL=fake-rule-model
python -m src.cli run --input examples/inputs/01-normal.json --planner llm \
  --now "2026-10-28 15:00" --plan-dir runs/plans-llm --out runs/run-llm
```

Windows PowerShell 用：

```powershell
$env:OPENAI_API_KEY="dummy"
$env:OPENAI_BASE_URL="http://127.0.0.1:8765/v1"
$env:OPENAI_MODEL="fake-rule-model"
python -m src.cli run --input examples/inputs/01-normal.json --planner llm --now "2026-10-28 15:00" --plan-dir runs/plans-llm --out runs/run-llm
```

换成真模型只改环境变量，代码一行不动（下面以 DeepSeek 为例，其他厂商同理）：

```bash
export OPENAI_API_KEY=sk-...                 # 真密钥，只放环境变量，不要提交
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_MODEL=deepseek-chat
python -m src.cli run --input examples/inputs/01-normal.json --planner llm \
  --now "2026-10-28 15:00" --plan-dir runs/plans-llm --out runs/run-llm
```

模型只负责"下一步做什么"，工具执行、步数上限、落盘、完成校验都在 Agent 侧，
模型无法绕过去写脏数据（见 `docs/02-solution.md` 第 5 节）。

## 4. 目录

```
src/
  cli.py       命令行入口：run / resume，参数解析、把"现在"固定下来便于复现
  agent.py     ReAct 主循环、步数与工具调用上限、检查点、收尾校验、跨规划器台账
  planner.py   两个规划器：LLMPlanner（真实模型）/ MockPlanner（离线策略）
  tools.py     5 个业务工具 + 成本公式 + 故障注入 + 参数防御
  models.py    Task / Action / Observation / StepRecord
data/
  costs.json    单价表、方案规格、工时参数（全部合成数据）
  materials.json 18 条合成素材
  saved_plans/  运行时的落盘目录（内容不入库，保留 .gitkeep）
examples/       13 组输入 + 实际输出 + trace（见 examples/README.md）
docs/           01 需求 / 02 方案 / 03 评估 / 04 AI 使用 / 05 复盘
tests/          56 个 pytest 用例（含真 HTTP 的模型路径）
scripts/
  gen_examples.py      一键重跑全部实施例并核对状态（13 个）
  verify_submission.py 提交物自检（目录、交付字段、trace、必验场景）
  fake_llm_server.py   本地 OpenAI 兼容端点，用于验证 --planner llm 的代码路径
```

## 5. 交付字段（`result.json`）

| 题目要求 | 字段 | 说明 |
| --- | --- | --- |
| 任务计划 | `plan.outline[]` | 4 道工序的任务/工时/归属费用项，费用与工时之和与 `cost_total`、`estimated_minutes` 对齐 |
| 步骤状态 | `steps_brief[]` | 每步 `action + status(ok/partial/error/terminal) + summary` |
| 工具结果 | `trace.json` 的 `observation` | 完整返回体与错误信息 |
| 费用合计 | `plan.cost_total` + `plan.breakdown[]` | 逐项明细 |
| 已保存方案 | `saved_plan{plan_id,path,deduped}` | 失败时为 `null`；路径相对仓库根目录 |
| 未完成原因 | `status` + `status_reason` | 原因里带缺口/已保留的中间产物 |
| 被否方案 | `rejected[]` | 费用/工时越界由 Agent 自动记账，工具连续失败也留原因；换 LLM 规划器不丢 |
| 断点续跑 | `resume{resumed,from_step}` | `case-03` / `case-12` |

## 6. 状态码

| 状态 | 含义 | 退出码 |
| --- | --- | --- |
| `COMPLETED` | 方案已落盘且费用/工时/脚本校验通过 | 0 |
| `NEED_INPUT` | 缺主题/预算/截止时间，等运营补 | 3 |
| `INCOMPLETE_BUDGET` | 所有候选都超预算或超工时，附缺口 | 4 |
| `INCOMPLETE_TOOL_FAILURE` | 关键工具不可用且无可替代方案 | 4 |
| `INCOMPLETE_SAVE` | 落盘失败，脚本与费用仍保留 | 4 |
| `INCOMPLETE_LIMIT` | 步数或工具调用到上限 | 4 |
| `INCOMPLETE_PLANNER` | 模型输出无法解析 / 状态非法 / 接口不可达 | 5 |
| `INCOMPLETE_INCONSISTENT` | 模型声称完成但校验不通过（无方案、超预算、超工时） | 4 |

## 7. 已知限制

1. **离线规划器不是大模型**：它按固定策略读观察做决策，能证明流程和失败处理正确，
   不能证明真实模型的决策质量。`--planner llm` 的代码路径已用本地兼容端点端到端跑通
   （`case-10`），但"真实模型会怎么选"仍未在真实模型上验证——这是本仓库唯一的效果缺口。
2. 素材与费用全是合成数据；脚本由模板拼装，不是真实文案能力。
3. 单机单进程，没有并发控制；幂等去重靠文件名 + 内容指纹，跨机器不保证。
4. 未做界面与线上部署（考核说明中非必需）。
5. 搜索是标签 + 字符二元组匹配，素材库只有 18 条；0 命中时只有一次换词机会。
