# 02 · 方案与取舍

## 1. 整体结构

```
CLI (src/cli.py)
  └─ 解析任务 → 算 available_minutes → 选 planner → Agent.run
                        │
        ┌───────────────┴────────────────┐
        │  Planner（决策）                │  Agent（执行/约束）
        │  ├ LLMPlanner（真实模型）       │  ├ 步数 & 工具调用上限
        │  └ MockPlanner（离线策略）      │  ├ 每步落检查点 state.json
        └───────────────┬────────────────┘  ├ 收尾校验（不许假完成）
                        ▼                    └ 汇总 result.json / trace.json
                 ToolRegistry（5 个工具 + 故障注入）
                   list_plan_variants / search_materials / estimate_cost
                   generate_script / save_plan
                        │
                   data/costs.json, data/materials.json, data/saved_plans/
```

职责切得很硬：**规划器只能"说下一步做什么"**，不能直接读写文件、不能跳过工具、不能自己宣布成功。
这条边界是后面所有失败处理与审计能成立的前提。

## 2. 一次运行的调用流程

以 `examples/case-01-normal`（预算 10）为例，`trace.json` 里是 7 步：

| 步 | 动作 | 关键参数 | 观察（摘要） | 依据 |
| --- | --- | --- | --- | --- |
| 1 | `list_plan_variants` | topic=秋季校园咖啡新品推广 | 3 个档位：完整/精简/极简 | 先拿候选，费用必须现算 |
| 2 | `estimate_cost` | variant=full | 12.0，约 12 分钟 | 完整度优先 |
| 3 | `estimate_cost` | variant=lite | 8.0，约 7 分钟 | 12.0 超预算 10.0，退一档 |
| 4 | `search_materials` | queries=[咖啡,校园], limit=2 | 命中 2/2 | 8.0 可行且 7 分钟够，选定 |
| 5 | `generate_script` | variant=lite, 2 条素材 | 3 镜头脚本 | 素材齐了 |
| 6 | `save_plan` | cost_total=8.0, script | plan_aa0a4621d5 | 脚本就绪 |
| 7 | `finish` | COMPLETED | — | 落盘成功 |

第 3 步的依据里带着第 2 步的真实数字（12.0 > 10.0），这是"观察 → 调整"的最小可见证据。
完整文件：`examples/case-01-normal/trace.json`，人类可读版：`examples/case-01-normal/stdout.txt`。

### 2.1 任务计划长什么样

题目要求交付"任务计划"。它不是另起一套东西，而是**已选方案的工序拆解**，由费用工具一起算出来
（`result.json → plan.outline`，lite 档示例）：

| # | 工序 | 预计工时 | 归属费用项 | 费用 |
| --- | --- | --- | --- | --- |
| 1 | 素材检索与授权 | 2.0 min | 素材授权 | 1.2 |
| 2 | 脚本撰写（含配音文案） | 0.8 min | 脚本撰写 + 配音 | 3.1 |
| 3 | 剪辑合成（含配音合成） | 3.2 min | 剪辑合成 | 3.2 |
| 4 | 内容审核 | 0.5 min | 内容审核 | 0.5 |
| | **合计** | **7 min** | | **8.0** |

两条自洽性由测试守着：`sum(outline.cost) == cost_total`、
`ceil(sum(outline.seconds)/60) == estimated_minutes`（`tests/test_tools_cost.py::TestTaskPlanOutline`）。
这样"计划里的钱和时间"与"报价单"不可能各说各话。

### 2.2 步骤依赖

| 工序 | 依赖 | 违反时的行为 |
| --- | --- | --- |
| `estimate_cost` | 无（只需 variant_id） | 未知档位 → error，换下一档 |
| `search_materials` | 已选定档位（决定需要几条素材） | 素材不足 → `partial` + 按实际条数重算费用 |
| `generate_script` | 已有素材 id 列表 | 素材为空/无效 → error，触发重试/降级 |
| `save_plan` | 已有脚本 + 已达成的费用 | 缺字段或费用与实算不符 → **拒绝保存** |

依赖不是靠"模型自觉"维持的：工具自己校验入参（缺 `script` 直接报错），
`save_plan` 再核算一次费用，所以"脚本没生成却保存成功"这种状态在结构上不成立。

## 3. 动态决策：观察到什么就改什么

同一个循环里，策略把"上一步观察"映射到"下一步动作"，而不是照脚本念：

| 观察 | 调整 | 落地用例 |
| --- | --- | --- |
| `estimate.total > budget` | 记录否决原因，换更小档位重新估算 | case-01（12→8）、case-06（全否→缺口） |
| `estimate.estimated_minutes > available_minutes` | 同上，但原因写成耗时超标 | case-09（12 分钟 > 可用 10 分钟 → 7 分钟档） |
| `estimate.error` | 该档位记为"估算失败"，换下一档 | 单测覆盖 |
| `search.status = partial`（返回 1/4） | 按实际条数**重算费用**再决定是否继续 | case-05（12.0 → 10.2，仍交付） |
| `search.returned = 0` | 换主题首词再检索一次；仍 0 则报未完成 | case-09 早期版本触发过，见 05 复盘 |
| `generate_script.error` ×1 | 参数不变重试 1 次 | case-04 第 5、6 步 |
| `generate_script.error` ×2 | 放弃当前档位，降级到下一档并重算费用 | case-04 第 7 步（full→lite，10.8→8.0） |
| `save_plan.error` | 重试 1 次；再失败 → `INCOMPLETE_SAVE`，保留脚本与估算 | 单测覆盖 |
| `save_plan.deduped = true` | 直接完成，注明"幂等命中" | case-07 |
| 缺 `budget`/`deadline` | 不发工具，直接 `request_input` | case-02 |

上表是 Mock 规划器的策略；换成 LLM 只改"谁来做比较"，工具返回、上限、校验、落盘都不变。
`case-10` 用真实 HTTP 的模型接口跑出了同一条 12.0 被否 → 8.0 交付的路径，且 Agent 侧
自动记下了 `rejected: full 12.0 超预算 10.0`；`case-13` 则在同一 LLM 路径上注入脚本失败两次，
观察它从 full 12.0 改算 lite 8.0，素材与估算都没有丢。

**为什么只重试 1 次就降级**：重试成本低但收益衰减快；同一工具连续两次失败大概率是服务侧问题，
继续原地重试会把步数烧光。降级到更小档位能用更少的工具职责完成交付，是更有产出的选择。
上限（12 步 / 12 次调用）兜住最坏情况。

## 4. 工具契约

| 工具 | 入参 | 返回 | 失败形态 |
| --- | --- | --- | --- |
| `list_plan_variants` | topic, duration_preference? | 3 个档位（**不含费用**） | 服务不可用 → error |
| `search_materials` | queries[], limit | items[], returned, degraded | 空 queries → error；0 命中 → `partial` |
| `estimate_cost` | variant_id, material_ids? | breakdown[], total, estimated_minutes | 未知档位 → error |
| `generate_script` | variant_id, material_ids[], tone, topic | script{shots[]} | 素材为空/无效 → error；素材不足 → `partial` |
| `save_plan` | topic, variant_id, material_ids[], cost_total, script | plan_id, path, deduped | 费用与实算不符 → **拒绝保存** |

几点刻意的设计：

1. **`list_plan_variants` 不返回价格**。价格必须走 `estimate_cost`，逼模型"先报价再决策"，
   也让费用在 trace 里有独立可核对的步骤。如果候选自带价格，第 2、3 步就没意义了。
2. **`save_plan` 二次核算**。模型可能记错或抄错数字，落盘前用同一套公式重算，
   差 0.01 就拒绝。这样"费用合计"这一列不可能被模型污染。
3. **`estimate_cost` 支持 `material_ids` 覆盖**。素材不够时，费用要按真实条数算，
   否则会拿"4 条素材的报价"去交付"1 条素材的方案"，属于虚报。
4. **检索接受整句主题**。运营给的是"秋季校园咖啡新品推广"而不是关键词，
   所以长 query 用字符二元组打分（`_score`），短 query 仍走子串匹配。
   这样"模型只把主题原样丢进检索"也能命中，不至于因为不会分词而 0 素材。

## 5. 四道工程闸门

| 闸门 | 位置 | 行为 |
| --- | --- | --- |
| 步数 / 工具调用上限 | `Agent.run` 循环入口 | 到顶 → `INCOMPLETE_LIMIT`，`status_reason` 里写清保留了哪些中间产物 |
| 收尾校验 | `Agent._finish` | 用统一口径 `_artifacts()` 读取进度；声称 `COMPLETED` 但（无 plan_id / 无脚本 / 无有效报价 / 费用超预算 / 工时超可用）→ 改判 `INCOMPLETE_INCONSISTENT` |
| 状态白名单 | `Agent.run` | 模型给出非法状态（如 `DONE`）→ `INCOMPLETE_PLANNER` |
| 检查点 | `Agent._checkpoint` | 每步落 `state.json`；续跑只补未完成动作，不重跑已成功工序 |

这些闸门是"模型不可信"假设的体现：**决策权交给模型，验收权留在代码里**。
单测 `TestFinishGuard` 用三个假规划器验证了闸门真的会拦：什么都不做、产物齐全但超预算、
产物齐全但超工时。

## 6. 幂等与续跑

- **保存幂等**：`plan_id = sha1(topic + variant + sorted(material_ids) + cost)[:10]`，
  文件已存在就直接返回 `deduped=true`。不使用时间戳或随机 UUID，因为它们会让"同一份内容"变成多份。
- **检查点**：每步结束写 `state.json`（先写 `.tmp` 再 `replace`，避免半截文件），
  内容包含任务、规划器工作区、步数、工具调用数、完整 trace。
- **续跑**：`resume --state ... --set budget=10` 载入检查点、补齐字段、从第 N+1 步继续。
  `--set` 可覆盖任意任务字段，因此"缺信息 → 补充 → 续跑"和"工具恢复后重跑失败步骤"是同一套机制。
- **续跑是真的从断点继续，不是重跑一遍**：检查点里同时保存了 `last_action`、`last_observation`
  与 `progress` 台账，续跑时规划器接着"上一次观察"判断，而不是回到第 1 步重新拉候选。
  `case-12` 里第 9 步直接是 `save_plan`（补上失败那步），`search_materials` / `generate_script`
  在整条 trace 里各只出现 1 次。
- **续跑会把"连续失败计数"清零**：人工介入本身就是一个新的重试窗口；不清零的话，
  续跑会立刻再次触发"降级/收尾"，等于没续跑。这条在 `Agent.run` 的 resume 分支里显式处理。
- **口径统一**：检查点里的 `progress`（每类工具最近一次成功返回）与 `rejected_auto`
  （按真实估算/失败自动记下的被否候选）都由 Agent 维护，与规划器实现无关。
  收尾校验、结果报告、续跑都从这里读，所以换成 LLM 规划器也不会出现"做完了却判不出完成"
  或"被否方案为空"——这两个坑在接 `--planner llm` 时都真的踩到过（见 03 第 4 节）。
- **规划器看得见台账**：`DecisionContext` 把 `chosen_variant` / `rejected` /
  `same_tool_failures` 一并交给规划器。Mock 用自己的队列，LLM 用这份台账，二者不共享实现，
  只共享事实。

## 7. 技术取舍

### 7.1 为什么原生实现，不用 LangGraph / ADK / AgentScope

- 本题的核心是**状态机与失败处理**，恰恰是框架"帮你藏起来"的部分。用框架能更快串起来，
  但评审问"哪一步由谁决定、上限在哪、假完成怎么拦"时，答案会变成"框架内部"。
- 原生实现约 1400 行（含 CLI、循环、两个规划器与 5 个工具），无第三方运行时依赖，
  `python -m src.cli` 直接能跑，评审不用装环境。
- 反过来，如果需求扩到多 Agent 协作、长会话记忆、可视化调试，我会选 LangGraph：
  它的中断/恢复与图状态是现成的，本仓库的 `state.json` 那套就是它的简化版。

### 7.2 为什么默认用离线规划器

本机没有模型密钥，如果只有真实模型路径，评审拿到仓库第一步就卡在 `OPENAI_API_KEY`。
所以做两个规划器共享同一 `decide(ctx)` 接口：

- `MockPlanner`：**读真实工具返回值**做决策（不是固定脚本），因此能验证"观察 → 调整"与全部失败分支；
- `LLMPlanner`：同一接口，换成 `/chat/completions` + JSON 输出。

代价与边界必须说清：mock 的策略是人写的，它证明的是**流程与工程约束正确**，
不证明"大模型在同样输入下也会这样决策"。`docs/03` 把它列为未验证项，不混为一谈。

### 7.3 为什么费用全部由工具算

让模型心算 5 项单价、按 30 秒分段取整、再求和，是稳定出错的经典场景。
把公式收进 `compute_cost`，模型只做比较（12 > 10），出错面从"算术"缩到"比较"，
而且比较错了还会被 `save_plan` 的二次核算拦住。

### 7.4 为什么降档位而不是降参数

脚本失败时也可以"减少镜头数、去掉配音"来省钱，但那会改变档位规格，费用要另算一套，
预算校验会变得不可解释。降档位是把"不可行的组合"换成"库里已有的可行组合"，
费用仍然来自同一张单价表，评审能逐项手算复核。

## 8. 与"参考实例"的对应

题目参考实例：预算 10、初始方案 12、替代方案 8，观察超预算后改选再保存。
本实现即 `case-01`：12.0 被否 → 8.0 通过 → 保存；若没有可行方案（`case-06`，预算 4、最低 5）
则返回未完成与缺口 1.0，且目录里没有任何落盘文件。
