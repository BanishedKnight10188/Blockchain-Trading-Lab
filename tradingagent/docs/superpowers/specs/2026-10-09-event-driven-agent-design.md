# 事件驱动 Trading Agent：架构分析与 MVP 规格

日期：2026-10-09（Asia/Shanghai）。状态：评审草案，本轮交付设计与计划。

## 1. 已确认目标

- 主项目为 Blockchain-Trading-Lab/tradingagent。
- 产品分为事件驱动 Trading Agent 与 JEV 短线自动操盘两个独立板块。
- 第一阶段：研究 → 结构化观察 → 持续监控 → 事件唤醒 → 二次决策 → Paper 交易 → 记录结果。
- 新 Agent 不成为 JEV 每轮预测的前置步骤。
- 本轮仅分析并制定计划，不启动模型、操盘或变更既有资金权限。

设计默认：单用户、每个 Agent lane 绑定一个 USDT 永续合约，首批支持 1m/5m 已收盘 K 线观察。合约由用户选择，BTCUSDT 仅作测试样例。允许多个 lane，但 MVP 各自使用独立 Paper 钱包；共享真实账户的策略协调后续单独设计。

## 2. 当前项目事实

以下来自静态代码与最新开发记录，不是本轮重新执行的运行验收。

| 能力 | 代码证据 | 本次结论 |
| --- | --- | --- |
| 分层内核 | agent_platform 的 Domain/Application/Ports/Adapters | 延续现有结构，不建立另一套平行工程 |
| 合约报价、历史、多尺度数据 | adapters/binance_direct/futures_*、multiscale.py、application/background.py | 复用客户端、标准化、连续性检查；补 Watch 数据接口 |
| 确定性指标 | application/features.py | 有 EMA/ATR 等计算；当前 FeatureService 限定完整 1m Candle，不能直接声称支持任意周期 |
| 首次分析 | application/session_analysis.py、domain/session_analysis.py | watch_conditions 目前是字符串，不是可执行 Watch |
| 触发与调度 | application/triggers.py、runtime/scheduler.py | 有阈值告警、去重、冷却；pending/seen 位于内存，不是持久消费队列 |
| 模型接口 | ports/model.py、adapters/openrouter/chat.py | 有有限结构化建议；chat.py 明确拒绝 tool_calls，尚不是 Agent 工具循环 |
| 费用账本 | adapters/sqlite/budgets.py、模型执行器 | 复用预留、结算与未知费用语义，不重新实现或清零 |
| 通用执行 | application/trading_execution.py、ports/trading_execution.py | 有持久命令、幂等、状态、查询恢复，适合复用 |
| 决策证据 | domain/trade_evidence.py、domain/trading_execution.py | 绑定 JEV 请求和 jev: 命令身份，需要兼容扩展 |
| 合约风险 | application/futures_trading.py 的 _risk | 关键检查仍在 JEV 应用服务，需要成为公共执行授权 |
| 持仓维护 | application/trading_maintenance.py、runtime/futures_trading.py | 资金费等维护独立于模型，不等于完整止损 Guardian |
| 当前分析角色 | docs/DEVELOPMENT_STATUS.md 最新 2026-10-09 记录 | 背景已改为 Haiku，JEV 独立；旧 Flash 表述属于历史 |

最新记录证明过一次正常 Haiku→JEV 输入/费用联调；没有证明事件驱动 Agent、持续自主开平仓或长稳。本次不重做 JEV 已验收范围，不把文档中的历史测试计数当成本轮结果。

## 3. 原框架评价与改进

保留：LLM/Tools/Runtime/Risk 分工、结构化 Watch、执行前重新取事实、共享规则回放，以及 Coding 的隔离边界。

改进：

1. 从项目增量出发。已有行情、账户、执行和账本，主要新增 Watch、可靠事件消费、Agent 工具循环及证据类型。
2. 提前交易与风控闭环。原 V1 写了模拟交易，V3 才建立 Paper/Risk，阶段边界不一致。MVP 即接现有 Paper 与公共授权；Coding 放在闭环之后。
3. Journal 不等于消费队列。可靠消费还需要领取、租约、重试、完成标记及恢复。
4. 拆开 Watch、EventDelivery、AgentRun 生命周期。Watch 已触发时，模型或订单可能仍在进行。
5. 明确时间语义。同根 K 线低点进入区域并收回，不等于先回踩、若干根之后反弹。V1 仅支持同根规则，跨根序列后续版本化增加。
6. 明确策略归属。两个决策源共享基础设施，不自动共享资金或仓位控制权。
7. 有界 Agent。工具、轮数、费用、超时、上下文大小和权限均有上限；Reflection 只提出研究候选，不自动放宽风控。

| 实现方式 | 优点 | 代价 | 判断 |
| --- | --- | --- | --- |
| 在 FuturesTradingService 继续加分支 | 初始改动少 | JEV 调度、Watch、工具循环耦合进大服务 | 不采用 |
| 在现有平台增加独立 event_agent 用例/worker | 复用基础设施，生命周期和账户清晰 | 要扩展证据与授权接口 | 推荐 |
| 立即拆多服务与外部消息系统 | 进程隔离强 | 部署、事务、对账复杂度增加 | 暂缓 |

## 4. 目标架构

~~~mermaid
flowchart TD
    Feed["共用行情与历史接口"] --> Features["确定性特征与数据质量"]
    Features --> Watches["Watch Engine"]
    Watches --> Queue["Watch 状态 + 事件 Outbox"]
    Queue --> Agent["事件 Agent / Context / Tool Registry"]
    Agent --> Watches
    Agent --> Intent["Trade Intent"]
    Jev["独立 JEV 短线模块"] --> Intent
    Intent --> Gate["公共授权 / 风险 / 账户归属"]
    Gate --> Execution["现有持久执行通道"]
    Execution --> Paper["Paper Backend"]
    Guardian["独立 Position Guardian"] --> Gate
    Feed --> Guardian
    Paper --> Guardian
    Agent --> Trace["决策、工具与费用档案"]
    Execution --> Trace
~~~

这是目标接入关系；JEV 接入公共授权的部分也属于计划，不宣称已完成。

技术延续 Python 3.12、asyncio、Pydantic 2、SQLite、FastAPI/Jinja2。首版自定义有界工具循环，内置函数注册即可，不要求先引入 LangGraph/MCP Server/Redis/PostgreSQL。以后通过 Adapter 暴露 MCP。大量计算和研究代码进入独立进程，不能阻塞共享事件循环中的 JEV 或 Guardian。

## 5. 运行隔离

EventAgentLane 绑定 lane_id、独立 session_id、ExecutionScope、symbol、style_revision、agent_revision、用户确认的风险配置和费用子限额。scope 由运行配置注入，模型工具参数不允许指定其他账户或 environment。

- 两个板块分别启停，使用独立调度槽与 HTTP 并发限额。
- MVP 使用不同 session_id，延续 paper:futures:<session_id>，不修改旧钱包身份。
- 可以共用模型费用总账本，但新 Agent 有独立子限额，总限额仍累计约束所有使用方。既有 JEV 费用授权不自动授权新 Agent。
- 用户手动操作保留作者及审计，不改写为 Agent 决策。
- 暂停 Agent 停止新分析/新开仓，丢弃在途风险增加结果；Guardian、资金费维护和订单恢复继续。
- 重启先恢复 Watch/事件/保护及未完成订单；默认暂停新增交易，显式恢复后继续。

共享真实账户后续需要账户级额度预留、symbol/position owner、手动交易协调和冲突策略。只加 strategy_id 无法解决交易所净持仓被两个策略同时操作的问题。

## 6. Watch 契约与时间语义

WatchDefinition 不可变、版本化，包含 schema_version=watch-v1、watch_id、definition_revision、lane/session、symbol、market=usdt_perpetual、timeframe、price_kind、hypothesis、evidence_ids、trigger、invalidation、created_at、expires_at、parent_watch_id。

V1 限制：

- timeframe 仅 1m/5m，price_kind=last_trade，evaluate_on=candle_closed。
- trigger/invalidation 分别为平面 ALL/ANY，最多 8 个叶子条件，总数最多 16。
- metric 仅 candle.open/high/low/close、volume_ratio_20、ema_12、ema_26、atr_14。
- op 仅 GT/GTE/LT/LTE/BETWEEN；Decimal 用十进制字符串，拒绝 NaN/Infinity/未知字段/非法区间。
- 期限大于 0 且最多 24h；max_triggers=1，on_trigger=wake_agent；每 lane 最多 20 个有效 Watch。
- 更新采用 expected_revision，并形成新 definition_revision。TRIGGERED Watch 不重开；继续观察创建有 parent_watch_id 的后继 Watch。尚在复核的 TRIGGERED 记录允许用户取消，立即退休其交易权限。

WatchRecord 保存 revision、状态、last_candle_key、输入哈希、最后原因和触发事件 ID。状态 ARMED/TRIGGERED/EXPIRED/INVALIDATED/CANCELLED；warming/data_gap 是健康原因，不直接代表假设失效。

优先级：取消/退休 → 到期 → 数据不完整（不触发、不判失效）→ invalidation → trigger。有效区间 created_at <= now < expires_at。

WatchFrame 包含 symbol/market/interval/price_kind、data_version、closed_candle_key、occurred_at/received_at、连续已收盘 candles、特征/质量/content_hash。

- last_trade K 线用于规则；mark 用于权益和 Guardian；bid/ask 用于执行价格。
- 同根 ALL 就是同一根已收盘 bar 满足全部条件。V1 拒绝跨根“先 A 后 B”，不静默改成同根。
- volume_ratio_20 = 当前完整 bar 的 volume / 前 20 根同周期完整 bar 的均量，基准排除当前 bar；分母零为 unavailable。
- EMA/ATR 复用现有确定性算法，适配同周期输入；不将仅支持 1m 的 FeatureService 直接套给 5m。版本与暖机要求写入证据。
- 相同 candle_key/内容去重；同 key 不同内容进入 data_conflict 并阻断该分区新信号。缺口修复后恢复连续窗口。
- created_at 之前的历史只暖机。断线回补恢复状态；超过事件 TTL 的历史触发只记 suppressed，不唤醒交易。

## 7. 持久事件与 AgentRun

WatchEvent 包含 event_id、watch/version、lane/session/symbol/market、数据/指标版本、发生/接收时间、expires_at、假设、条件实际值、质量、snapshot_hash。

事件身份由 watch_id、definition_revision、closed_candle_key 派生。Watch 状态、处理游标、事件事实和 outbox 在同一 SQLite 事务提交。

EventDelivery 独立保存 PENDING/LEASED/DONE/EXPIRED/DEAD_LETTER、lease_token、lease_until、attempts、run_id。领取租约 120s，每 30s 续租；只能当前 token 续租/完成。发送模型之前的暂时性领取失败最多重试 3 次，之后 DEAD_LETTER；已发送模型或已有副作用时进入恢复，不重放整轮。至少一次投递，副作用幂等去重，不承诺外部请求“恰好一次”。

AgentRun 保存事件、模式、所有版本、上下文哈希、每轮请求/工具/费用、意图/命令 ID；状态 QUEUED/RUNNING/WAIT/COMPLETED/FAILED/EXPIRED/INTERRUPTED/RECONCILING。

请求发送前持久化 request_id/发送状态；结果或收费不确定进入 RECONCILING，保留预算预留，事件重投不再次收费。模型响应在执行工具前落库；有副作用工具以 run_id+tool_call_id 派生幂等键。

## 8. 有界 Agent 与工具

新增 AgentModelPort，不放宽旧 ModelPort/ChatModel 的建议契约。工具能力在 Adapter 检查，选定模型需实际协议验收，不能从摘要联调推定。

| 模式 | 首版工具 |
| --- | --- |
| ANALYSIS | get_market_snapshot、get_indicator_catalog、create_watch、update_watch、cancel_watch、list_watches |
| REVIEW | get_market_snapshot、get_account_state、get_watch_event、preview_trade、submit_trade_intent、create_watch |
| RESEARCH | 首版不启用；后续单独增加 Sandbox/Backtest/Registry |

Tool Registry 校验 schema、模式、lane/账户归属、次数、结果大小；MCP 仅是可选传输。

工程初值：每 lane 1 个在途 AgentRun、每轮最多 4 次模型请求/8 次工具调用、单请求最多 2048 输出 token、上下文最多 32 KiB、整轮最多 90s。Watch 事件 TTL=120s，单请求 deadline=min(now+30s, run_deadline)。这是新 Agent 上限，不修改 JEV 秒级 TTL。

启动和用户请求可触发 ANALYSIS，之后主要由 Watch 唤醒 REVIEW。没有有效 Watch 时最多每 15 分钟一次有界研究；已有有效 Watch 不定时反复调用 LLM。均受暂停、费用和轮次上限约束。

## 9. 意图、证据与公共授权

TradeIntent 包含 intent/run/event ID、scope、action=open_long/open_short/reduce、quantity、原始报价、expires_at、账户/风格/Agent/Watch/policy revision 和保护配置。WAIT 是 run 结果，不生成命令。V1 杠杆使用 lane 用户确认值，模型调整杠杆后续增加。

EventAgentTradeEvidence 包含 kind=event_agent、run_id、触发证据、上下文/输出/工具结果哈希、意图、风险版本；GuardianTradeEvidence 包含 kind=guardian、保护配置和当前风险事实。

旧 TradeDecisionEvidence 不改写；TradeCommand 兼容新证据并分别验证身份，不能伪造 JEV 请求套接口。

新增 TradeAuthorizationPort：每个新 Agent 命令需匹配证据，在实际后端 submit 前必经授权。将 JEV _risk 现有纪律抽到公共校验器，保持计算和拒绝语义。风险输入采用新 FuturesRiskSettings，仅含 TradingLimits、TradingPolicy 和交易数量/金额规则，不借用含 real_jev 来源标签的 TradingRun 充当新 Agent 身份。原可选 preflight 只作补充。

检查 scope/owner、权限、事件/假设有效性、版本、新鲜度、价格漂移、数量/金额/规则、保证金、持仓/杠杆/损失限额、未决订单、保护配置。复核/执行前重新评估原 invalidation，数据不够则阻断开仓；Watch 单次触发结束不意味着假设永远有效。新 Agent 的 agent_revision 映射到命令的 trader_revision，但证据按 event_agent 类型独立校验。执行前重取事实，沿用 TradeCommand 最多 30s 生命周期。

## 10. 独立持仓保护

新 Agent 每个开仓意图必须有 protective_stop_mark，多仓低于当前 mark、空仓高于当前 mark，并满足该 lane 显式风险限制。资金、最大损失、手续费/滑点和保护参数由用户配置，本设计不代填交易参数。

保护配置在订单发送前持久化，关联 command_id。Guardian 根据回执和实际持仓激活；部分成交也保护，崩溃后从持久命令/回执重建。

Guardian 独立任务跟踪 mark/持仓/保护/run loss，生成限定所属账户的 reduce 命令，经同一授权和执行通道。可信 Guardian 可在 Agent 暂停时减仓，不能开仓/反向；普通暂停与保护减仓的权限需分开。

行情不可用时标记 protection_degraded、阻断加风险并告警；没有可用执行报价不假装平仓。Paper 本地保护依赖本机存活，不等于交易所保护单。Testnet 阶段补交易所保护单及监督恢复。

该要求只适用于新 Agent lane，不擅自改变既有 JEV 自主退出策略或旧钱包。

## 11. 复盘与回放

实时/回放共享纯 Watch evaluator、指标版本、数据哈希与事件时钟。不能用今日 LLM 回答声称重现历史当时决策；Agent 回放使用存档响应或标明的离线 Fake。

MVP 记录假设、触发、二次判断、风险拒绝、成交/费用/资金费和未交易原因。Reflection 保留原事实，生成候选结论，不自动修改在线指标或限额。

后续实验对比规则基准、Agent 二次判断、Agent 研究方案，计入手续费、资金费、滑点、延迟与模型成本。成交采用决策完成后可获得的报价，不能按触发 K 线收盘价零延迟成交。样本外/滚动验证先于晋升；胜率或模型 confidence 不单独证明盈利。

## 12. 分阶段路线与验收

| 阶段 | 内容 | 完成标准 |
| --- | --- | --- |
| M1 可靠观察 | DSL、指标、质量、CRUD、原子触发/outbox | 同输入实时/回放一致；重复/崩溃不丢事件 |
| M2 有界 Agent | 工具、上下文、权限、费用、消费 | 自主创建 Watch，被唤醒二次判断；未知收费不重复调用 |
| M3 Paper 闭环 | 证据、公共授权、独立钱包、Guardian、页面 | 开仓→保护/减仓→复盘可追踪；暂停/重启不重复资金动作 |
| M4 运行加固 | 断线、积压、锁争用、度量、有界真实联调 | 分阶段延迟/失败率/成本及约定观察窗口、故障演练 |
| M5 自主研究 | Coding Sandbox、回放/实验、指标注册 | 生成代码隔离运行，验证/晋升独立，在线版本固定 |
| M6 Testnet | 账户/执行 Adapter、保护单、对账 | 实际测试网订单/部分成交/未知状态和恢复 |

M1–M3 合计是已确认第一阶段 MVP。M4 先于扩大自动运行；M5/M6 可按需要调整顺序。真实资金上线不属本计划。LightGBM、多 Agent、复杂盘口因子及自动晋升暂缓。

单人粗估 M1–M3 为 14–22 工作日，属于规划估计；主要不确定性是工具协议、授权兼容、故障恢复，M4 运行观察另计。

验收分层：

1. 离线手算 fixtures 证明 Watch→Agent→风险→Paper 开平仓及 provenance。
2. 崩溃/重复/旧租约/model unknown/保护降级/order unknown 的恢复。
3. 新 Agent 的开关、阻塞和资金不影响 JEV，旧档案可读。
4. 有界真实模型确实调用工具且费用落账，允许 WAIT，不强迫成交。
5. 后续单独约定持续观察时间和费用；一次成功不是长稳。

## 13. 官方契约核对

2026-10-09 已核对：

- [OpenRouter Tool Calling](https://openrouter.ai/docs/guides/features/tool-calling)：应用执行用户定义工具并回传 tool_call_id，需要完整循环。
- [Binance USDⓈ-M General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)：部分 503 代表执行未知，应先查询核对，不能统一判为未下单。
- [Binance USDⓈ-M Market Streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)：按当前协议核对合约类型/收盘字段，不沿用旧文档地址假定能力。
- [SQLite WAL](https://sqlite.org/wal.html)：同机规模适用，但同时只有一个 writer；事务需短，不将模型/网络调用放入写事务。

实现期再次核对模型工具能力、流协议和 Testnet 能力，本轮无需收费请求。
