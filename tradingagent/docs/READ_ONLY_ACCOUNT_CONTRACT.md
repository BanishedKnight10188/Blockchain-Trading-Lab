# Binance Spot只读账户契约

修订：2026-10-05。T06离线实现已验证；没有用户凭据，不执行真实账户请求。
依据[官方REST签名与账户文档](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)、
[当前用户数据流文档](https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md)、
[RFC4231 HMAC测试向量](https://www.rfc-editor.org/info/rfc4231/)。

## 已实现的签名边界

- HmacCredentials用SecretStr保存Key/Secret，repr隐藏两个字段；不自动发现或加载任何密钥。
- ASCII HMAC-SHA256，query按参数名排序、UTF-8百分号编码，签名绑定实际发送字节；RFC4231 Case2验证。
- 参数有数量/长度/整数上限，bool/float/调用方signature拒绝；时间仅UTC，用整数运算精确转毫秒。
- SignedQuery的repr不显示签名和参数；密钥不进入业务领域、SQLite、输出报告或Git。

## 本次继续的实现顺序

1. 固定https://api.binance.com的ReadOnlyClient，只公开GET白名单。先写拒绝资金写入/任意URL/未知参数、签名、校时、日志脱敏、权限冻结和限流的Mock测试。
2. 私有GET仅account/openOrders/order/myTrades；BTCUSDT订单/成交范围固定。指定公共GET用于校时/过滤；其他路径和方法在网络前拒绝。
3. 先取得服务器时间证明，单调时钟推进签名时间，默认recvWindow5000；不扩大窗口掩盖时差。HTTP响应有界、无代理继承/重定向、取消传播。
4. 凭据缺失/权限错误使账户不可用，保留公共行情主链路；401/403或明确认证错误不自动盲重试。429/418遵从等待期。
5. 账户/观察订单/成交严格映射到自有DTO；按整数交易所ID分页，边界重复幂等，缺历史则成本UNKNOWN。外部账户变化不自动归属给本Agent。
6. AccountSync先用T03原子导入与Fake凭据验证；失败保留最后已确认余额和数据时间，账户15秒刷新；不将陈旧余额标为fresh。
7. 用户数据流按当前WebSocket API只读订阅研究；不可用保留REST，不照搬旧listenKey或扩大权限。

真实权限验证只使用只读查询，不通过下单/撤单/划转/提现探测权限。实际Key由用户在本机配置，不发送到聊天。

## 已实现及实际边界

- ReadOnlyClient固定域名与GET白名单，方法/路径/参数/不支持组合在校时和发网前拒绝；无代理继承、重定向或自动重试。私有SDK日志在当前签名任务内脱敏，含引号/反斜杠异常repr回归。
- 账户采样时间是成功接收时间，updateTime不是余额新鲜度；权限字段不进入执行能力。订单/成交为Spot/BTCUSDT，真实执行者HUMAN，不自动推断决策归属。
- Decimal保留实际quoteQty和手续费币种。旧quote缺失仍能恢复；原成交body不改写。schema v6首次非空补证与源audit同事务，后续矛盾金额拒绝。v5迁移从已完成原子import的审计补证，矛盾时保留原版本和SQLite备份。
- TradeHistory从0或已提交ID inclusive读取，页内ID严格递增，跨页边界重复必须同事实；时间不能倒退或超出接收时刻。默认每次最多4页，每页1000项；到上限仍保存可继续的游标。
- history_complete只表示本次从0读到API末尾；增量批次不冒称完整历史，标志不证明充值来源或持仓成本。成本始终UNKNOWN，成本核算由T13处理。
- AccountSyncService串行化15秒刷新；任何账户/订单/成交读取失败不部分提交，保留已确认余额/as_of/revision/游标并标UNAVAILABLE。限流按更长等待期暂停；存储失败不发布或缓存成功，取消传播。
- ImportResult包含本事务账户快照，避免提交后另一次投影读取竞争；旧结果JSON没有该可选字段仍可加载，新Sync要求原子receipt。
- 可选BinanceUserStream仅发送userDataStream.subscribe.signature，使用官方固定WS API地址，原始排序参数签名（不沿用REST百分号编码）。签名校时后发送；原始SDK帧日志关闭；连接不继承代理、不压缩、不重定向。
- 订阅返回的subscriptionId必须精确匹配。余额/成交通知只映射AccountSignal，不直接记账；初次/重连订阅先提示重对账。HTTP握手、校时和订阅ack限流都保存等待期；认证失败冻结该stream，REST仍独立。
- 用户流单连接最大23h50，取消/断线关闭连接；没有内部无限盲重试。T15消费者/退避及Supervisor装配尚未实现。
- 当前所有账户、用户流证据均为Fake/Mock。未验证真实Key权限、实际成交历史或USER_STREAM订阅；T16单独验收，不依赖真实写请求。
