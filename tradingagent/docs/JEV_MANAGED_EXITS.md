# JEV 自主止盈止损

2026-10-08 用户确认：暂由 JEV 决定止盈、止损时机，不要求用户先制定固定比例或价格。沿原自主开发/精简验证授权，复用持仓决策与执行，不建立新的阈值触发器。

当前行为：

- 每轮模型请求明确止盈/止损职责由JEV承担；根据当前行情、历史、持仓方向、程序计算的平均开仓价、未实现盈亏、资金费/手续费、风格与纪律判断。
- 参数模式选择现有完整候选：WAIT、同向加仓、按当前数量部分减仓、CLOSE全平。盈利和亏损都允许减仓/全平；不把盈利减仓误限为方向失效时才能执行。
- 新上下文position_management/jev-managed-exits-v1，take_profit_mode和stop_loss_mode均jev_decision，fixed_price_triggers=null、exchange_protective_orders=false。参数问题集futures-plan-v3，旧固定金额问题集futures-action-v2；旧审计/钱包/策略不重写。
- JEV决定是否及何时退出，程序仍确定精确数量并检查报价、持仓/版本、资金、仓位和杠杆上限、亏损纪律及费用。现有累计亏损上限限制增加风险，不是额外的固定止损平仓阈值；reduce-only不能反向开仓。
- 当前运行周期60秒，模型或行情失效没有新的JEV退出决定；暂停保留资金费/报价/模拟清算维护。当前没有交易所保护挂单，不宣称断网或模型失败时仍能按JEV意图自动止损。保护单与更快触发属于后续可选加强，不要求用户现在提供阈值，也不作为本轮框架前置条件。

验证：4项多空盈利/亏损、部分减仓/全平路径实际RED，缺position_management。实现后39项原参数/核心回归通过，4项新测试首次因Decimal科学计数法文本断言失败，改为数值比较后4 passed /3.32秒。没有为已经通过且代码未变的39项再次跑全套；Ruff/format通过。

同库8776重载PID43956/session93425，页面/API200、浏览器已显示JEV自主判断与无需固定比例。迁移前后原会话、策略、费用、旧授权及长期配置/README哈希一致；原1000USDT/空仓/paused，spent0.002459016/held0.002161068/remaining0.095379916不变。8775未动；本轮没有收费请求或交易所订单，离线模型选择不能算真实JEV主动止盈止损验证。

证据：output/verification/jev-managed-exits-red-20261008.txt、jev-managed-exits-green-20261008.txt、jev-managed-exits-final-green-20261008.txt、jev-managed-exits-before-20261008.json、jev-managed-exits-after-20261008.json、jev-managed-exits-server-20261008.txt。下一仍先解决公共行情间歇性维护问题，再观察真实参数决策与退出、长稳及Testnet。
