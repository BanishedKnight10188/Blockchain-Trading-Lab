# Jev独立模块实施计划

## 2026-10-06 用户再次澄清：三模块互不绑定（当前实施优先级最高）

Flash常态主分析；Jev建议可选择开启；另有专门Jev操盘模块。先前将Jev建议开关与自动执行绑定的JI1/JI3A语义已被覆盖，旧验证保留为中间证据，不能称最终交付。

Ruling：复用Flash/Jev Adapter、预算门控与CAS设置，新增独立trader配置、开关与设置版本。advisory/auto只控制操盘模块的执行方式，Flash与Jev建议不受它支配；建议关闭不影响操盘，操盘关闭不影响建议。两条Jev路径各自持有typed request/activation/在途资格，不串行调用。

### JI3B：拆分建议与操盘的设置和页面

Files：model_modules新增JevTraderSettings和analysis_mode=continuous；AgentControlState新增trader与advice_revision/trader_revision（默认兼容旧离线设置）；OperatingSettings.public_state依据trader_enabled而非建议开关。AgentControlService新增update_advice/update_trader，CAS与审计复用。Web`/overview`包含大盘、Flash主线与可选Jev建议（jev-advice.js）；新`/jev-trader`只含操盘开关、建议/自动方式与testnet执行状态（jev-trader.js）。`/agent`为总览别名，删除本轮中间agent.html/js。新增两条受保护的scope API，互相保留设置，独立版本不因另一模块修改而增加。CLI增加--jev-trader/--no-jev-trader；--jev只表示建议模块。

- [x] RED：默认配置、建议/操盘四组合、互不增加对方版本、持久恢复、旧设置缺trader默认关闭。
- [x] RED：ASGI两页面与独立接口；关闭建议不暂停已选操盘、关闭操盘不关闭建议，始终0预算/无执行。
- [x] 实现拆分，修正当前文案/状态/运行文档，保留历史验证脚本与wheel；补旧请求拒绝隐式覆盖及future版本/双store旧快照竞态回归。
- [x] GREEN：最终1113项/137subtests、Ruff、实际JS/浏览器页面与刷新、新wheel隔离重启、独立复核无剩余P1/P2。具名证据见JEV_PARALLEL_VERIFICATION.md。

后续JI2细化为Flash常态lane、JevAdvice可选lane、JevTrader独立lane；JI5只能由JevTrader输出经代码门控进入执行器，JevAdvice没有资金Port。预算账本/账户scope/风格/硬纪律共用但请求、开关版本、队列与在途锁独立。

> 使用superpowers:executing-plans在用户指定目录inline实施；沿用自主推进授权，依赖由用户管理，无Git提交。实现先用superpowers:test-driven-development。用户已确认自动操盘先testnet，并要求同时提供建议模式；不启用真实资金执行。

**Goal:** 将Jev从默认串行级联计划改为独立可关闭的持仓决策模块，先关闭配置与派发边界。
**Architecture:** Domain持有JevModuleSettings/OperatingSettings，Application预算执行器在reserve前及交付前核对启用版本；CLI/系统投影保存开关与mode，Jev与Flash不互相调用。auto固定testnet，执行资格未就绪为blocked。后续runtime/UI/MCP/执行器另分任务，当前无任何实际交易。
**Tech Stack:** 现有Conda Python3.12/Pydantic2/httpx/pytest；不安装新依赖。
**Spec:** JEV_INDEPENDENT_MODULE.md；用户最新要求优先于MODEL_SELECTION.md旧级联规划。

## Global Constraints

- 只在D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent；正式解释器C:/Users/exile/anaconda3/envs/tradingagent/python.exe，PYTHONUTF8=1。
- 强模型calls_enabled固定false；实际收费/交易仍不执行，不读取模型凭据。
- Jev独立开关不改变Flash身份、日预算或资金纪律；默认关闭，开启配置不等于已连接。
- 既有持久预算/UNKNOWN及重复取消费用结算不变；关闭/重新开启后旧结果不可交付。
- 不安装MCP SDK、猜测工具名/授权scope/testnet，不把主账户持仓当子账户库存。
- 两个mode为advisory/auto；auto execution_environment仅testnet，不接受live环境，不复用production数据开关。

## Review Focus

1. Jev关闭但port与Key存在：派发前拒绝，零新请求/预留。JI1。
2. 在reserve等待中关闭、在途关闭再开：未派发结算确认0，已派发保留费用但不交付旧结果。JI1。
3. 开启Jev配置：Flash身份/强模型禁用不变，默认Web仍无模型/0预算，不创建外部客户端。JI1。
4. Flash缓慢/故障：Jev任务不等待Flash，无共享在途锁；旧账户/风格/策略拒绝发布。JI2。
5. MCP工具/账户scope/权限改变：能力关闭，不冒充空余额或自动调用写工具。JI4；JI1不新增MCP执行能力。

## JI1：配置与预算前置门控

Files：修改agent_platform/domain/model_modules.py、application/decision_models.py、config.py、cli.py、bootstrap.py、application/system_queries.py；新增domain/operating_modes.py；tests/domain/test_model_modules.py及application/test_decision_models.py；既有wheel检查需显式启用Fake Jev执行器。
Interfaces：JevModuleSettings(enabled:StrictBool=False, mode固定independent_position, account_provider固定binance_agent_os)；ModelModulesConfig.jev；OperatingSettings(mode=advisory/auto, execution_environment固定testnet)；RuntimeConfig.operation；BudgetedDecisionModel(settings=None).set_enabled(bool)，实际变化增加本进程activation revision。
- [x] 写严格开关/配置roundtrip、两个mode/auto固定testnet、CLI --jev/--no-jev/--mode及Web0预算/blocked投影测试，运行实际RED。
- [x] 写关闭零reserve、reserve等待中关闭确认0、在途关开仍拒绝旧结果且费用确认测试，运行实际RED。
- [x] 实现配置与门控；复用settle_owned及独立typed request；bootstrap不装配外部客户端。
- [x] 53项相关专项及重复取消结算GREEN；日志jev-modes-ji1-red/green-20261006.txt。完整回归与阶段复核尚待本轮后续完成。

## JI3A：先落实两个模式的本地选择页（历史阶段，由JI3B替代）

本节中间实现和1105项完整验证已封存；页面/开关语义由上方已完成JI3B替代，不再按旧单开关计划接续。下方清单仅保留当时计划，当前任务从JI2开始。

Ruling：先实施JI3中的持久设置与页面，再实施JI2后台；设置页只依赖JI1配置，不需要已经连接Jev。此顺序让用户可以实际选择两个模式，不把未实施的快速决策/自动执行当作已交付。

Files：新增domain/agent_controls.py、application/agent_controls.py、web/templates/agent.html、web/static/agent.js；修改domain/events.py、bootstrap.py、application/system_queries.py、web/app.py、navigation.html及status/workbench的过时文案；测试application/test_agent_controls.py、web/test_agent_control_routes.py。

Interfaces：AgentControlState固定key=agent-controls，revision/updated_at/operation/jev；未保存的启动默认revision0，第一次确认保存revision1。AgentControlService使用既有StateStorePort原子CAS+STATE_CHANGED审计。ControlSelection要求mode/严格jev_enabled/confirmed=true；Web要求expected_revision非负整数及既有Host/Origin/CSRF。保存设置优先于CLI默认，production读取配置下不可保存auto；重启组合冲突在创建外部客户端前拒绝。

没有新增SQL表/列，复用domain_states通用投影；新owned state加入StateType和typed union。旧事实不重写，schema仍v7。旧版本软件不能读取新增状态，测试仅使用独立库，正式库应先备份后启动新版。系统每次读当前持久设置；未装配模型/执行器始终not_connected/blocked，不能把设置持久化声称为后台运行接通。

- [ ] RED：确认/严格输入/并发CAS与审计同事务、独立store重开/默认覆盖、生产读开关冲突、损坏状态失败关闭。
- [ ] RED：实际ASGI页面/API/cookie/CSRF/Origin/未知字段脱敏；重启保存与mode/jev状态一致，选择auto仍0预算无执行。
- [ ] 实现设置与页面，保持刷新/保存竞态保护；更新无资金路由检查的本地设置白名单。
- [ ] GREEN：专项/既有会话/预算/状态/架构检查；实际JS流程验证、全套回归、最终独立复核与证据。

## JI2：独立持仓runtime（未实施）

先细化session/account/strategy/activation身份与每条lane的调度/预算公平性；再以Fake阻塞Flash时Jev仍完成、旧结果拒绝、单在途与有界合并验证。不临时扩大真实预算或保证未经实测的延迟。

## JI3：独立后台的判断、订单与运行状态展示（未实施）

JI3B已完成两页及独立开关与持久设置，不重做。后续接入JI2的当前持仓/数据scope与时间/判断/费用/延迟和在途状态，以及JI5的订单/对账/暂停信息；沿用Host/Origin/CSRF和确认。结果必须保留各自lane及证据版本，不能将配置开启显示成模型或交易已运行；开始前细化事件与schema迁移。

## JI4：Agent OS只读映射（未实施）

先固定工具Schema与主/子账户scope，Fake tools/call只读映射、失效/续期/超时回归；真实SDK由用户安装。未验证MCP testnet或无人确认接口时不伪装可自动执行。

## JI5：自动testnet执行控制器（未实施）

按用户明确的两mode范围另立具体持久化/执行计划：ExecutionIntent/Port、clientOrderId/原子claim、UNKNOWN_SUBMIT对账、资金/订单过滤器/亏损限制、mode/activation/session版本作废、停止与恢复，Fake费用/成交与testnet事实隔离。只接受验证过的testnet Adapter；Agent OS自动能力无法验证时明确blocked，Direct Testnet联调单独标记provider。默认策略/资金纪律未配置时不派发。
