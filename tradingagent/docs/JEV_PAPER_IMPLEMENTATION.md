# JEV Paper Implementation Plan

> Inline执行：superpowers:executing-plans；测试先行：superpowers:test-driven-development；任务完成独立复核。沿用用户自主授权，不做Git提交或安装依赖。

**Goal:** 优先交付持久、独立、可启动/暂停的JEV本地Paper自动操盘，再接Testnet。
**Architecture:** 新owned Paper账户/cycle复用核心SQLite CAS/审计，claim与成交在写锁中核验会话/style和操盘开关。独立worker消费行情并直接调用typed决策Port，模拟余额和Exchange事实分开，真实模型复用共享预算。
**Tech Stack:** 已安装Conda Python3.12、Pydantic2、SQLite、httpx、FastAPI/Jinja与原生JS。
**Spec:** docs/JEV_PAPER_SPEC.md。

## Global Constraints

- 正式解释器C:/Users/exile/anaconda3/envs/tradingagent/python.exe；根目录D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent。
- 依赖仍由用户管理；不安装/升级/推送/部署/覆盖用户README。
- Paper初始BTC=0，独立paper:<session_id>；风格来自活动会话已确认原值/版本。
- 不向Binance提交订单；Mock与offline_demo明确标注，真实JEV无配置不降级Mock。
- 单次真实JEV上限0.02 USD，2026-10-07首次累计总额1 USD已确认；本机凭据/有效窗口/启动确认齐备才收费，强模型不调用。
- 5秒行情时效、15秒请求窗口、最多60模型请求/小时；真实试跑不跨Asia/Shanghai预算日。

## Review Focus

1. 双进程或重复触发：只claim/成交一次；JP1/JP2。
2. 模型在途暂停、风格/操盘版本变化：保留费用，拒绝旧结果；JP1/JP2。
3. 成交审计失败：余额/cycle/审计全部回滚；JP1。
4. 重启或日期变化：模拟资金恢复，收费与成交保持暂停，trial不自动续期；JP2/JP3。
5. live_public + paper：只消费行情，绝不使用生产账户余额；JP2/JP3。

## JP1：持久模拟账户与事务守卫

Files：新增domain/paper_trading.py、ports/paper.py、adapters/sqlite/paper.py；修改domain/events.py、adapters/sqlite/store.py；tests/domain/test_paper_trading.py、tests/adapters/test_paper_store.py。
Interfaces：PaperSettings、PaperAccountState、PaperCycleState；PaperStore.create/get/latest/claim/complete/pause/recover/recent。claim/complete在事务中核对核心sessions及agent-controls，complete与模拟资金/audit同事务。
- [x] 写严格金额/初始化/style归属、真实SQLite重开、原子claim/重复、暂停/风格变更晚到及审计故障测试。
- [x] 运行实际RED，日志paper-jp1-red-20261006.txt。
- [x] 实现owned领域、PaperStore与typed事件注册，不新增SQL表或重写旧事实。
- [x] 运行GREEN及既有Paper/事件/SQLite架构检查。

## JP2：独立JEV Paper控制与后台

Files：新增application/paper_trading.py、runtime/paper_trading.py、adapters/fake/paper_trading.py；修改domain/operating_modes.py、domain/agent_controls.py、application/agent_controls.py、config.py；tests/application/test_paper_trading.py、tests/runtime/test_paper_worker.py。
Interfaces：PaperTradingService.configure/start/pause/step/public_view；PaperTradingRuntime.start/stop/health；PaperMarketPort.sample；typed Decide Port复用。模拟调用PaperSimulator，成交前后执行明确USDT硬纪律/过滤器/价格时效检查。
- [x] 写不等待Flash、单在途、BUY/SELL/WAIT、低confidence、价格漂移/过期、余额/亏损及关闭/关开竞态测试并跑RED。
- [x] 实现独立后台及明确Mock/离线行情；增加paper环境，testnet守卫仍保留。
- [x] 跑GREEN与原三模块开关/预算取消/风险检查。

## JP3：真实JEV配置与装配

Files：新增bootstrap_paper.py；修改config.py、cli.py、bootstrap.py、runtime/supervisor.py；tests/test_paper_config.py、tests/test_paper_bootstrap.py。
Interfaces：PaperModelConfig（trial_total_usd、single_call_usd、expires_at、ModelPrice）；--paper、--paper-mock、--paper-model-config、--trader-environment；正式OpenRouterDecisionModel/BudgetedDecisionModel，无强模型装配。
- [x] 写默认零Key读取/零收费、非法配置启动前拒绝、时间/价格/单次0.02/累计/小时上限、MockTransport及退出测试并跑RED。
- [x] 实现显式配置/模型装配；有界截止内复用共享预算；无真实配置不调用或降级。
- [x] 跑GREEN；真实费用与密钥不就绪时记录缺口，继续离线工作。

## JP4：操盘页与受保护接口

Files：新增web/paper_routes.py、web/static/paper-trader.js；修改app.py、jev-trader.html/js、runtime/acceptance.py；tests/web/test_paper_routes.py。
Interfaces：GET /api/paper；POST /api/paper/configure、/start、/pause；GET /api/paper/cycles（最近50），局部设置与模拟操作保护。
- [x] 写实际ASGI/CSRF/确认/严格版本、暂停/重启、余额/来源/费用不可冒充真实数据测试并跑RED。
- [x] 实现现有操盘页内Paper控制与持仓/cycle显示，不增加第三主页面。
- [x] 跑GREEN、原始JS竞态检查和实际浏览器配置/启动/暂停/刷新。

## JP5：有界运行与阶段验收

- [x] 运行自动Paper+Mock/真实Adapter MockHTTP，保存成交/余额/费用/暂停/重开恢复证据。
- [ ] JP5真实JEV：首次累计1 USD/单次0.02 USD、本机新Key与本次价格窗口prepare通过。用户即时校正成功，25秒公共行情及实际Web缓存ready；8774已接续持续服务，风格80/v1已在页面确认、收费0/钱包未配置。已修总览盘口图表（13JS/23Web及实际浏览器通过）；行情gap仍间歇出现，硬守卫保留。还需资金/策略/启动确认后实测付费响应/成交及缺口恢复；配置至2026-10-07 16:41:18上海，首次同一库data/jev-paper-20261007.sqlite3不自动续期或重授预算。
- [x] 独立复核发现实际RED→修复；全套测试/Ruff、干净打包与Python -I隔离验证。
- [x] 同步状态/ledger/运行文档，准确区分真实JEV、Mock、公共行情和离线演示；Paper通过后接Testnet。
