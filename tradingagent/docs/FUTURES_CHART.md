# 合约图表周期

2026-10-08。图表与首次 LLM 历史窗口独立：会话的市场/币种、风格、首次分析记录不由图表控件改写，切换不调用模型、不下单。

## 已接入

- 大盘周期：1m、3m、5m、15m、30m、1h、2h、4h、6h、8h、12h、1d、3d、1w、1M；显示100/300/最多499根。合约来自当前会话，不接受图表请求指定其他账户或交易市场。
- 使用 Binance 公共 `/fapi/v1/klines` 的原生周期；仅绘制已收盘数据，每30秒检查更新。月线按日历月校验；周线按UTC周一；缺口、重复、错误收盘时刻及不匹配的身份拒绝。
- 最多499条单次读取，上市时间短时显示实际数量；最后一根未收盘时会排除，选择“最多499根”可得到498根。独立HTTP客户端和16项/15秒缓存，图表不会占用首次分析或持仓维护客户端。
- 切换周期立即隔离旧图；取消与版本检查防止旧请求晚到覆盖当前选择。公共读取失败60秒后重试；同一周期已有确认数据时保留并标明本次更新失败与原采集时间。无已确认数据时显示缺失，不补造走势。
- `GET /api/analysis/chart?interval=5m&limit=300` 使用既有浏览器认证，只读当前会话。返回会话ID与独立 ChartHistory；Flash仍使用创建时确认的1/7/30天小时历史，图表轮询不重新claim或修改该记录。

## 1秒的接入边界

2026-10-09核对更正：当前接入的USDT合约[公共REST历史K线接口](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#kline-candlestick-data)周期列表最小为1m；不能据此断言全部合约原生接口均无1s。[连续合约WebSocket文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market#continuous-contract-kline-candlestick-streams)已列出1s，可优先验证`btcusdt_perpetual@continuousKline_1s`。连续合约REST历史接口仍最小列到1m，不能承诺可回补秒级历史。此次公共GET因代理ConnectError、直连ConnectTimeout未取得响应，尚未实测1s流。当前控件仍显示“需接成交流”并禁用，后端1s返回422；这是现有实现限制，文案待接入时修正。

后续优先验证交易所原生1s连续合约流；可用时直接接入并缓存，不以本地成交聚合作为默认前置工作。原生流的断线/冷启动仍需标明缺口，不能从分钟K线反推秒内价格或填补走势。成交流可独立用于更细的主动成交特征，或在原生秒线不可用时另行评估聚合方案。目前没有实现秒线流、未收盘K线实时更新、缩放或向前加载。

## 验证证据

10项实际RED后：历史/原生周期/会话接口与独立资源生命周期62 passed（4.36秒）；连接池占用补充1项实际RED后最终63 passed（4.90秒）。Node周期切换/历史隔离/旧响应/连接失败保留/无效数据检查通过，变更文件Ruff与JS语法通过；仅定向检查，未跑全套或收费模型。最终日志 `chart-pool-final-20261008.txt`，此前 `chart-intervals-final-20261008.txt`、`chart-intervals-ui-final-20261008.txt` 保留。

`output/verification/chart-intervals-public-verified-20261008.json` 实际BTC 1m/5m/15m/3d/1w均100根、1M 85根；前两次公共传输失败报告保留，诊断后成功，没有修改代理或放宽校验。`chart-intervals-before/after-20261008.json` 核对原会话、92v1、首次记录hash及无钱包状态保持一致，并验证修复后正式预览5m/15m各100根（采集03:45:41/03:45:43 UTC）。正式浏览器已显示BTCUSDT/15m/300根、03:45:54 UTC采集，图表与下面首次1h窗口分离，截图 `chart-intervals-ui-20261008.jpg`。只做短期联调，不宣称长稳。

运行曾间歇503，临时只记录类型的连接诊断实际观察 `ConnectError → EndOfStream → SSLEOFError(errno=8)` 后 PoolTimeout。核对安装的httpcore代码：代理CONNECT成功后TLS握手失败，隧道的HTTP连接未关闭、保持ACTIVE并占用最大连接数，后续读取耗尽池。公共客户端在收到响应前的ConnectError/ConnectTimeout/PoolTimeout关闭旧客户端并重建，以回收隧道，再最多重试一次；串行读取锁确保没有其他在途请求被关闭，仍受15秒总时限。HTTP限流、已读取响应和数据错误不重放；实际模拟池占用RED/GREEN验证。代理/上游最初TLS中断具体原因未确定，不能把短期成功称长期稳定。临时诊断入口已退出，正式CLI仍使用原7897代理，不安装/修改依赖。
