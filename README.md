# 内容活动方案 Agent

> 小影校招 · AI Agent 开发岗考核作业（题号：内容任务规划 Agent）

给一个主题、预算和截止时间，Agent 在一个循环里自己决定下一步：拉候选方案 → 逐个算钱 →
检索素材 → 写脚本 → 落盘。预算不够就换更小的方案，工具挂了就重试或降级，
实在做不完就**如实报未完成并保留已有成果**，不会把失败步骤记成成功。

- **运行方式**：CLI（`python -m src.cli`），Python 3.10+，**零第三方运行时依赖**
- **离线可跑**：默认 `--planner mock`（离线策略规划器），不需要任何密钥
- **两条规划路径都端到端跑通过**：`--planner mock` 与 `--planner llm`（OpenAI 兼容接口）。
  后者目前接的是仓库内 `scripts/fake_llm_server.py` 起的本地兼容端点——真 HTTP、真 JSON 解析、
  真循环，但那个"模型"是规则模拟的，**不代表真实模型能力**，边界见 [docs/03](docs/03-evaluation.md#52-未验证项)

---

## 1. 评审入口（5 分钟路径）

| 想确认什么 | 直接看 |
| --- | --- |
| 有没有"模型决策 → 执行工具 → 观察 → 调整"的循环 | `src/agent.py` 的 `Agent.run`；`examples/case-01-normal/trace.json` 逐步记录 |
| 超预算会不会改方案 | `examples/case-01-normal/`（12 超预算 → 改 8）；`case-06`（全都超 → 报缺口） |
| 工具失败怎么处理 | `examples/case-04-tool-failure-degrade/`（重试 2 次 → 降级 → 仍交付） |
| 缺预算/截止时间会不会瞎猜 | `examples/case-02-missing-input/`（停下要信息）、`case-03`（补齐后续跑） |
| 重复执行会不会写脏数据 | `examples/case-07-repeated-save/`（幂等命中，未重复写入） |
| 有没有步数上限 | `examples/case-08-step-limit/`（3/3 步停下，状态 `INCOMPLETE_LIMIT`） |
| 真实模型那条代码路径能不能跑 | `examples/case-10-llm-endpoint/`（走真 HTTP + 真解析，7 步完成） |
| 失败步骤能不能续跑（加分项） | `case-11` 落盘失败 → `case-12` 从第 8 步续跑，只补失败那步 |
| 交付结果是否齐全 | `result.json` 的 `plan.outline`（任务计划）、`steps_brief`（步骤状态）、`plan.cost_total`（费用合计）、`saved_plan` / `status_reason` |
| 测试是否真跑过 | `examples/tests-output.txt`（49 passed）、`tests/` |
| 取舍与限制 | `docs/02-solution.md`、`docs/03-evaluation.md` |

验收信息：仓库 `https://github.com/thynan2002/XiaoYing-GuoHaoyu-AIAgent-Answer`，
最终提交 SHA 以仓库 HEAD 为准（`git rev-parse HEAD`）。

---

## 2. 环境

```bash
python --version      # 3.10+，开发环境 3.13.14
pip install -r requirements.txt   # 只装测试用的 pytest；运行本身无依赖
```

Windows / macOS / Linux 均可。命令都在仓库根目录执行。

> Windows 控制台如果出现中文乱码，先执行 `chcp 65001`；代码已对 stdout 做 UTF-8 兜底。

## 3. 运行

### 3.1 正常跑一单

```bash
python -m src.cli run --input examples/inputs/01-normal.json --out /tmp/run1
```

预期能看到：`estimate_cost(full)=12.0` 被预算 10 否决 → `estimate_cost(lite)=8.0` 通过 →
检索 2 条素材 → 生成脚本 → 保存，最后 `状态: COMPLETED`。

### 3.2 注入工具失败（脚本服务挂两次）

```bash
python -m src.cli run --input examples/inputs/04-tool-failure.json \
  --fail-tool generate_script --fail-times 2 --fail-mode error --out /tmp/run2
```

预期：脚本连续失败 2 次 → 放弃完整版 → 降级精简版 → 重新估算 8.0 → 仍能交付。

### 3.3 预算不够

```bash
python -m src.cli run --input examples/inputs/06-budget-exceeded.json --out /tmp/run3
```

预期：三个方案都被否，`状态: INCOMPLETE_BUDGET`，给出缺口 1.0，不落盘。

### 3.4 缺预算 → 停下来问 → 补齐后续跑

```bash
python -m src.cli run --input examples/inputs/02-missing-budget.json --out /tmp/run4
# 状态 NEED_INPUT，退出码 3
python -m src.cli resume --state /tmp/run4/state.json --set budget=10 --out /tmp/run5
# 从第 2 步继续跑完
```

### 3.5 一次生成全部实施例（可选）

```bash
python scripts/gen_examples.py    # 重跑 12 个用例，覆盖 examples/case-*（含本地模型端点）
```

### 3.6 测试

```bash
python -m pytest tests -q
# 49 passed
```

### 3.7 走真实模型那条路径（无需密钥也能先验证协议）

先用仓库内的本地兼容端点把链路跑一遍（两个终端）：

```bash
# 终端 A：起本地 OpenAI 兼容端点
python scripts/fake_llm_server.py --port 8765
# 终端 B：把 CLI 指过去
export OPENAI_API_KEY=dummy
export OPENAI_BASE_URL=http://127.0.0.1:8765/v1
export OPENAI_MODEL=fake-rule-model
python -m src.cli run --input examples/inputs/01-normal.json --planner llm --out /tmp/run-llm
```

换成真模型只改环境变量，代码一行不动：

```bash
export OPENAI_API_KEY=sk-...            # 真密钥
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4o-mini
python -m src.cli run --input examples/inputs/01-normal.json --planner llm --out /tmp/run-llm
```

模型只负责"下一步做什么"，工具执行、步数上限、落盘、完成校验都在 Agent 侧，
模型无法绕过去写脏数据（见 `docs/02-solution.md` 第 5 节）。

## 4. 目录

```
src/
  cli.py       命令行入口：run / resume，参数解析、把"现在"固定下来便于复现
  agent.py     ReAct 主循环、步数与工具调用上限、检查点、收尾校验
  planner.py   两个规划器：LLMPlanner（真实模型）/ MockPlanner（离线策略）
  tools.py     5 个业务工具 + 成本公式 + 故障注入
  models.py    Task / Action / Observation / StepRecord
data/
  costs.json   单价表、方案规格、工时参数（全部合成数据）
  materials.json 18 条合成素材
examples/      12 组输入 + 实际输出 + trace（见 examples/README.md）
docs/          01 需求 / 02 方案 / 03 评估 / 04 AI 使用 / 05 复盘
tests/         49 个 pytest 用例（含真 HTTP 的模型路径）
scripts/
  gen_examples.py   一键重跑全部实施例（12 个）
  fake_llm_server.py 本地 OpenAI 兼容端点，用于验证 --planner llm 的代码路径
```

## 5. 交付字段（`result.json`）

| 题目要求 | 字段 | 说明 |
| --- | --- | --- |
| 任务计划 | `plan.outline[]` | 4 道工序的任务/工时/归属费用项，费用与工时之和与 `cost_total`、`estimated_minutes` 对齐 |
| 步骤状态 | `steps_brief[]` | 每步 `action + status(ok/partial/error/terminal) + summary` |
| 工具结果 | `trace.json` 的 `observation` | 完整返回体与错误信息 |
| 费用合计 | `plan.cost_total` + `plan.breakdown[]` | 逐项明细 |
| 已保存方案 | `saved_plan{plan_id,path,deduped}` | 失败时为 `null` |
| 未完成原因 | `status` + `status_reason` | 原因里带缺口/已保留的中间产物 |

## 6. 状态码

| 状态 | 含义 | 退出码 |
| --- | --- | --- |
| `COMPLETED` | 方案已落盘且校验通过 | 0 |
| `NEED_INPUT` | 缺主题/预算/截止时间，等运营补 | 3 |
| `INCOMPLETE_BUDGET` | 所有候选都超预算或超工时，附缺口 | 4 |
| `INCOMPLETE_TOOL_FAILURE` | 关键工具不可用且无可替代方案 | 4 |
| `INCOMPLETE_SAVE` | 落盘失败，脚本与费用仍保留 | 4 |
| `INCOMPLETE_LIMIT` | 步数或工具调用到上限 | 4 |
| `INCOMPLETE_PLANNER` | 模型输出无法解析 / 状态非法 | 5 |
| `INCOMPLETE_INCONSISTENT` | 模型声称完成但校验不通过（无方案、超预算） | 4 |

## 7. 已知限制

1. **离线规划器不是大模型**：它按固定策略读观察做决策，能证明流程和失败处理正确，
   不能证明真实模型的决策质量。`--planner llm` 的代码路径已用本地兼容端点端到端跑通
   （`case-10`），但"模型会怎么选"仍未在真实模型上验证。
2. 素材与费用全是合成数据；脚本由模板拼装，不是真实文案能力。
3. 单机单进程，没有并发控制；幂等去重靠文件名 + 内容指纹，跨机器不保证。
4. 未做界面与线上部署（考核说明中非必需）。
