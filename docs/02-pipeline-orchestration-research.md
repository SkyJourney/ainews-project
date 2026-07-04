# 流水线编排框架选型调研

> 对应 [00-overview.md](./00-overview.md) 结论摘要第二行。调研范围：如何编排一条多阶段 LLM 流水线，且每个阶段的并行子任务数量是**运行时动态决定**的（fetch 阶段 N = 活跃信息源数；originalize 阶段 M = 当天聚类条目数），需要在无人值守下可靠运行（重试、部分失败处理、可观测性）。定时触发框架已确定为 Celery，本调研的任务是确定"流水线本身怎么编排"，以及它和 Celery 的关系定位。

> **2026-07-04 修订**：初版把"持久化编排"和"单个 agent 的 LLM 执行"这两层的执行体都指向了 Claude Agent SDK，这是个错误——用户明确要避免被单一模型厂商的 Agent 框架锁死，且已有自建 LiteLLM 做多模型统一转发（OpenAI/Anthropic 及其他端点均可）。本次修订把执行层从 Claude Agent SDK 换成"直连 LiteLLM + 可选 Instructor 做结构化输出校验"，并补充 Temporal vs LangChain vs LangGraph 的完整选型矩阵。

## 一、先把需求拆成两层

关键是不要把两件事混为一谈：

1. **持久化编排层**——谁来保证"起 N 个子任务、其中第 7 个静默失败、自动重试或补偿、事后能查是哪几个失败"。这是现有 AInews 管道痛点的根源（originalizer 覆盖率不足靠人工补跑、发布记录靠事后补记），必须由这一层解决。
2. **单个任务的 LLM 调用**——每个 fetcher / originalizer / cluster / digest 内部实际调模型做抓取、翻译、归纳、判断的那一步。回头看现有 8 个 subagent，真正需要"自主多轮工具调用循环"的其实没几个：cluster 判断、digest 归纳、originalizer 的翻译/摘要，本质都是"一次 prompt 进、一个结构化 JSON 出"，不需要 agent loop，需要的是一个**模型无关的结构化调用层**。

大多数"类 LangChain"框架强在第 2 层、弱在第 1 层；而这个项目的痛点全部集中在第 1 层。这个拆分决定了最终的推荐方向——且第 2 层不应该绑定任何单一模型厂商的 Agent SDK。

## 二、逐方案评估（持久化编排层）

| 方案 | 动态 fan-out / fan-in | 重试 / 部分失败 / 可观测性 | 自托管运维 | 模型/厂商锁定 |
|---|---|---|---|---|
| **LangGraph** | 强。`Send` API 天生支持"运行时决定需要 42 个 worker 就 spawn 42 个"这种 map-reduce，表达最优雅 | 中。有 checkpointer，但崩溃恢复/每子任务自动重试要在应用层自己拼装，达不到运维级 | 低（纯库） | 无强锁定，但消息/工具调用抽象带 LangChain 生态的"味道" |
| **CrewAI** | 弱。role-based crew 偏顺序/层级结构，非任意图，动态 N 表达不自然 | 弱 | 低 | 无 |
| **AutoGen / AG2** | 中。会话式 GroupChat 灵活但非结构化 fan-out，难以保证"恰好汇总 N 条结果" | 弱 | 低 | 无 |
| **Temporal** | 强。workflow 内循环起 N 个 activity / child workflow 再 gather，N 是运行时值；fan-in 就是 await 全部结果 | **强，且是运维级能力**。每个 activity 有独立重试策略、进程崩溃后从上次完成的步骤重放、Web UI 逐子任务展示哪些失败 | 中（需要 Temporal Server + Postgres/MySQL + worker，或用 Temporal Cloud） | **无**——activity 就是普通函数，调哪个模型/哪个 API 完全不关 Temporal 的事 |
| **纯 Celery canvas** | 中。`chord(group(...), callback)` 能表达，动态 N 可行 | **弱且有已知坑**。chord 的部分失败语义反直觉（组内某个失败、其余仍会跑、但 callback 会被跳过），`chord_unlock` 历史上出现过无限重试 bug，补偿逻辑要完全自己写 | 已经决定要用 | 无 |

结论不变：**Temporal 胜出**，因为痛点是"无人值守时静默失败没人兜底"，不是"表达 agent 协作的语法不够优雅"。下面单独把 Temporal 和最初设想的"类 LangChain"路线（LangChain / LangGraph）做一次更细的对比，说明两者到底是不是竞品。

## 三、选型矩阵：Temporal vs LangChain vs LangGraph

先澄清一个常被混用的概念：**LangChain（经典 chains/LCEL）和 LangGraph 不是同一层**。LangChain 是 prompt/组件组合库，本身不是编排引擎；LangGraph 是在其上构建的**有状态图编排层**，用来表达"agent 自主决策走哪条边、调哪个工具"。日常语境里说"用 LangChain 编排"，现在通常指的是 LangGraph。

| 维度 | Temporal | LangGraph | LangChain（经典 chains/LCEL） |
|---|---|---|---|
| 定位 | 通用持久化工作流引擎，控制流由**代码/config 确定** | LLM 应用的有状态图编排层，节点可以是 LLM 决策也可以是普通函数 | LLM 应用组件库（prompt 模板、chain 组合），**不是编排引擎** |
| 控制流由谁决定 | 你写的代码决定拓扑（确定性），但 fan-out **宽度**可运行时变化 | 可以让模型在运行时决定走哪条边/调哪个工具（agentic routing） | 静态 chain，几乎不支持运行时分支 |
| 动态 fan-out N 个并行任务 | 原生一等公民（activity/child workflow 循环），运维级能力 | 支持（`Send` API），但要应用层自己管理 checkpointer 持久化 | 弱/无，需要手工拼装 |
| 崩溃恢复 / 断点续传 | **核心卖点**：进程崩了，从上次完成的步骤自动重放，无需额外代码 | 需要显式配置 checkpointer（如 Postgres backend），覆盖度需要自己验证 | 无 |
| 重试策略粒度 | 每个 activity 可独立配置重试次数/退避/超时 | 需要应用层实现 | 无 |
| 可观测性 | Web UI 原生展示每个 workflow/activity 的完整执行历史、失败详情、可重放 | 需要额外接 LangSmith 才有类似能力 | 无编排级可观测性 |
| 语言支持 | Go/Java/Python/TS/.NET/PHP，多语言 worker 可混跑 | Python/JS | Python/JS |
| 最适合的场景 | **拓扑固定、只有 fan-out 宽度是运行时动态的**批处理流水线，必须无人值守可靠运行 | **拓扑本身需要模型运行时决策**（agent 自主选工具/选分支）的场景 | 单次/简单 prompt 组合，不需要编排可靠性 |
| 对 AInews 的适配 | **强**——8 个 Phase 拓扑是固定的，只有 fetch（N=活跃源数）和 originalize（M=聚类条目数）的并发宽度是运行时值，这正是 Temporal 的核心场景，不是 LangGraph 的核心场景 | 中——如果未来某个环节确实需要"模型自主决定要不要多抓一轮/换个解析策略"，可以把该环节实现成一个嵌在**某个 Temporal activity 内部**的小型 LangGraph 子图，而不是让 LangGraph 取代 Temporal 做整体编排 | 弱——AInews 不需要"组合多个 prompt 模板"这类基础能力，直接调 LiteLLM + Instructor 更直接，没有必要为此引入 LangChain 的抽象 |

**关键判断**：AInews 8 个 Phase 的拓扑（fetch→filter→cluster→originalize→write→digest→git sync）是**代码决定的确定性顺序**，唯一的"动态"体现在 fan-out 的宽度（N 个源、M 条 entry），而不是"下一步该走哪条路要不要问模型"。这正是 Temporal 的设计中心，LangGraph 解决的是另一个问题（拓扑本身由模型决策）。两者不是互斥选项——如果未来某个具体环节（比如 originalizer 要不要根据抓取结果自主换策略）确实需要模型自主路由，可以把 LangGraph 作为该 activity 内部的局部工具嵌入，而不是用它替换 Temporal 整体。

## 四、推荐：Temporal 作编排骨架 + 直连 LiteLLM（可选 Instructor）作执行体

**不用任何"Agent SDK"（无论 Claude Agent SDK 还是 LangGraph 的 agent 抽象）作为默认执行层。** 理由：现有 8 个 subagent 里，真正需要多轮自主工具调用循环的场景基本不存在——cluster 判断、digest 归纳、originalizer 的翻译/摘要，都是"一次结构化调用"模式，用 agent 框架是过度设计，还会带来不必要的厂商/框架锁定。

具体执行体设计：

```
Temporal（可靠性/编排层，完全不关心调的是哪个模型）
  └─ activity 内部：
       ├─ 纯 Python，零 LLM（fetch rss/api、filter、git sync、reindex）
       └─ 直接 HTTP 调用自建 LiteLLM 端点（OpenAI 兼容协议）
            └─ 可选套一层 Instructor（LiteLLM 官方有适配教程），
               保证拿到校验过的结构化 JSON，而不是裸文本解析
```

映射到现有流程：

- **Phase 1 Fetch**：workflow 读活跃源列表 → 循环起 N 个 `fetch` activity（N = 当前源数）→ 每个 activity 带独立重试策略；新增源/拉黑源只是改输入数据，不改 workflow 代码。RSS/API/script 三类本质是确定性解析，**activity 内部走纯 Python，完全不调模型**；只有 webfetch 类（无结构化 API 的 HTML 列表页）需要调 LiteLLM 做抽取。
- **Phase 3.5 Originalize**：聚类结果 M 条 → spawn M 个 `originalize` child workflow → 某条抓原文超时，Temporal 自动按策略重试；彻底失败也会在 Web UI 里标红，**不再需要人工"补跑覆盖率缺口"这个动作**——这直接对应 2026-07-04 那次 56 条 entry 只完成 16 条的真实故障。
- **Phase 7-8 Git/Publish**：作为顺序步骤，天然支持幂等重试；"补记重新发布"这类事故理论上会消失。

**每个 activity 用哪个模型，就是 activity 代码/配置里的一个 model 字符串**（通过 LiteLLM 转发到 OpenAI/Anthropic/其他端点），这完全是流程编排层面的事，不需要额外的"模型路由"抽象——LiteLLM 已经把这层解决了。**关键约束**：activity 要设计成幂等（比如落盘前检查目标文件是否已存在/内容哈希是否一致），否则 Temporal 的自动重试会导致重复写入或重复计费。

**观测性附带收益**：LiteLLM 原生支持把每次调用的 prompt/response/token/耗时通过 callback 上报 Langfuse 之类的 LLM 可观测性工具（`litellm.success_callback = ["langfuse"]`，零代码改动），这一点在 [03-architecture-proposal.md](./03-architecture-proposal.md) 里作为独立组件详细展开——它补的是 Temporal Web UI 覆盖不到的"这次到底跟模型说了什么、模型答得好不好、花了多少钱"这一层。

## 四.1 结构化输出：把每个固定 LLM 场景设计成 tool，而不是解析自由文本

现有 8 个 subagent 里的固定 LLM 场景——cluster 判断、digest 归纳、filter 的 LLM fallback、originalizer 的翻译/摘要——都应该显式建模成一个 **tool（function calling schema）**，而不是让模型自由生成一段文本/JSON 再靠 Python 事后解析。做法：每个场景定义一个 Pydantic 模型（如 `ClusterJudgment`、`DigestSummary`、`OriginalizeResult`），转成 tool 的 JSON schema，调用时把 `tool_choice` **强制指向该 tool**（不用 `auto`），模型的返回就是符合 schema 的结构化参数，不存在"模型有时候会在 JSON 前面加一句寒暄"这类需要防御性解析的情况。

**这直接命中现有系统的历史病根**：SKILL.md 自己记录的 v2.1→v2.3/v2.4 重构理由，本质都是"让模型自由生成一整块大 JSON，规模一大就在 32k 输出上限截断，真实数据丢失"（cluster agent 要在脑子里拼 23k 字符的完整 cluster.json；filter agent 因构造 89 条 entries 的大 JSON 超时截断）。用 tool schema 强制**单条**结构化输出，从源头上避免"自由文本生成大块 JSON"这个故障模式，也让 `cluster-merge.py` 里那些"校验 agent 自造 url / 漏映射 / is_new 错判"的事后防御代码大部分可以直接去掉——schema 本身就保证了字段合法性。

**LiteLLM 的 `tools=`/`tool_choice=` 参数是跨 provider 统一透传的**（OpenAI 格式定义，自动翻译成 Anthropic 等其他 provider 各自的原生格式），所以这套模式和"模型选哪个只是配置里一个字符串"的原则完全兼容，不会因为换供应商而要重写 schema。

**Instructor 不是这个方案的替代品，而是这个方案的薄封装**：它做的就是"从 Pydantic 模型自动生成 tool schema + 自动把返回的 tool-call 参数解析校验回该模型"，免去手写 JSON schema 的样板代码。是否引入 Instructor 只是"自己写这层胶水代码"还是"用现成库"的偏好选择，不影响整体架构。

**关键约束——不要让两层重试叠加**：Temporal 已经是这套架构里"失败重试"的唯一权威层（每个 activity 有自己的重试策略）。如果 Instructor 自己又内置"schema 校验失败自动重试"，会出现嵌套重试（Temporal 重试 N 次 × Instructor 内部重试 M 次 = 最多 N×M 次实际模型调用），还会让 Temporal Web UI 里"这个 activity 失败了几次"和真实发生的调用次数对不上。建议把 Instructor 的内部重试设到最低（甚至 0），校验失败就直接抛异常让 activity 失败，交给 Temporal 的重试策略统一处理。

**一个真实的粒度取舍（未替用户决定，见 [03-architecture-proposal.md §7](./03-architecture-proposal.md) 待决问题）**：现有 cluster 是"一次 agent 调用判断全部 filtered entries，返回一个 mappings 数组"。如果照搬成"一个 Temporal activity + 一次 tool call 判断全部条目"，任何单条判断失败（或数组里一条 schema 校验不过）都会导致**整批重新判断**——这和 fetch/originalize 特意按条 fan-out 换取"单条失败只重试单条"的设计原则不一致。是保持现状接受粗粒度重试（调用少、成本低），还是把 cluster 也按条 fan-out 成 N 个独立 tool call（细粒度重试，但调用数从 1 次变成 N 次），是一个需要跟成本/延迟一起权衡的决定，不是纯技术判断。

## 五、与 Celery 的定位关系

**Temporal 基本接管 Celery 现在扮演的"流程编排"角色，Celery 降级为纯 cron 触发器。**

- Celery Beat 定点触发 → 一个极薄的 task，只做一件事：`client.start_workflow(AInewsPipelineWorkflow, date)`，立刻返回。
- 之后所有 fan-out、重试、补偿、状态追踪，全部在 Temporal workflow 内部完成，不在 Celery 里。
- Celery 不再持有任何业务编排逻辑，也就不会再碰到 chord 部分失败语义这些坑。

（备选方案：Temporal 自带 Schedules 功能，理论上可以完全取代 Celery 做定时触发；但既然定时框架已经确定用 Celery，让 Celery 只做"敲一下门"这一件薄事，是改动最小、职责最清晰的组合方式，不建议为了省一个组件而推翻已有决定。）

**一句话：Celery 负责触发，Temporal 负责编排与兜底，LiteLLM（自建）负责统一转发模型调用并以 tool schema 强制结构化输出，Instructor 按需负责生成 schema 与解析校验。**

## Sources

- [LangGraph Send API（map-reduce how-to）](https://langchain-ai.github.io/langgraph/how-tos/map-reduce/)
- [LiteLLM 统一 tools/tool_choice 跨 provider 文档](https://docs.litellm.ai/docs/completion/input)
- [Temporal for AI —— 持久化 agentic workflows](https://temporal.io/solutions/ai)
- [Temporal Persistence 官方文档（生产环境后端要求）](https://docs.temporal.io/temporal-service/persistence)
- [Celery chord 部分失败陷阱（GitHub Issue #1881）](https://github.com/celery/celery/issues/1881)
- [Celery chord_unlock 重试问题（GitHub Issue #9674）](https://github.com/celery/celery/issues/9674)
- [多智能体框架对比 2026（CrewAI / AutoGen / AG2 动态拓扑）](https://gurusup.com/blog/best-multi-agent-frameworks-2026)
- [LiteLLM × Langfuse 集成文档](https://docs.litellm.ai/docs/observability/langfuse_integration)
- [Instructor × LiteLLM 教程](https://docs.litellm.ai/docs/tutorials/instructor)
