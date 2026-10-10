# JEV 长期费用配置与动态杠杆

2026-10-08 用户明确：杠杆不要固定，费用不要到期。沿自主推进/减少测试授权原地实现，无新依赖、Agent、工作树、提交或真实订单。

设计：

- 参数模式由JEV选择完整比例/数量/杠杆方案，沿现有候选1/2/5/10、最大10倍迁移当前已暂停空仓钱包；初始2倍只是账本初始值。原1000USDT、500名义仓位/20亏损上限等保留，不能因迁移重置资金。迁移须CAS、无pending决策/未知执行，旧/新policy及账户配置记录可审计。
- 新具名费用配置expires_at=null、价格valid_until=null；不设置远期日期替代无期限。旧有限期配置保持兼容与不可变。当前0.10USD累计/0.02USD单次上限保留，引用旧根费用记录并保留已发生费用与未知预留。
- SQLite在reserve同一写事务中检查所有日期的消费/预留和永久累计上限，跨天/重启/其他模块不能重置累计金额。日/小时限制作为额外守卫，页面显示长期有效和累计费用。
- 价格当前核对OpenRouter官方JEV页：输入0.042USD/百万，输出0。无时间失效不等于忽略收费：实际超估算/预算继续冻结，并保持原故障暂停机制。
- 服务重载后钱包仍暂停，模型在页面明确启动后才派发；本轮不制造成交或解除行情守卫。

执行：

1. [x] 写无期限/跨天累计/参数迁移回归并验证RED。
2. [x] 实现日期可空、价格有效性、原子累计预算与页面/启动工具。
3. [x] 实现安全迁移及审计，精简相关验证。
4. [x] 核对原状态、停止本任务8776、备份、迁移及创建独立长期配置，同库重启并免费核验。

验证与当前运行（2026-10-08 17:29上海）：

- 首批4项实际RED；余额读取最低累计封顶另1项实际RED。最终相关120 passed /15.64秒；Ruff/format 16文件和JS语法通过。没有全套测试、新依赖或新Agent。
- 回归覆盖跨天保留确认/未知费用、并发只能占用剩余额度、无期限价格、旧有限期兼容、原资金保留、CAS/幂等迁移、离线10倍开仓及有仓迁移拒绝。
- 新配置 `data/jev-paper-continuous.local.json`：累计0.10USD、单次0.02USD；expires_at/price.valid_until均为null，具名continuous-paper-v1授权附加在原共享账本。免费Key认证200，没有模型请求。
- 8776原钱包已审计迁移为parameterized，候选1/2/5/10、max10；1000USDT/空仓/paused保留，初始账本2倍未充值。旧/新类型化策略成对保存在futures_policy_migrations，原始JSON另有迁移前快照及完整备份。旧JSON未带v2字段，核验按类型化默认值补全比较，没有把默认字段补全误记为额外策略变更。
- 服务PID35912/session44954以新配置同库重载，8775未重启。页面/API200，费用active=true/read_only=false/长期有效；paid_models_enabled=false、真实订单false。浏览器实际显示“空仓，开仓时由JEV选择”和候选1/2/5/10。
- 15笔原费用记录哈希一致：spent0.002459016、held0.002161068、remaining0.095379916；钱包/原会话/92v1/全部旧费用policy/原README哈希一致。没有新增收费/订单；原cycles数量一致。
- 当前maintenance_unavailable仍存在：本轮没有修复行情守卫，也没有启动真实v2决策。独立只调杠杆（不加仓）、止盈止损、真实参数联调、长稳、Testnet仍待开发。

证据：`output/verification/jev-continuous-final-green-20261008.txt`、`jev-continuous-before-20261008.json`、`jev-continuous-after-20261008.json`、`jev-continuous-backups-20261008.json`、`jev-dynamic-migration-20261008.json`、`jev-continuous-config-20261008.txt`、`jev-continuous-server-20261008.txt`。备份使用SQLite在线备份API，完整包含已存在的合约扩展表并核对schema/完整性/外键；原core CLI备份仅认可核心表，合约扩展兼容仍应统一完善，不宣称其已支持本钱包。

官方价格来源：https://openrouter.ai/typesafe/jev-1.13 ，本轮免费查证；不重新索取Key。
