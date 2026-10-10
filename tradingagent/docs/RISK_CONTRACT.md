# 确定性纪律契约

修订：2026-10-05。T07内核离线实现；所有金额/阈值示例为Fake测试配置，未提供个人资金纪律或实盘策略。

RiskService只依赖Domain/ClockPort，不调用模型、网络或交易方法。风格强度不参与硬风险计算；同一证据和纪律下0与100结果相同。风格只在后续解释或明确版本的策略配置中使用，不在本模块创建未经验证的信号阈值。原生TradingStyle.context是style-v1权威，不新增重复的styles.yml配置。

## 结果与资料

- ALLOW：本次输入通过已实现检查；不授予交易权限，不表示盈利概率或真实成交保证。
- BLOCK：存在明确资金或纪律违规，报告全部已知原因。
- UNAVAILABLE：资料不足、陈旧、不一致或不支持，不能冒充正常HOLD。
- 同时有违规和缺资料时返回BLOCK并保留两类原因；无资料不能用推测的零余额判违规。
- 行情READY且quote有效时间不超过5秒；账户FRESH且已有确认版本、账户/特征不超过60秒；已预热特征、当前trigger和建议引用都必须可用。
- 检查账户Spot/BTCUSDT、PositionView等于BTC free+locked；卖出只依据free，locked不能视作可卖。未确认的账户不提供具体资金判断。

## 数量与费用

- 用户未设max_buy_quantity/max_sell_quantity时，不放行含具体数量的建议；BUY还要求max_position_quantity。默认观点没有quantity，不生成默认资金比例。
- 数量检查交易所min/max/step、当前book上的最小成交额及用户持有量上限。数值建议必须有明确过滤版本及5秒内book。
- RiskContext.cost_buffer_rate与cost_policy_version成对显式配置，默认未知。测试中该比例用于保守资金预留：BUY按ask×quantity×(1+buffer)，SELL按quantity×(1+buffer)预留BTC。无配置不放行数值建议。
- buffer并非交易所实际手续费或已验证滑点；它不能证明其他手续费币种资金充足或保证人工订单能成交。真实费率/过滤配置与使用验收在后续装配完成前仍缺失，不能将Fake配置自动用于实盘。
- 金融计算先限制128有效位与±128指数，再用512位Decimal上下文；不随调用方默认精度舍入，极端输入直接UNAVAILABLE。

## 日亏损

用户配置max_daily_loss_usd后，BUY需要明确完整、60秒内、Asia/Shanghai同一天的USD亏损证据；不从未知成本或USDT余额变化推算USD损失。达到上限停止增加风险；SELL/HOLD仍独立接受其他检查，系统不会强制新交易。默认未配置日亏损值，不冒称为0。证据生成、成本与费用完整性由T13处理，真实值尚未联调。

风险服务在T11发布前重新检查当前证据；当前单元测试不代表在途建议失效、Web装配或全链路已经完成。
