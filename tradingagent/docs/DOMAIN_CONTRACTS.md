# T02 领域契约实现说明

T02已实现common精确值、Pydantic2冻结基类、账户/订单/成交/归属、行情与风格会话、建议、风险、费用、复盘、paper及全部核心Ports。
金额JSON使用十进制字符串；时间带时区UTC；extra=forbid；集合为tuple。

## 已验证的契约

- Balance精度与不可变；Candle的OHLC和正时间窗口。
- TradingStyle以严格0–100整数保存；风格上下文权重精确相加为1，版本变更使旧快照失效。
- ObservedOrder终态不能倒退、累计成交不能下降、不接受其他账号或更早观察。
- PositionView未知成本为None，不能填0；KNOWN持仓必须有成本值。
- AccountSnapshot资产不能重复；TradeBatch只包含同账号/市场/品种且trade_id不重复。
- ObservedTrade执行者为HUMAN；TradeAttribution默认UNCLASSIFIED，关联建议或分类需用户确认。
- MarketEvent保留exchange/received时间质量，payload不能换品种。
- MarketSnapshot只含按序、不重叠、已收盘且不晚于as_of的K线；空数据为WARMING。
- FeatureSnapshot保留未预热值None；T05已实现指标纯函数，实时运行仍待缓冲/传输装配。
- AccountPort、MarketDataPort、ClockPort、SessionStorePort均使用自有类型。
- FakeAccount验证只读结构、scope与cursor，不能回退未知cursor或提供资金写方法。

## 待实施

领域schema_version仍为1，与SQLite数据库的user_version不同。
T03数据库统一升级到v5，四个持久化Port已验收，保留原会话和审计。
T04已实现隔离paper模拟与回放；风险算法、指标计算、模型路由、复盘服务继续实施。
MarketSnapshot.latest_quote_at保存报价有效时间，book_as_of独立保存订单簿有效时间；未知时间仍可表达，但paper禁止未知/过期报价成交。接收时间不替代已知交易所报价时间。
Candle.quote_volume未知为None。T05 VWAP需要真实成交额，不能从收盘价估计；字段和指标契约见MARKET_DATA_CONTRACT.md。

## 已实现的后续契约

- 决策快照冻结市场、特征、账户、风格修订与触发；禁止跨品种及未来上下文。
- 建议与用户反馈分别保存；建议状态只允许明确前向转换，修改/关联不改原始作者。
- 风控结果用ALLOW/BLOCK/UNAVAILABLE；未配置数量纪律保持None，不随滑杆自动产生限额。
- 费用请求、预留、用量与余额分别定义；未确定计费保留预留，已结算需要实际费用。
- 普通事件与状态只接受自有类型和明确kind；不提供任意字典payload，拒绝额外敏感字段。
- 复盘revision>1必须引用父版本；成本未知时PnL保持None，回访明确数据截止时间。
- paper显式mode和paper:账号命名空间；真实成交类型不用于模拟成交。
- T02完成契约与行为测试；T03预算存储已在后续步骤实现，风险算法、模型调用和复盘服务尚未运行。

ModelRequest/Response单独放在model_calls.py，避免费用与建议类型形成循环导入；决策目的枚举放在common.py。
JournalEvent可保存DecisionSnapshot及TradeGroup，确保完整原始证据与分组能持久化。
真实账户/订单/成交/归属拒绝paper:前缀。TradeAttribution.identity包含venue/market/account/symbol/trade；aggregate_id是该tuple固定JSON编码的SHA256，带trade-attribution:前缀。
StateRecord中AgentSession与ReviewRevision的内部revision必须与CAS envelope一致；其他状态使用独立envelope revision。
会话生命周期仅完成领域转换；应用启动/暂停/关闭和页面控制由T07继续实施。

## 验证命令

```powershell
conda activate tradingagent
python -m pytest tests/domain tests/architecture -q
python -m pytest -q
python -m ruff check .
```

通过已实现类型的测试不代表整个T02或首版已验收。
