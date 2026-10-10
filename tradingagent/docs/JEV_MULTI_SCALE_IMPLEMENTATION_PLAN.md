# JEV 六层输入实施计划

**Spec:** JEV_MULTI_SCALE_INPUT_PLAN.md。用户已授权直接完成前三项。

**范围：** 四层背景LLM、20根3分钟线＋60根1秒线、原生K线WS。使用既有Conda与依赖，在指定目录开发，不提交、推送或重置钱包/费用；只做关键回归。旧会话保持旧输入，新输入显式选择。

- [x] T1 数据领域与原生WS：domain/multiscale.py定义六层窗口/收盘/数量/时间/hash；adapters/binance_direct/multiscale.py实现REST预加载、一个K线订阅连接、1s连续合约与长周期更新、缺口/重连失效。关键测试：80根数量、未收盘/不同币种拒绝、断线后秒线重新准备、REST不请求1s。
- [x] T2 背景整理：domain/background.py定义有绑定身份的结构化摘要；application/background.py独立低频整理；adapters/openrouter/background.py实现明确模型/价格/共享累计预算/未知预留；adapters/sqlite/background.py保存完整整理请求/结果，Fake适配器仅供离线。关键测试：同身份缓存、过期/换币隔离、刷新不阻塞、失败保留旧版本、已确认/未知费用。
- [x] T3 JEV装配：bootstrap_futures.py、application/futures_trading.py接收完整多尺度快照；新请求包含缓存摘要和紧凑80根，仍存入既有逐笔档案。config.py及任务向导显式选择，不改旧创建身份/消费；显示准备缺项。关键测试：未准备零调用、完整真实输入档案、旧路径回归。
- [x] T4 有界实测与交付：用户累计授权追加至1 USD且保留旧费用。一次Flash默认推理耗尽2048输出，收费如实保留；关闭额外推理后四层摘要成功（32431/721token、6243ms），JEV真实收到缓存＋80根短线（14976/310token、模型891ms/整轮1179ms），WAIT0.93、usage confirmed、accepted=true。完整背景与周期记录保留，原5钱包/231费及用户文件重载前后hash一致，8776已加载。证据multiscale-workflow-1usd-20261009-c.json。仅证明一次真实联合工作流；自主成交/退出、P50/P95、长稳仍是后续验收，不强行制造成交。

**接口约定：** CandleWindow(symbol, interval, source, captured_at, requested_count, candles)；KlineBuffer.accept(window)/window(interval, count, at)；BackgroundRequest/BackgroundResult；MultiScaleContext.snapshot(session)与status，后台刷新独立于JEV请求。

**Ruling：** 用户指定现有开发目录且要求减少测试/自主推进；原地开发、无需重复审批，不安装依赖、不提交、不全套重复测试。使用原IMPLEMENTATION_LEDGER.md作为连续记录，不创建额外计划工作区。15分钟背景刷新为可配置默认；费用配置有效性与市场背景的新鲜度分别处理。

