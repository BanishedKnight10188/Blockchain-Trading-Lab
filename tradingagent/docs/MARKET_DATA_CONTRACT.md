# 公共行情与指标契约

修订：2026-10-05。T05代码与离线故障链路已实现；真实REST时间读取成功，真实WS联调尚未通过。
字段核对来自[Binance官方Spot流文档](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)及[官方REST文档](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)。
站点旧路径存在迁移/不可访问，核对使用官方仓库当前正文；不以期货、期权或testnet字段替代Spot。

## 已实现的正规化

- 仅BTCUSDT，trade、bookTicker、UTC 1m kline；组合流名字、品种与payload类型必须一致。
- 价格/数量使用最多128字符的固定十进制wire字符串，不接受指数、符号或空白；防异常指数进入金额运算。时间/ID用非负整数毫秒，拒绝bool及float；错误不回显payload。
- trade使用T交易时间，E仅验证格式，不把较晚发布时间当成交易时间。
- bookTicker缺交易所时间，occurred_at=实际received_at且time_quality=received，不伪造时间。
- K线T为含末毫秒的结束字段；自有Candle.closed_at=T+1ms，形成半开窗口，必须恰好1分钟。
- 同一未收盘与收盘事件的ID不同，防后续重复识别吞掉最终收盘事件。
- x=true不得声称仍在未来的窗口已完成；检查包含末毫秒的T。半开转换的1ms边界仍由快照时间保护。
- REST klines以请求开始的服务器时间下界作保守收盘界限；time响应的serverTime在接收时锚定monotonic，不计网络传输延迟，30秒刷新，取与本机UTC的较早值。跨分钟延迟或本机领先不会把partial行冻结为最终事实。
- REST取样时间只代表接收，time_quality=received，不推定服务器实际发布时间。

tests/fixtures/binance_market.jsonl为按官方字段合成的脱敏测试输入，并非真实账户或实时录制证明。

## 已实现的指标

全部计算使用独立Decimal34/ROUND_HALF_EVEN，不受调用者context影响。
默认EMA12/26、ATR14，最多使用最近120根已完成分钟线；可配置周期会进入algorithm_version与snapshot_id。
只用无缺口的连续尾段，缺失分钟不填价格或零成交量。

| 字段 | 计算与不可用条件 |
| --- | --- |
| EMA | period根close的SMA作初值，之后alpha=2/(period+1)递推；不足period为None |
| ATR | 首根high-low；以后TR=max(high-low, abs(high-prev_close), abs(low-prev_close))；period根均值起步，Wilder递推 |
| VWAP | 尾段真实quote_volume之和/base volume之和；任一quote volume未知或总base量0则None，不用close估计成交额 |
| interval_return | 连续尾段end/start−1；不足2根为None；使用的尾段长度随预热增长，最多lookback |
| volatility | 尾段简单收益的样本标准差；不足2个收益为None |
| volume_change | 最近N根总量/之前完整N根总量−1，默认N=5；不足2N根或分母0为None |
| spread | 最新book的ask-bid，单位USDT；book时间未知或超过5秒为None |

warmup_ready同时要求：市场READY、所有指标已知、新鲜quote时间、新鲜book、
最新闭合分钟等于as_of向下对齐的分钟。新报价无法掩盖K线断更。
历史指标可以保留，但未预热/过期/缺口不能被策略当成当前可用结果。
lookback必须覆盖各周期、两个成交量窗口及波动率所需3根；错误配置启动前拒绝。

## 行情缓冲与恢复

- MarketBuffer事件时间1s/5s/1m成交聚合，分别保留300/60/120根；权威已闭合1m线独立保留120根。成交聚合不冒充Binance最终K线，不填缺失或零量窗口。
- 水位线=最新已接受事件时间−2秒；成交先按事件时间/序号/ID确定排序再闭合窗口。起始不完整或缺口覆盖的成交窗口complete=false。
- 去重缓存默认4096项/300秒双上限，pending最多4096项；已保留的报价、pending和分钟事实继续保护身份。容量溢出、太晚事件、序号回退明确GAP，不静默当新鲜数据。
- GAP观察时间与实际缺失窗口起点独立。REST必须覆盖实际缺失的全部已完成分钟（最多最近120分钟），批次验证后才原子恢复；同ID异事实、改写闭合线或缺行全部拒绝。
- 新as_of不得早于已观察时间，已发布同as_of快照永远不变；历史查询使用存档证据。已收盘WS含末毫秒可接纳，但半开窗口结束前不出现在快照。

## 公共传输

- REST固定https://data-api.binance.vision，仅GET time/klines/exchangeInfo/bookTicker及固定参数白名单；无凭据、任意URL、代理继承、重定向或自动HTTP重试。
- identity编码，非identity Content-Encoding在解码前拒绝；原始响应最多256KiB，网络10秒超时、总请求15秒上限。HTTP/JSON/深嵌套错误脱敏，取消继续传播。
- 429/418使用单调时钟和Retry-After阻塞全部公共请求；无有效头时分别等待60/180秒，不盲重试。
- WS固定wss://data-stream.binance.vision的trade/bookTicker/kline_1m组合流，明确禁止重定向与压缩/继承代理；每帧64KiB，传输队列32，通知队列最多4096。
- reader在实际接收时正规化并append，不受consumer暂停影响；通知溢出明确缺口并重连。REST endTime按服务器时间下界的最新闭合分钟设置，取得120根完整窗口；下次补线也按下界安排，避免本机领先导致整分钟延迟。
- 断线/serverShutdown/30秒无帧/23小时50分轮换用2/4/8…60秒退避；REST限流等待更长时遵从其等待期。未完成首个缺失分钟或补线不足时维持GAP。
- stream单consumer；结束或取消清理reader、REST bootstrap、queue getter与连接。调用者持有并关闭REST客户端。
- CLI market-probe必须显式--live-public，1–60秒，独占创建报告；不创建会话、不读账户、不建议/下单、不发模型调用。

## 证据与限制

离线market测试118项通过。公共服务器时间真实读取成功，时差约0.68秒；25秒诊断未建立WS连接，报告output/verification/t05-public-probe-20261005-03.json为no_data，不能冒充实时行情成功。
当前未持久归档每个原始行情/迟到事实或实现跨进程市场恢复；这些由T15统一运行和事件日志装配完成。当前BufferUpdate明确返回未接受事实的身份与原因。
网络条件改变后再使用诊断CLI验证；不为失败扩大主机白名单、启用资金权限或重复空跑。

JEV仍未定义；指标通过手算和类型测试不等于策略收益已验证。
