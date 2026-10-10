# JEV 本地 Paper 自动操盘

2026-10-06，用户将本任务提升为当前最高优先级：先完成本地Paper，再接Binance Testnet；希望尽快使用真实JEV。2026-10-07用户确认首次累计上限1 USD、单次模型上限0.02 USD。有效价格/试跑窗口、本机凭据与明确启动确认齐备后才收费，不自动续期。

## 交付目标

JEV操盘页能够配置虚拟资金和明确的试验策略，启动/暂停独立后台，显示判断、模拟成交、BTC/USDT余额和费用；刷新和服务重启保留事实。真实JEV直接读取Paper持仓与行情，不等待Flash或JEV建议。模拟交易永远不调用Binance订单、撤单、划转或提现接口。

用户已有持续自主开发授权；使用brainstorming/writing-plans/TDD，在指定目录inline推进，用户管理依赖，不另设设计审批或Git提交。

## 环境与身份

操盘方式仍是advisory/auto；新增执行环境paper，默认testnet保持兼容。Paper启动要求trader启用、auto和paper明确确认，JEV建议开关与Flash配置保持独立。Paper允许读取生产公共行情，因为虚拟账户不读取生产账户余额；testnet现有生产数据混用守卫保持。

虚拟账户固定`paper:<session_id>`，与交易所事实的account_ref、余额、成交及游标隔离。风格使用已明确确认的活动会话0–100原值和style_revision；不在Paper配置中创建另一个默认风格。风格、会话或操盘模块版本变化时停止交付旧结果。

## 模拟账户与纪律

第一次创建虚拟账户明确确认initial_usdt（初始BTC为0）、order_quantity、max_position_quantity、max_run_loss_usdt、fee_bps、slippage_bps、max_price_drift_bps、min_confidence和strategy_instructions。页面示例参数只用于模拟实验，确认前不创建、不启动；同一会话不能重复充值或重建。

金额使用现有精确Decimal边界；数量步长和最低成交额采用明确标注的Paper过滤器，不能冒称实时Binance过滤器。手续费和滑点复用PaperSimulator，保留paper-top-of-book-v1。损失限额以USDT计价并作用整次模拟，包含费用和当前bid标记，不将USDT当USD。风格不改变资金、仓位、亏损和数据时效上限。

## 持久化与派发

复用核心SQLite v7的domain_states与STATE_CHANGED审计，新增owned PaperAccountState/PaperCycleState类型；不改写旧账户事实，也不另造缺乏事务保护的内存钱包。模拟资金变动、cycle状态和审计同事务提交。

模型请求前原子claim一个pending cycle，检查当前会话/style、trader开关/模式/环境/配置版本和余额版本；多进程/重复触发只有一个claim成功。每账户只允许一个在途请求，不积累队列。成交提交时在同一SQLite写锁中重新检查上述核心状态，禁止晚到结果穿过暂停/关开/风格变化。已产生模型费用仍记入原预算账本。

重启恢复资金/历史但暂停模拟，不自动恢复收费或订单；遗留pending标记作废，不重新提交同请求。不得通过重复请求生成第二笔模拟成交。

配置/启动请求绑定所确认的session_id、style_revision及account_ref。启动保留严格余额CAS；暂停另外绑定activation_revision，允许同一次运行的旧余额版本暂停，但拒绝跨之后重新启动的旧操作。SQLite写锁内核对，暂停不改变资金。页面遇到服务器设置版本变化保留输入、更新版本并清除确认，要求重新核对。

## 判断与行情

沿用固定typesafe/jev-1.13的typed Decisions API、BudgetedDecisionModel和共享预算/小时额度。提供BUY/SELL/WAIT候选与用户策略、价格时间/来源、有限报价窗口、Paper余额/纪律、原风格和版本。无行情、过期、未来时间、模型失败、低置信度或硬纪律不满足时不成交；不把不可评估写成模型已判断WAIT。

有限行情窗口为最近12根已闭合K线；未来闭合项排除。指标仅接收同BTCUSDT/同as_of且60秒内的快照，否则明确为空，不拿另一时点的特征拼接。

独立Paper后台单在途；行情使用显式binance_public或offline_demo。离线演示源和Mock决策均明确标注，不声称真实JEV或真实行情。真实JEV未配置时不自动降级Mock。公共行情只读Adapter已存在，Paper只消费其market/feature事实，忽略exchange account字段。

模型响应必须绑定request/question_set、只接受候选和一致概率分布；返回后重新读取最新报价，超过5秒、deadline或配置价格偏移上限拒绝成交。执行数量由代码与显式配置确定，不交给模型自由生成。

## 收费配置

新增显式--paper与互斥--paper-mock/--paper-model-config入口；默认Web不创建Paper worker或读取模型Key。真实配置文件不含密钥，包含首次试跑累计费用上限、单次上限、核对过的价格与明确试跑截止时间。Key仅从本机OPENROUTER_API_KEY读取，错误脱敏，不进入状态/日志/页面。

首次收费试跑固定在同一Asia/Shanghai预算日内，deadline不跨午夜且不超过价格有效期；共享持久日预算取首次总额，因此重启、其他请求或跨会话不能绕过这次费用门槛，截止后不自动续费。UNKNOWN保留预留。强模型和Flash收费调用仍关闭。

2026-10-06核对[OpenRouter模型页](https://openrouter.ai/typesafe/jev-1.13)：输入0.042 USD/百万token、输出0；这不是永久价格。端点和typed questions见[Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)。费用未明确、价格过期或Key缺失时真实运行不启用。

## 页面与验收

现有/jev-trader增加执行环境、Paper配置/创建、启动/暂停、来源/会话风格/运行原因、余额/权益/判断/费用/延迟与最近50条cycle。受保护API沿用cookie、Origin、CSRF、confirmed和严格版本；前端刷新不覆盖正在编辑或保存的配置。

测试覆盖真实SQLite重启/审计回滚、重复与并发claim、暂停和风格变更后晚到拒绝、余额/费用/滑点/亏损/价格时效、真实HTTP Adapter的MockTransport、默认无收费/资金调用、Paper与生产公共行情组合。执行有界离线自动运行并保存真实时间/成交/余额/停止证据，再在费用和本机配置就绪后实际JEV+Paper联调。Testnet留到Paper验收后，不将本轮称已接通Agent OS。
