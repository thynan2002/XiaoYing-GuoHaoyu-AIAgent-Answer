# 03 · 评估

所有数字都来自仓库内可直接重跑的文件：`examples/case-*/result.json`、
`examples/case-*/trace.json`、`examples/tests-output.txt`。
重跑方式：`python scripts/gen_examples.py && python -m pytest tests -q`；
提交前再用 `python scripts/verify_submission.py` 做一次不联网的交付物自检。

## 1. 十三个用例：预期 vs 实际

| # | 用例 | 输入要点 | 注入 | 预期 | 实际 | 关键数字 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | [case-01-normal](../examples/case-01-normal/) | 预算 10 | 无 | 12 被否 → 改 8 → 交付 | `COMPLETED` | full 12.0 否决；lite 8.0；7 步 / 6 次调用 |
| 2 | [case-02-missing-input](../examples/case-02-missing-input/) | 无 budget | 无 | 停下要预算 | `NEED_INPUT` | 1 步，0 次工具调用，退出码 3 |
| 3 | [case-03-resume-after-input](../examples/case-03-resume-after-input/) | 承接 02 + `--set budget=10` | 无 | 补齐后续跑并交付 | `COMPLETED` | 8 步（含前次 1 步），plan_139ae1abe5 |
| 4 | [case-04-tool-failure-degrade](../examples/case-04-tool-failure-degrade/) | 预算 14 | `generate_script` 连续 2 次 503 | 重试 → 降级 → 仍交付 | `COMPLETED` | full 12.0 选中 → 素材 2/4 重算 10.8 → 脚本失败 2 次 → lite 8.0；11 步 |
| 5 | [case-05-partial-materials](../examples/case-05-partial-materials/) | 预算 12 | `search_materials` 只回 1 条 | 按实际素材重算后交付 | `COMPLETED` | 12.0 → 10.2；脚本标记 `partial` |
| 6 | [case-06-budget-exceeded](../examples/case-06-budget-exceeded/) | 预算 4 | 无 | 无可行方案，报缺口，不落盘 | `INCOMPLETE_BUDGET` | 12.0 / 8.0 / 5.0 全否，缺口 1.0，`plan` 为 `null` |
| 7 | [case-07-repeated-save](../examples/case-07-repeated-save/) | 与 01 同输入 | 无 | 幂等命中，不写第二份 | `COMPLETED` | `deduped: true`，目录里只有 1 个文件 |
| 8 | [case-08-step-limit](../examples/case-08-step-limit/) | 与 01 同输入 | `--max-steps 3` | 到上限停，状态明确 | `INCOMPLETE_LIMIT` | 3/3 步；理由写明"已否 1 个方案" |
| 9 | [case-09-tight-deadline](../examples/case-09-tight-deadline/) | 预算 20，可用 10 分钟 | 无 | 耗时超标的档位被否 | `COMPLETED` | full 12 分钟 > 10 → lite 7 分钟 → 8.0 |
| 10 | [case-10-llm-endpoint](../examples/case-10-llm-endpoint/) | 预算 10 | 无（接本地兼容端点） | 走真 HTTP 的模型路径也能完成 | `COMPLETED` | `planner: llm`，7 步，12.0 → 8.0 |
| 11 | [case-11-save-failure](../examples/case-11-save-failure/) | 预算 10 | `save_plan` 持续 503 | 报未完成，但保住素材/脚本/费用 | `INCOMPLETE_SAVE` | 8 步；`saved_plan` 为 `null`；素材 2 条 + 脚本仍在 |
| 12 | [case-12-resume-after-save-failure](../examples/case-12-resume-after-save-failure/) | 承接 11 的检查点 | 无（工具恢复） | 只补失败那步 | `COMPLETED` | 第 9 步直接 `save_plan`；检索/脚本全链路各只 1 次 |
| 13 | [case-13-llm-tool-failure](../examples/case-13-llm-tool-failure/) | 预算 14 | `generate_script` 连续 2 次 503 | 同一 LLM 路径重试 → 降级 → 仍交付 | `COMPLETED` | full 12.0 → 素材 2/4 重算 10.8 → 失败 ×2 → lite 8.0；10 步；`planner: llm` |

每个目录都包含：`input.json`（输入）、`command.txt`（实际命令）、`stdout.txt`（人看的输出）、
`result.json`（机读结果）、`trace.json`（逐步记录）、`state.json`（检查点）、
`saved_plan.json`（成功时才有）。

## 2. 验收点覆盖矩阵

| 验收点 | 覆盖用例 | 辅助证据 |
| --- | --- | --- |
| 正常完成 | 01, 09, 10 | `TestHappyPath` |
| 缺预算 | 02 → 03 | `TestMissingInput`（含缺 deadline） |
| 工具失败 | 04, 11 | `TestToolFailure`（含"全工具挂掉"） |
| 预算超限 | 06 | `TestBudget`（含"刚好等于预算"） |
| 重复执行 | 07 | `TestRepeat`（第二次 `deduped=true`，目录只 1 文件） |
| 步数上限 | 08 | `TestLimit`（步数 + 工具调用两种上限） |
| 部分成功保留 | 05 | 04 的素材与估算在降级后仍被复用 |
| ReAct 循环 | 01, 04, 05, 09, 10 | 每步都有 `decision_basis` + `observation` + `follow_up` |
| 真实模型代码路径 | 10, 13 | `TestLLMHttpPath`（真 HTTP，不打桩） |
| LLM 路径的工具失败调整 | 13 | 重试 2 次 → 降级 lite；`rejected` 含 full |
| 失败步骤续跑（加分） | 11 → 12 | `TestResumeFromFailure`（断言续跑不重跑检索/脚本） |
| 失败不记成功 | 04, 11 | `test_failure_never_marked_success` |
| 假完成拦截 | — | `TestFinishGuard`（假规划器声称完成被改判） |
| 任务计划自洽 | 全部成功用例 | `TestTaskPlanOutline`（费用/工时与报价单对齐） |

## 3. 测试结果

`python -m pytest tests -q` → **56 passed in 2.3s**（完整输出见 `examples/tests-output.txt`）。

| 测试文件 | 关注点 | 用例数 |
| --- | --- | --- |
| `tests/test_tools_cost.py` | 费用公式可独立复算（12/8/5、明细求和、按条数递减）、任务计划自洽、检索边界（整句主题 / 噪声查询）、保存前二次核算、幂等、错误参数不崩 | 20 |
| `tests/test_agent_loop.py` | 正常链路、缺主题/预算/截止时间、续跑、工具失败与降级、部分成功重算、预算缺口、重复执行、双上限、假完成/超预算/超工时拦截 | 20 |
| `tests/test_planner.py` | 模型输出解析（裸 JSON / 代码块 / 夹带散文 / 未知动作 / 非 JSON / args 类型 / 缺依据兜底）、mock 策略分支 | 11 |
| `tests/test_integration.py` | 真 HTTP 的模型路径、LLM 路径被否记录、端不可达时的收场、失败步骤续跑、交付字段完整性 | 5 |

费用测试不只断言 `total == 12.0`，而是用单价表手算一遍再比对（`test_manual_recompute_matches`），
这样公式实现漂移会立刻被抓住，而不是"改公式改测试一起改"。

## 4. 开发过程中真实出现的失败（与修复）

这部分是运行时真的踩到的，不是设计推演。

### 4.1 交付结果缺字段（自查发现）

第一版 `result.json` 只有状态、方案、费用、用量，**没有"任务计划"和"步骤状态"**——
而题目要求交付结果里必须有这两项。修复：`estimate_cost` 顺带算出工序清单
（`plan.outline`，工时与费用之和与报价单对齐），`result.steps_brief` 输出每步成败摘要。

### 4.2 续跑其实是"重跑"（自查发现）

`resume` 虽然从第 N+1 步编号继续，但 `last_action` 没进检查点，规划器回到"第一步"，
于是又把候选、估算、检索跑了一遍——`case-03` 的 8 步里第 2 步仍是 `list_plan_variants`。
这在"缺信息补齐"场景看不出来（那时本来没有进度），但**失败步骤续跑**（加分项）就不成立了。
修复：检查点保存 `last_action` / `last_observation` / `progress`，续跑接着上次观察判断；
并把"连续失败计数"清零（人工介入 = 新的重试窗口）。现在 `case-12` 第 9 步直接是 `save_plan`。

### 4.3 接 LLM 规划器才暴露的三个坑

1. **收尾校验读的是 mock 的私有状态**：`_finish` 从 `scratch["saved"]/["script"]` 取进度，
   只有 `MockPlanner` 会写这些键；换成 `LLMPlanner` 后任务明明做完了，却被判成
   `INCOMPLETE_INCONSISTENT`（`case-10` 首次运行就是这样）。
   修复：引入 Agent 侧 `progress` 台账（每类工具最近一次成功返回），`_artifacts()` 统一口径。
2. **`SYSTEM_PROMPT` 里的 JSON 花括号被 `str.format` 当占位符**，第一次调用直接
   `KeyError: '"action"'`。修复：改用 `{{tools}}` + `replace`。
3. **本地模型端点被系统代理劫走**：请求 `127.0.0.1:8765` 返回 502
   "upstream connect failed"，因为 urllib 默认走 `http_proxy`。
   修复：目标 host 是回环地址时用空 `ProxyHandler`——接 ollama / vLLM 也会遇到同一个坑。

### 4.4 更早的两处（保留原记录）

1. **幂等"意外"命中**：第一次跑 case-01 就 `deduped: true`，因为同目录上一轮试跑已存过。
   修复：`gen_examples.py` 先清空 `data/saved_plans/`，再按固定顺序执行。
2. **检索 0 命中伪装成工具故障**：`case-09` 最初主题"展会现场快剪"没有标签命中，
   走了"0 命中 → 换词 → 仍未命中 → 未完成"。修复：主题改成"城市展会现场快剪"，
   同时给检索加"长 query 用字符二元组打分"，让整句主题也能命中。

### 4.5 提交前自查发现并修掉的问题

1. **重复代码**：`src/agent.py` 的 `chosen_variant` 赋值重复；`tests/test_tools_cost.py`
   末尾整段重复了两个测试类。因为 Python 后定义的类会覆盖前一个，`pytest` 实际只收集后一份，
   前一份是**看起来测了、其实从未执行**的死代码。已删除；新增 7 个针对性用例后，测试数
   从 49 变为 56，且每个收集到的用例都真实存在。
2. **重复文档与占位符**：`docs/05` 的 6.1 节重复，`docs/04` 留着"待本人确认"占位符。已重写，
   并让 `verify_submission.py` 把占位符当成失败项。
3. **命令不可长期复现**：README 的命令没带 `--now`，输入截止时间是 2026-11-02；过了这天
   "可用分钟"归零，正常用例会失败。所有命令已固定 `--now`，落盘路径改为相对路径。
4. **换规划器丢状态**：LLM 路径的 `result.rejected` 原本为空。改为 Agent 按真实工具返回值
   自动记账，`case-10` 现在能看到 full 12.0 超预算 10.0；`same_tool_failures` 也传给了模型。
5. **工具参数防御**：模型给未知档位时 `generate_script` 原先会抛 `KeyError` 打崩循环；
   现在返回 error 观察。重复的 `material_ids` 也不再重复计费。

## 5. 结果解读

### 5.1 能证明什么

- 预算/工时判断发生**在工具返回值之后**：`case-01` 第 3 步依据写着 `12.0 超预算 10.0`，
  `case-10`（LLM 规划器）第 3 步依据写着 `完整版 被否（费用 12.0 超预算 10.0），换 lite 再算`——
  都是运行时读到的数字，不是预设文案。
- **被否方案有独立台账**：`case-01` / `case-10` 的 `rejected` 都含
  `{"variant":"full","reason":"费用 12.0 超预算 10.0"}`；`case-04` 还记了
  "脚本工具连续失败 2 次，放弃该方案"。这份台账由 Agent 从工具返回值生成，不依赖 mock。
- 失败后**计划真的变了**：`case-04` 从 full（10.8）退到 lite（8.0），第 5、6 步如实记 error，
  第 7 步依据写明"脚本连续失败 2 次，放弃完整版"。
- 部分成功**不会丢结果**：`case-05` 只回 1/4 条素材，第 4 步先按 1 条重算 10.2，再写脚本、保存。
- 未完成时**不落盘**：`case-06` / `case-08` / `case-11` 的 `saved_plan` 均为 `null`；
  `case-11` 仍保留素材、脚本与 8.0 的估算。
- 重复执行**不改数据**：`case-07` 只命中已有方案，目录仍 1 个文件。
- **失败步骤续跑**：`case-12` 第 9 步直接 `save_plan`，检索与脚本在整条 trace 里各只 1 次。
- **模型那条代码路径真的在跑**：`case-10` 走的是 `urllib → HTTP → choices[0].message.content
  → JSON 解析 → Action`，不是本地函数调用；`TestLLMHttpPath` 用随机端口起服务再断言。
- **同一 LLM 路径也能处理工具失败**：`case-13` 的 `planner` 同样是 `llm`，素材 2/4 时先重算
  10.8，脚本失败 2 次后从 full 降到 lite 8.0，素材与估算保留在 `artifacts`/`progress` 里。
- **提交物自洽可查**：`python scripts/verify_submission.py` 不联网核对 13 个实施例、
  必验场景、trace 字段与文档占位符，当前全部通过。

### 5.2 未验证项（明确边界）

| 项 | 状态 | 影响 |
| --- | --- | --- |
| 真实大模型的决策质量 | **未验证**：`case-10` 的"模型"是 `scripts/fake_llm_server.py` 里的规则 | 能说"接口与循环接得上"，不能说"真实模型也会选 8 的方案" |
| 真实模型对提示词的鲁棒性 | 未验证 | 真模型可能漏 `decision_basis`（已做兜底）、可能反复调同一工具（会被上限拦住） |
| LLM 路径的被否记录 | 已验证（代码路径） | 由 Agent 侧统一记账，不依赖模型自己写；`case-10` 有完整 `rejected` |
| 并发 / 多用户 | 未实现 | 幂等靠本地文件名，多机共享目录时不保证 |
| 脚本质量 | 模板拼装 | 只验证"有结构化产出"，不评估文案好坏 |

真实模型这条路径的设计是"最小可替换"：换 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
三个环境变量即可，工具、上限、校验、落盘完全不变。**真模型跑一次并补一份 trace，
仍是本仓库已知的缺口**——写在这里而不是藏起来。

## 6. 限制与下一步

- 素材库 18 条，检索靠标签 + 二元组匹配，覆盖面窄；0 命中时只有一次换词机会。
- 工时模型是线性的（脚本 45s + 每条素材/每个镜头 60s + 审核 30s），没有并行与排队。
- 未做真实计费对接与人工审核回写。
- 续跑的粒度是"从最后一个动作继续"，不是"只重跑某一步"；一次运行里多次失败仍会吃掉步数
  （`case-04` 用了 11/12 步）。
