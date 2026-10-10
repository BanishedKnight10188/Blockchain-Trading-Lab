# USDT 合约公共运行行情验证

2026-10-07，正式tradingagent Conda Python3.12。范围：新公共行情DTO/Port/Provider及现有公共GET白名单扩展；没有装配持续后台、JEV合约策略或Web资金操作。执行计划 [FUTURES_MARKET_IMPLEMENTATION.md](FUTURES_MARKET_IMPLEMENTATION.md)，规格 [FUTURES_MARKET_SPEC.md](FUTURES_MARKET_SPEC.md)。

## 实现和TDD

FM1新增domain/ports/adapters/binance_direct的futures_market模块，Provider自有公共client，不与历史/Flash共用HTTP锁。mark/book时间和数量分别保留，5秒时效、请求总5秒、金额字符串精确解析、所选symbol固定；无Spot/历史价格回退。FM1 RED `futures-market-fm1-red-20261007.txt`实际30 failed/4 passed（模块未存在；4项原公共拒绝边界已通过）。实现后连历史/领域/架构 **105 passed / 115 subtests，1.25秒**，`futures-market-fm1-green-20261007.txt`。

FM2增加真实PRICE/LOT/MARKET_LOT/MIN_NOTIONAL/PERCENT_PRICE/marketTakeBound规则，分别保留市价和普通步长，不用precision或ignore的maintMarginPercent猜算。缓存300秒/32符号，过期刷新失败不回退。资金费Regular且明确结算mark，(after,through]、严格升序、1000每页/最多4页/31天，错误/Special/缺类型/满容量未覆盖全批拒绝。RED `futures-market-fm2-red-20261007.txt`实际36 failed/34 passed（缺rules/settlements）。实现后連历史/合约domain/store/架构 **159 passed / 115 subtests，5.66秒**，`futures-market-fm2-green-20261007.txt`。

独立复核发现2项P2、无P1：FM-R1嵌套实例的字段可被model_copy绕过，FM-R2首次未来mark被第二HTTP等待洗成合法。主agent实际 `futures-market-review-red-20261007.txt` **10 failed/72 passed**，额外自身wrapper重验用例 `futures-market-review-extra-red-20261007.txt` **11 failed/72 passed**。修复为新DTO自身always重验、嵌套对象先重建字段、标记价首次接收立即校验，再检查最终报价；未改变其他领域DTO的配置。`futures-market-review-green-20261007.txt`连原范围 **172 passed / 115 subtests，6.15秒**。

首次GREEN有3项Pydantic serializer警告（受控伪造的int价格/费率），已在未校验实例的转换阶段禁止序列化警告，后续字段验证仍拒绝；没有改变负值/Special/错误版本拒绝行为。最终完整 `futures-market-final-suite-20261007.txt` 实际 **1372 passed / 150 subtests passed，135.38秒，exit0**；仅既有Starlette弃用提示。Ruff `agent_platform tests tools` 与299文件format已通过；README原hash保持，无新wheel。

独立reviewer真实纯Mock探针也确认FM-R1/FM-R2及10项失败；额外探针确认USDCUSDT的1e−18 tick、32项缓存淘汰、恰好4000条/4页的through边界、真实5秒请求超时。复核没有网络、用户DB或编辑；两项问题由主agentTDD修复，不把静态意见当已修复证据。

## 真实公共数据

只对用户既有127.0.0.1:7897代理显式请求固定fapi公共GET，不改系统代理、Key或账户。初次 `futures-market-public-probe-20261007.json/.txt`已成功；首次时间守卫修复后的新证据 `futures-market-public-final-20261007.json/.txt`仍全部成功。没有覆盖初次报告。

| 合约 | 实际quote（仅探测当时） | 规则 | 近1天结算 |
| --- | --- | --- | --- |
| ETHUSDT | mark2565.24，bid2565.47，ask2565.48；mark15:52:30Z/book15:52:30.526Z，received15:52:30.930766Z | tick0.01、市价step0.001 | 3项Regular，各含结算mark |
| SOLUSDT | mark116.54617368，bid116.54，ask116.55；mark15:52:31Z/book15:52:31.576Z，received15:52:31.895260Z | tick0.01、市价step0.01 | 3项Regular，各含结算mark |

上述为2026-10-07一次有界数据事实，不是实时价格或用户成交；当前行情后续必须刷新。探测模型调用0、账户读取0、交易所订单0；不创建钱包或修改用户会话/资金/预算。

## 下一未完成范围

Provider完成不代表用户页面已显示实时合约价格或已JEV操盘。仍需合约Paper后台装配、资金费重放/延迟结算与同刻订单排程、独立风险维护/JEV结构化候选/预算、Web配置/启动/持仓成交。之后有界模型试跑、Testnet/Agent OS与24h验证。完整当前清单 [REMAINING_WORK.md](REMAINING_WORK.md)。

没有依赖安装、模型收费、私人账户调用、交易所订单、提交/推送、部署或用户服务重启；到期费用不续期，原Spot暂停会话和80/v1保持原事实。
