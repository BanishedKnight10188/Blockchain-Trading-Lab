# 多币种会话 Implementation Plan

按用户授权在当前开发目录原位执行；使用 executing-plans、test-driven-development 与 verification-before-completion，不提交或安装依赖。

**Goal:** 创建会话时选择全部可交易USDT永续之一，自动取得历史并准备独立首次Flash分析。
**Architecture:** 新增会话分析身份、合约只读Provider和持久首次任务。Spot流与执行保持原有身份，合约页面独立读分析结果。
**Tech Stack:** 既有 Conda Python3.12/Pydantic/httpx/SQLite/FastAPI/原生JS。
**Spec:** `docs/SESSION_MARKET_SPEC.md`。

## Global Constraints

- Python=`C:/Users/exile/anaconda3/envs/tradingagent/python.exe`；不安装或升级依赖。
- USDT永续目录动态过滤，不人工维护币种白名单；周期1h，历史1/7/30天。
- 新会话风格0–100整数明确确认；旧会话默认spot/BTCUSDT，身份不可修改。
- 首次Flash与JEV独立；真实Flash默认未装配，不自动消费JEV预算、不发交易所订单。
- 日志不含凭据；数据与结果持久化；暂停/结束/风格变化不发布旧结果。

## Review Focus

- 同名BTCUSDT现货与合约的旧钱包隔离：合约configure/start/step拒绝Spot路径。
- 新上市合约不足7天：历史覆盖不足状态阻止模型，不补假K线。
- 多页重复、末端未收盘、时间倒退：严格范围/连续性校验。
- 后台重启与并发读取：原子首次任务claim；未完成尝试不自动再收费。
- 保存风格与模型响应交错：结果保留原风格并discard，不污染新版本。

## Task 1 — 会话身份和合约历史Provider

Files: domain/session_market.py、domain/sessions.py、application/sessions.py、adapters/binance_direct/futures_public.py、ports/session_analysis.py、tests/test_session_market.py。
Interfaces: `SessionAnalysisTarget(market,symbol,history_days,interval)`；`FuturesPublicClient.catalog()->tuple[PerpetualContract,...]`；`history(target)->HistoricalMarketData`。

- [x] RED：旧会话spot兼容、USDT永续目录全量过滤、ETH168根、30天分页、重复/缺口/未来/限流/字节上限拒绝。
- [x] 实现固定GET传输和已收盘窗口校验；会话create接收可选target；保持旧数据库body兼容。
- [x] GREEN：Conda运行 `tests/test_session_market.py`，保存验证日志。

## Task 2 — 首次分析任务与运行装配

Files: domain/session_analysis.py、application/session_analysis.py、adapters/sqlite/session_analysis.py、runtime/session_analysis.py、bootstrap.py；tests/test_initial_analysis.py。
Interfaces: `InitialAnalysisService.step()`、`public_view()`、`validate_target(target)`；`InitialAnalysisPort.analyze(request)->InitialAnalysisResult`；`SqliteInitialAnalysisStore.claim/finish/get`。

- [x] RED：一次claim、重复读取/重启不重复调用、历史缺失不调用、无模型显示明确状态、风格/会话变化discard。
- [x] 实现持久任务、100KB上下文上限、15秒模型截止、后台生命周期；独立于Flash/JEV旧worker。
- [x] GREEN：上述测试和会话/Paper/快照相关回归；记录真实Flash未配置缺口。

## Task 3 — Web选择与合约总览

Files: web/app.py、web/session_analysis_routes.py、web/templates/session.html、web/templates/overview.html、web/static/session.js、web/static/session-analysis.js、web/static/overview.js；tests/web/test_session_market_routes.py。
Interfaces: GET `/api/analysis/contracts`、GET `/api/analysis/current`；POST `/api/sessions`可选analysis_target。

- [x] RED：CSRF、未知/下市symbol拒绝、目录不可读、现有target不能style更新、页面元素与历史状态。
- [x] 实现动态搜索列表、历史选择、不可变显示与独立历史曲线；合约视图屏蔽现货数据。
- [x] GREEN：Web/API和JS语法/浏览器验证。

## Task 4 — 验证与接续

- [x] 公共真实catalog及ETH7天/另一个非BTC合约历史，零模型与订单请求；记录范围/数量/摘要。
- [x] 适当回归、Ruff、实际浏览器检查，更新 DEVELOPMENT_STATUS/IMPLEMENTATION_LEDGER。
- [x] 持续实例切换保持原数据库会话与预算；不重置资金，不续期失效授权。

真实Flash费用配置和合约Paper风险/执行为明确接续项，不能用Fake验证冒充已接通。

验收：最后全套1224项/145subtests通过，另22项公共响应边界补验、Ruff/Node通过。真实目录525个、ETH/SOL各168根；独立复核三处P2全部关闭。证据见 SESSION_MARKET_VERIFICATION.md；真实Flash未收费联调，合约Paper未装配，后续不续期旧JEV预算。
