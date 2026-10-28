# 03 · 评估

所有数字都来自仓库内可直接重跑的文件：`examples/case-*/result.json`、
`examples/case-*/trace.json`、`examples/tests-output.txt`。
重跑方式：`python scripts/gen_examples.py && python -m pytest tests -q`。

## 1. 九个用例：预期 vs 实际

| # | 用例 | 输入要点 | 注入 | 预期 | 实际 | 关键数字 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | [case-01-normal](../examples/case-01-normal/) | 预算 10，截止 11-02 | 无 | 12 被否 → 改 8 → 交付 | `COMPLETED` | full 12.0 否决；lite 8.0；7 步 / 6 次调用 |
| 2 | [case-02-missing-input](../examples/case-02-missing-input/) | 无 budget | 无 | 停下要预算 | `NEED_INPUT` | 1 步，0 次工具调用，退出码 3 |
| 3 | [case-03-resume-after-input](../examples/case-03-resume-after-input/) | 承接 case-02 + `--set budget=10` | 无 | 从断点续跑并交付 | `COMPLETED` | 8 步（含前次的 1 步），plan_139ae1abe5 |
| 4 | [case-04-tool-failure-degrade](../examples/case-04-tool-failure-degrade/) | 预算 14 | `generate_script` 连续 2 次 503 | 重试 → 降级 → 仍交付 | `COMPLETED` | full 12.0 选中 → 素材 2/4 重算 10.8 → 脚本失败 2 次 → lite 8.0 交付；11 步 / 10 次调用 |
| 5 | [case-05-partial-materials](../examples/case-05-partial-materials/) | 预算 12 | `search_materials` 只回 1 条 | 按实际素材重算后交付 | `COMPLETED` | 12.0 → 10.2；脚本标记 `partial`；7 步 |
| 6 | [case-06-budget-exceeded](../examples/case-06-budget-exceeded/) | 预算 4 | 无 | 无可行方案，报缺口，不落盘 | `INCOMPLETE_BUDGET` | 12.0 / 8.0 / 5.0 全否，缺口 1.0，`saved_plans/` 为空 |
| 7 | [case-07-repeated-save](../examples/case-07-repeated-save/) | 与 case-01 同输入 | 无 | 幂等命中，不写第二份 | `COMPLETED` | `deduped: true`，plan_aa0a4621d5，目录里只有 1 个文件 |
| 8 | [case-08-step-limit](../examples/case-08-step-limit/) | 与 case-01 同输入 | `--max-steps 3` | 到上限停，状态明确 | `INCOMPLETE_LIMIT` | 3/3 步；理由写明"已否 1 个方案" |
| 9 | [case-09-tight-deadline](../examples/case-09-tight-deadline/) | 预算 20，可用 10 分钟 | 无 | 耗时超标的档位被否 | `COMPLETED` | full 12 分钟 > 10 → lite 7 分钟 → 8.0 交付 |

每个目录都包含：`input.json`（输入）、`command.txt`（实际命令）、`stdout.txt`（人看的输出）、
`result.json`（机读结果）、`trace.json`（逐步记录）、`state.json`（检查点）、
`saved_plan.json`（成功时才有）。

## 2. 验收点覆盖矩阵

| 验收点 | 覆盖用例 | 辅助证据 |
| --- | --- | --- |
| 正常完成 | 01, 09 | `tests/test_agent_loop.py::TestHappyPath` |
| 缺预算 | 02 → 03 | `TestMissingInput`（含缺 deadline） |
| 工具失败 | 04 | `TestToolFailure`（含"全工具挂掉"） |
| 预算超限 | 06 | `TestBudget`（含"刚好等于预算"） |
| 重复执行 | 07 | `TestRepeat`（第二次 `deduped=true`，目录只 1 文件） |
| 步数上限 | 08 | `TestLimit`（步数 + 工具调用两种上限） |
| 部分成功保留 | 05 | 04 的素材与估算在降级后仍被复用 |
| ReAct 循环 | 01, 04, 05, 09 | 每步都有 `decision_basis` + `observation` + `follow_up` |
| 失败不记成功 | 04 | `TestToolFailure::test_failure_never_marked_success` |
| 假完成拦截 | — | `TestFinishGuard`（假规划器声称完成被改判） |

## 3. 测试结果

`python -m pytest tests -q` → **38 passed in 0.41s**（完整输出见 `examples/tests-output.txt`）。

| 测试文件 | 关注点 | 用例数 |
| --- | --- | --- |
| `tests/test_tools_cost.py` | 费用公式可独立复算（12/8/5、明细求和、按条数递减）、检索边界、保存前二次核算、幂等 | 12 |
| `tests/test_agent_loop.py` | 正常链路、缺输入、续跑、工具失败与降级、预算缺口、重复执行、双上限、假完成拦截 | 17 |
| `tests/test_planner.py` | 模型输出解析（裸 JSON / 代码块 / 夹带散文 / 未知动作 / 非 JSON / args 类型）、mock 策略分支 | 9 |

费用测试不只断言 `total == 12.0`，而是用单价表手算一遍再比对（`test_manual_recompute_matches`），
这样公式实现漂移会立刻被抓住，而不是"改公式改测试一起改"。

## 4. 开发过程中真实出现的失败（与修复）

这部分是运行时真的踩到的，不是设计推演：

1. **续跑没生效**：第一版 `Agent.run` 在 `resume` 时用 `state.json` 里的旧任务覆盖了 CLI 的 `--set`，
   结果补齐预算后仍然输出 `NEED_INPUT`，trace 里出现两条连续的 `request_input`
   （第 1 次实现的实际输出已不可复现，修复后为 `case-03` 的 8 步）。
   修复：以调用方传入的任务为准，检查点只保留进度不覆盖新输入。
2. **幂等"意外"命中**：第一次跑 case-01 就返回 `deduped: true`，因为同目录上一轮试跑已经存过。
   这说明**实施例之间不能共享方案目录却不控顺序**。修复：`gen_examples.py` 先清空
   `data/saved_plans/`，再按固定顺序执行（case-01 先跑，case-07 才故意命中）。
3. **检索 0 命中伪装成工具故障**：`case-09` 最初的主题是"展会现场快剪"，素材标签一个都没命中，
   Agent 走了"0 命中 → 换词 → 仍未命中 → `INCOMPLETE_TOOL_FAILURE`"。
   流程没 bug，但用例失去意义。修复：把主题改成"城市展会现场快剪"，同时保留"0 命中换词重试"
   这条策略（它本身就是合理的兜底）。这也暴露了 mock 的直接弱点——它只会用标签命中来抽关键词。

## 5. 结果解读

### 5.1 能证明什么

- 预算/工时判断发生**在工具返回值之后**：case-01 的第 3 步依据里写着 `12.0 超预算 10.0`，
  这是运行时读到的数字，不是预设文字。
- 失败后**计划真的变了**：case-04 从 full（10.8）退到 lite（8.0），并且第 5、6 步如实记 error，
  第 7 步的依据写明"脚本连续失败 2 次，放弃完整版"。
- 未完成时**不落盘**：case-06 / case-08 的 `saved_plans/` 与输出目录都没有 `saved_plan.json`。
- 重复执行**不改数据**：case-07 只命中已有方案。

### 5.2 未验证项（明确边界）

| 项 | 状态 | 影响 |
| --- | --- | --- |
| 真实大模型的决策质量 | **未验证**：本机无密钥，`--planner llm` 只做了代码与解析层单测 | 不能声称"模型会像 mock 一样选 8 的方案"；只能声称接得上、约束得住 |
| 模型对提示词的鲁棒性 | 未验证 | 真实模型可能不填 `decision_basis`、可能重复调同一工具（会被上限拦住） |
| 并发 / 多用户 | 未实现 | 幂等靠本地文件名，多机共享目录时不保证 |
| 脚本质量 | 模板拼装 | 只验证"有结构化产出"，不评估文案好坏 |

真实模型这条路径的设计是"最小可替换"：`build_planner("llm")` 换掉一个类，其余（工具、上限、
校验、落盘）完全不变。在拿到密钥后需要用 `examples/inputs/01-normal.json` 跑一次并补一份 trace，
才算完成真实验证——这是本仓库**已知的缺口**，写在这里而不是藏起来。

## 6. 限制与下一步

- 素材库只有 18 条，检索靠标签子串匹配，覆盖面窄；0 命中时只有一次换词机会。
- 工时模型是线性的（脚本 45s + 每条素材/每个镜头 60s + 审核 30s），没有并行与排队。
- 未做真实计费对接与人工审核回写。
