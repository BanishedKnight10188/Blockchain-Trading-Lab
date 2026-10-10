# USDT 合约公共运行行情

2026-10-07。接续已验收的合约Paper内核，为用户选定的任意可交易USDT永续提供独立运行事实。公共数据无需账户Key；本阶段不装配模型、Web操盘或交易所执行。用户已授权持续自主开发，沿用原目录、正式Conda环境、既有依赖；无需反复确认相同范围。

## 边界和接口

新建独立领域 `FuturesMarketSnapshot`、`FuturesContractRules`、`FuturesFundingWindow`，Port `FuturesMarketPort`。Binance适配器 `FuturesMarketClient` 拥有自己的 `FuturesPublicClient` 与生命周期，不与历史/Flash分析共用网络锁；核心不依赖HTTP类型。

- `snapshot(symbol)`：只请求选定币种premiumIndex和bookTicker，输出明确市场/币种/来源、mark/index、bid/ask及对应交易时刻/本机接收时刻、顶层数量、行情费率与下一结算时刻。合成的quote直接满足现有PaperQuote契约。
- `rules(symbol)`：读取exchangeInfo中唯一匹配的TRADING/PERPETUAL/USDT quote+margin资产，输出PRICE_FILTER、LOT_SIZE、MARKET_LOT_SIZE、MIN_NOTIONAL、PERCENT_PRICE、marketTakeBound。必须读取tick/step，不从precision推算；保留公开规则与捕获时刻，不将ignore的maintMarginPercent作为风险档位。输出不是完整交易所风险检查，后续应用明确映射到本地固定模拟规则。
- `settlements(symbol, after, through)`：返回(after,through]内已结算Regular资金费、结算mark与时刻，严格升序及窗口范围。特殊或缺失rateType、缺mark、重复/倒序/错币种均拒绝完整批次，不丢掉记录后声称成功；不使用premiumIndex费率扣钱包。

标记价/盘口独立5秒时效，未来、零/交叉盘口或缺字段拒绝；请求耗时不能刷新交易所时间。premiumIndex第一次收到时单独校验非未来/新鲜，book完成后再次按最终时刻检查两份证据。下一结算时刻仅用于展示与调度，不硬编码8小时或猜测未发生的结算。snapshot整个读取限5秒，不重试、不回退到历史K线或Spot。后续应用仍须执行前重新取quote并通过已验收水位守卫。

所有网络仅固定fapi公共GET白名单、明确symbol、无Key/signature/任意URL；保留原客户端4MiB上限、重复JSON拒绝、identity编码、取消清流、429/418冷却和无自动redirect/retry。资金费单窗口最多31天、页1000、最多4页，达到容量但仍未覆盖窗口时明确失败，游标+1ms避免inclusive端点重复；空批次是查询窗口内未返回事件，不代表应凭空补资金费，也不能证明到期结算已完成发布。后续后台结合调度时刻等待结算记录，不能在历史迟到时跨过持仓变动。时间输入须UTC且毫秒对齐。

新DTO自身与嵌套quote/contract/funding实例必须完整重验，不能因曾通过Pydantic构造就信任model_copy后的字段。对未校验实例序列化时不打印原输入警告，重建后仍严格拒绝错误市场、资产、负价格、越界费率、Special和错误版本；不修改其他领域模型的全局行为。

规则缓存300秒、至多32个币种，过期失败时不返回旧规则，下一次重试依旧受HTTP冷却；目录异动后不允许新风险，已有仓位的风险维护不能依赖目录缓存继续交易。此阶段不实现轮询后台/持久游标或资金费与同刻订单调度，这些属于运行接入。

## 验收与后续

先通过真实客户端+MockHTTP验证中文/普通USDT符号、时间和金额、过滤器分歧、分页、特殊结算、限制/取消/失败；复用原目录/历史测试。最后有界真实公共探测ETH和其他合约，只读取、不创建用户会话/钱包。若网络失败如实保存，不当成Fake行情成功。没有新增依赖，模型/账户/订单0。

后续是独立JEV上下文与结构化候选、Paper后台的资金费重放和风险维护、受保护Web配置/启动/持仓。旧用户Spot暂停会话保持原事实，到期收费不续期。

接口依据：[Binance USDⓈ-M 公共市场文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)，2026-10-07核对：public premiumIndex/bookTicker的独立时间；fundingRate按inclusive范围和升序分页，Regular/Special及结算mark；exchangeInfo明确不用precision替代tick/step。具体模块与测试见 [FUTURES_MARKET_IMPLEMENTATION.md](FUTURES_MARKET_IMPLEMENTATION.md)。
