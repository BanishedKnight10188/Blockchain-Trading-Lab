# JEV 实际输入、连接暂停与耗时诊断

2026-10-09新六层真实样本：JEV实际输入14976token/输出310，23815字节，端点759ms（transport750）、预算/校验/结算在内模型890.576ms，整轮1179ms；WAIT0.93。本地reserve40.643ms/settle82.203ms/validation1.992ms/provider764.026ms/fee_validation0.209ms/binding1.244ms。字段为嵌套计时，不能重复相加；一个样本不能当P50/P95，尚未达到<500ms。背景Flash32431/721token、6243ms仅发生初始化/低频刷新，JEV每轮读缓存；reasoning关闭后0思考token。新输入并未证明比旧输入token更少。证据multiscale-workflow-1usd-20261009-c.json；下面的失败与旧样本保留为历史。

2026-10-09真实六层接续：一次Flash背景请求75802字节在旧15秒响应体读取时超时，wait7969ms/body6937ms/total14922ms；JEV零调用，不能用这个数字表示JEV延迟。后台已独立60秒等待，JEV3秒TTL不变，47关键离线检查通过；尚未真实成功，未知费用保留。详见[JEV_MULTI_SCALE_LIVE_TRIAL.md](JEV_MULTI_SCALE_LIVE_TRIAL.md)。

2026-10-09接续：新建会话可选六层输入，其请求改为四层背景摘要＋20根3m＋60根1s，取代本页后文的小时历史＋30报价；旧会话仍沿用本页记录的旧输入。代码/真实公共行情/离线成交档案已验证，新模式尚无收费推理延迟或token实测。不能将旧844ms或10364token归于新输入。详见[JEV_MULTI_SCALE_RUNBOOK.md](JEV_MULTI_SCALE_RUNBOOK.md)。

记录时间：2026-10-09 00:20（上海）。会话 `ogn测试1` 实际选中 **ONGUSDT**，风格95/v1，不能按名称误认为OGNUSDT。以下输入取自已保存的真实请求，没有新增收费推理。

## 1. 连接中断为什么暂停

原运行记录为 `provider_transport_error`，不是20秒到期，也不是历史行情校时错误。首批111条记录里有3次传输失败；原诊断只有transport阶段，未保存底层异常类型，因此不能逐笔断言历史失败都是同一原因。

免费复测10次新连接，其中4次 `ConnectError`，全部发生在本机HTTP代理CONNECT之后的 `proxy.start_tls`；尚未发送API请求。失败阶段已与本机到代理的TCP连接区分。其余6次返回401，因为探测故意不携带API Key，401在此表示连通，不是正式模型鉴权失败。

已修复立即暂停的其中一个触发路径：**只有Trace证明TCP/TLS建连已开始、模型POST尚未开始发送，而且异常为ConnectError/ConnectTimeout，才允许一次重连。** 两次连接共用原请求、原费用预留、原3秒有效期。POST头开始发送后，读写异常、超时、HTTP拒绝和未知阶段全部不重试；仍按既有规则暂停并保留未知费用，不重放订单。

这能容忍一次握手抖动，不能修复VPN节点本身。免费生产传输代码探测10/10取得401，其中4次经过安全重连；该轮没有消费响应体，复用client不代表复用了socket。随后修正免费探测工具消费小响应，再测6/6连通：复用池的首个建连2453ms，后三次无TLS耗时，总耗时62/78/63ms。此事实不能替代真实JEV持续运行验收。

证据：

- `output/verification/openrouter-free-cold-probe-20261009.json`：10次原连接、失败Trace、当时会话状态。
- `output/verification/openrouter-free-reconnect-20261008T161326794501.json`：生产安全重连路径，10/10连通。
- `output/verification/openrouter-free-reconnect-20261008T161447433938.json`：消费响应后确认连接复用，6/6连通。
- `tools/check-openrouter-connection.py`：不读取密钥，底层固定改为未认证GET /api/v1/key，移除所有请求头和请求体。没有模型调用。

## 2. 500–1000ms包含什么

页面的 `model_latency_ms` 是围绕整个模型调用通道的本机计时，包含预算预留、HTTP、解析校验和费用结算。它不是纯模型计算耗时。历史读取和请求准备在这个计时之前；完整决策端到端时间还包括它们与执行前校验。

保存的首批105个真实响应（111条记录中的102 WAIT和3个已被较新结果取代的响应）：

| 指标 | 耗时 |
| --- | ---: |
| 最小 | 719ms |
| 中位 | 844ms |
| P90 | 1343ms |
| P95 | 1796ms |
| 最大 | 2438ms |

两个历史generation的免费GET统计：

| 本机完整调用 | OpenRouter返回的latency字段 | generation_time字段 |
| ---: | ---: | ---: |
| 828ms | 238ms | 0 |
| 1250ms | 236ms | 0 |

不同观测点不能严格相减为纯网络或纯推理耗时；差额还含网关、其他网络路径与本地费用处理。两条统计也不能当全部请求的服务端分布。它们说明不应把828/1250ms全部归因于JEV推理。

另一个离线100次测试：实际请求模型校验＋wire JSON构造/序列化，中位0.424ms、最大0.821ms；不包含SQLite、响应处理和网络。免费网络测试的新TLS握手出现1422/1438/2453ms，热连接62–78ms。**目前已经确认代理TLS不稳定；本机JSON构造不是主要耗时。** 上游路由、排队与推理各占多少，不能用旧粗粒度记录还原。

新请求现在记录 `transport_evidence`：尝试次数、发送前重连次数、connect/proxy_connect/tls/send/response_wait/body/total耗时，失败阶段、固定异常类型、是否开始发送。response_wait包含网络与远端等待，不能再命名为纯推理耗时。并发请求各自保存证据；旧记录不补造字段，旧档案哈希保持。

每秒派发不等于一秒内完成；目前最多3个并发、3秒有效期，无积压补发。较新有效结果完成后旧结果不得执行。下一步性能优化优先稳定代理链路、按新的分阶段记录判断慢点，再压缩重复输入；没有宣称已经降低真实JEV P95。

证据：`calibrated-clock-free-provider-check-20261008.json`、`jev-free-generation-metrics-20261009.json`、`jev-local-payload-timing-20261009.json`，均在 `output/verification/`。

## 3. 当前实际Prompt

完整实际wire请求：[jev-current-payload-20261008.json](../output/verification/jev-current-payload-20261008.json)。模型版本 `typesafe/jev-1.13`，`POST /api/alpha/decisions`，并非Chat Completions的system/user多轮消息。密钥只在授权头，本文件不含授权头。

请求顶层只有 `model`、`state`、`questions`。本轮没有Flash输出、Condor完整提示词、新闻、全深度盘口或逐笔成交数据；交易账户为本地虚拟钱包。行情来自Binance公共合约接口。JEV与Flash之间没有串行依赖。

### state：每轮决策事实

| 字段 | 实际内容 | 本样例compact JSON字节 |
| --- | --- | ---: |
| style / revisions | 风格95、style-v1、风格/操盘版本 | 约120 |
| account | 权益、保证金、可用USDT、多空/数量、费用、持仓版本 | 790 |
| position_management | 入场价、由JEV判断盈亏退出、无固定触发器、1秒节奏/3秒有效期 | 246 |
| market_snapshot | 当前bid/ask/mark、资金费、币种规则、原事件时间与校时证据 | 631 |
| recent_quote_ticks | 最近30个真实报价观察；三时间戳＋bid/ask/mark | 4171 |
| hard_limits | 资金/杠杆/最大仓位/亏损/费率等硬限制 | 164 |
| policy | 策略文字、置信度门槛、候选比例和杠杆 | 403 |
| history | 7天168根已收盘1h背景摘要；只发送最近12根完整OHLCV | 1310 |
| trade_plans | 19个完整、已计算数量的合法候选方案 | 4789 |

state共14028字节；wire请求16544字节。真实provider报告输入10265–10453 token，中位10364。字节不是token，不能据字节直接换算收费。历史按已收盘小时缓存，不是每秒重复拉168根K线。

当前策略文字原文：

> 根据当前行情和持仓选择开仓、加仓、减仓或等待，自主决定止盈止损时机；遵守资金、仓位和杠杆限额。

本钱包1000USDT；最大杠杆10、最大持仓名义额500USDT、最大运行亏损20USDT、最小confidence0.8。风格95不能绕过这些限制。止盈止损时机交给JEV，依然保留资金纪律。

### questions：一个完整交易方案选择题

`questions.plan.type = choice`。实际instructions全文：

```text
For a held position, decide the timing of take-profit and stop-loss yourself using current market evidence, entry price, unrealized PnL, funding, fees and confirmed style. No fixed profit/loss percentage or price trigger is configured. Select a reduction or full close when appropriate, including when the position is profitable; WAIT retains the current exposure. Do not wait for a fixed trigger or assume an exchange protective order exists. Hard limits and execution guards always prevail. Choose a complete trade_plans candidate for the strategy and style. Sizes and leverage are jointly fixed in each candidate; never combine candidates. Entry percentages are margin budget as a fraction of net account equity. Add/reduce percentages are fractions of current contract quantity. Changed leverage affects the whole isolated position. Hard limits always prevail. Choose WAIT when evidence is insufficient. Confidence is not a measured win rate.
```

当前空仓的19个criteria：

- WAIT。
- 账户权益5%作为开仓保证金，杠杆1/2/5/10倍，各自多/空：8项。
- 账户权益10%作为开仓保证金，杠杆1/2/5倍，各自多/空：6项。
- 账户权益20%作为开仓保证金，杠杆1/2倍，各自多/空：4项。

更大组合因500USDT名义仓位上限被本地剔除。示例 `OPEN_LONG_M5_L10` 的criteria原文：

```text
open_long; 5% of equity_margin; quantity 5917; whole-position leverage 10x; use this exact precomputed plan
```

数量是该轮ONGUSDT报价下的计算值，不能在下一轮照抄。已有持仓时，候选集合改为合法加仓/减仓/平仓方案；加减比例按现有合约数量，开仓比例按权益保证金。JEV选择的是包含仓位与杠杆的整套方案，目前不是任意连续数值输出，也没有单独的未来价格/预测期限问题。

JEV返回候选键、完整概率分布和confidence；本地负责解析、风险/费用/版本/新鲜度校验与执行。confidence不等于策略胜率。

### 可优化输入，尚未改动

优先消除trade_plans与criteria的重复描述（当前两处共约7.3KB），将三列绝对时间戳改成明确单位的相对时间，慢速历史保留精简摘要；保留实际行情事实、持仓/风险状态和逐笔完整原输入档案。压缩后需要新版本和有界A/B延迟证据，不能承诺一定达到100ms。此轮只修连接边界和诊断，没有偷改策略、候选集或收费价格。

## 验证与当前状态

新增6条关键传输测试先RED；实现后加并发隔离及档案持久化。最终相关84项passed/8.75s，覆盖禁止发送后重试、原有效期、密钥不输出、JEV校验、旧序列化和完整逐笔档案。Ruff相关10文件通过，未跑全套；既有两处架构越层引用不在本轮修复范围。

旧诊断工具会覆盖Trace回调，已改为串接；HTTP数字状态准确记录，不再误写unknown_transport_failure。免费GET已验证生产重连路径；没有新收费JEV持续试跑，也没有交易所订单。

8776加载新版PID6916/session25406；8775保留。四钱包均paused/1000USDT空仓，157条原费用及所有会话/策略/档案/用户README/费用配置核验preserved=true。用户此前手动恢复运行新增的消费保留：确认0.057868902USD、未知预留0.006518736USD、剩余0.035612362USD；原长期0.1累计/0.02单次限额未变。

保留证据 `jev-workbench-before-20261008T161321313888.json` → `jev-workbench-after-20261008T161820683656.json`。早前校时章节里的费用是当时快照，不能用来声称后续用户运行没有收费。

## 官方接口参考

2026-10-09补充：[分析间隔、费用副本测量与多尺度设计](JEV_MULTI_SCALE_INPUT_PLAN.md)。每会话1–10秒设置及新请求本地计时已实现；用户最新确认四层LLM背景（90天/30天/7天/1天）＋20根3分钟线＋60根1秒线，取代旧七窗口与前五层背景建议。六层输入及背景LLM仍待实现。本文件旧样本的Prompt/费用/PID保留为当时记录，不能当作最新运行状态。

- [OpenRouter JEV调用与输入结构](https://openrouter.ai/blog/tutorials/how-to-use-jev/)
- [历史generation统计GET接口](https://openrouter.ai/docs/api/api-reference/generations/get-generation)
- [HTTPX异步Trace](https://www.python-httpx.org/advanced/extensions/)
- [HTTPX连接重试范围](https://www.python-httpx.org/advanced/transports/)

JEV输入应该保持有限、明确；确定性计算与执行放本地。此次手工发送前重连还覆盖本机安装版本httpcore的代理TLS路径，该路径没有沿用普通HTTPConnection的retries循环；没有修改或升级依赖。
