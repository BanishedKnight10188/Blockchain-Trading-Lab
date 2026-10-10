# 通用合约执行通道一期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付与Paper类型解耦的合约交易事实、执行/账户接口、Paper后端及持久订单执行通道，为JEV共用主体提供基础。

**Architecture:** 公共行情引用中立FuturesQuote/FuturesFunding；旧Paper类型保留兼容别名。应用层通过Ports持久命令、查询账户和执行后端，超时先查询不重提；Paper Adapter复用模拟引擎/SQLite，不复制计算。此计划只实现架构规格的执行基础；持续资金费后台、JEV候选、Web、Testnet仍是后续交付。

**Tech Stack:** 现有Conda Python3.12、Pydantic2、SQLite、asyncio、pytest、Ruff；无新依赖。

**Spec:** [TRADING_CORE_ARCHITECTURE.md](TRADING_CORE_ARCHITECTURE.md)。

## Global Constraints

- 指定开发根目录原地工作，不提交、推送、部署或改用户数据库/服务；既有自主授权覆盖默认重复审批/提交/工作树步骤。
- 正式Python为C:/Users/exile/anaconda3/envs/tradingagent/python.exe；不安装或升级依赖。
- 单个USDT永续、单向逐仓；保持金额/来源/时间/风格/操盘硬纪律。Paper与Testnet命名空间明确，接口不开放live值。
- Domain/Application/Ports不依赖具体HTTP/MCP/SQLite/Paper Adapter；原兼容入口和模拟计算专项必须继续通过。
- 新契约重验实例及嵌套字段，拒绝model_copy绕过；命令ID最多128字符，expires_at在created_at后且最多30秒。
- 无收费、账户或交易所订单调用；现有预算不续期。离线SQLite使用独立验证数据库。

## Review Focus

- 相同命令ID不同内容：不可复用旧成交；新命令不得越过同账户未确定订单。
- 提交后超时、取消或写回失败：持久事实仍阻止重复提交，重启只查询。
- 部分成交、乱序回执和未知结果：数量/费用不回退，终态不可变，不能假定立即全部成交。
- 账户/会话/style/trader版本或市场改变：执行前拒绝，Paper事务内再次校验；不混入其他账户资金。
- 恶意实例、过期/未来行情、来源混合：在通用和后端边界严格拒绝，不因重新包装而洗掉证据。

## Task TG1：中立事实和执行契约

**Files:** Create domain/futures_values.py、domain/trading_execution.py、ports/trading_execution.py；Modify domain/futures_paper.py、domain/futures_market.py、adapters/binance_direct/futures_market.py；Test tests/domain/test_trading_execution.py。

**Interfaces:** FuturesQuote/FuturesFunding及Amount/SignedAmount/Price/Quantity/Source（旧Paper保留别名）。ExecutionScope(environment:paper|testnet,account_ref,session_id,symbol)；TradeCommand(command_id,scope,action,quantity,created_at,expires_at,expected_account_revision,style_revision,trader_revision)；ExecutionReceipt(command,status,filled_quantity,average_price,fee_usdt,backend_order_id,backend_at?,observed_at,reason)；TradingAccountSnapshot(scope,revision,status,free_usdt,margin_usdt,quantity,side,entry_notional,realized_pnl_usdt,funding_usdt,fees_usdt,equity_usdt?,unrealized_pnl_usdt?,quote?,captured_at)。ExecutionRecord(command,receipt,revision,quote?)。

Ports: FuturesExecutionPort.submit(command,quote,at)->ExecutionReceipt、lookup(command,at)->ExecutionReceipt|None；FuturesTradingAccountPort.account(scope,at)->TradingAccountSnapshot；ExecutionJournalPort.reserve(command,at)->tuple[ExecutionRecord,bool]、get(command_id)->ExecutionRecord、save(receipt,expected_revision,quote=None)->ExecutionRecord。

- [x] 写中立报价/资金费与旧兼容、命令时效/环境/命名空间、成交状态一致性、嵌套重验和账户方向/估值一致测试。
- [x] 运行tests/domain/test_trading_execution.py，Expected缺模块RED，保留日志。
- [x] 最小实现上述契约、严格字段与兼容提取；不改变模拟账本算法。
- [x] 运行新领域、futures_market、futures_paper和架构测试，Expected全通过。

## Task TG2：Paper执行及账户后端

**Files:** Create adapters/paper/futures.py；Modify ports/futures_paper.py、adapters/sqlite/futures_paper.py新增按ID查询操作；Test tests/adapters/test_futures_execution_backend.py。

**Interfaces:** PaperFuturesBackend(store, market_source=显式来源)实现TG1执行/账户Ports。lookup按持久操作ID查询而非最近50条；校验scope/order/revision和原style/trader指纹，返回原成交事实。submit先lookup，未成交才验证命令期限/来源/报价并调用store.execute；原子权限/资金守卫保留。账户没有可用quote时equity/upnl为None，其他资金事实仍可读。

- [x] 写ETH多空/减仓手算费用与资金、重启幂等/不同输入拒绝、过期/源/版本拒绝、无行情不伪造估值、超过50条仍可查询测试。
- [x] 运行后端专项，Expected缺模块RED。
- [x] 实现转换和精确查询，无重复资金计算；明确拒绝非Paper目标。
- [x] 运行后端、新领域与原模拟存储/引擎，Expected全通过。

## Task TG3：持久通用订单通道

**Files:** Create adapters/sqlite/trading_execution.py、application/trading_execution.py；Test tests/application/test_trading_execution.py。

**Interfaces:** SqliteExecutionJournal(path).initialize/reserve/get/save；TradeExecutionService(journal,execution,accounts,market,clock,market_source=显式来源).submit(command)->ExecutionRecord、reconcile(command_id)->ExecutionRecord。调用前持久pending；新请求重新取行情/账户，检查币种/时效/账户running及revision；已存在请求只查询，不重新提交。非终态阻止同账户新命令。SQLite BEGIN IMMEDIATE/CAS与不可变原命令保护多实例。

- [x] 写真实SQLite+Paper完整循环与替换Fake后端accepted→partial→filled；超时/取消/重开查询、不重提、查询None保持unknown、冲突/账户版本/陈旧报价拒绝；双实例claim和失败写入回滚测试。
- [x] 运行通道专项，Expected缺模块RED。
- [x] 实现持久事实与通用编排；前置拒绝返回rejected，已尝试提交的异常返回unknown；取消保留未知后传播。未知只经查询恢复，不自动重试。
- [x] 运行全部新专项与架构，Expected全通过；更新ledger。

## Task TG4：整体复核与验证

- [x] 依executing-plans/requesting-code-review进行一次全范围独立只读复核（技能明确要求）；真实缺陷补RED/GREEN，不重复派遣相同diff。
- [x] 正式Conda完整pytest、Ruff check/format；Expected exit0，记录实际计数与已有提示。
- [x] 独立数据库输出真实Paper手算运行证明，模型/交易所调用0；README哈希/文档链接检查。
- [x] 写验收与接续状态，明确执行基础完成不等于JEV持续操盘；下一持续后台/维护与独立决策装配。

Pre-flight: TG1产生的scope/command/receipt/account供TG2转换和TG3持久；TG2持久查询供TG3恢复，故不能只查recent。TG3 journal不与Paper账本宣称跨事务原子；先写pending、恢复查询已有成交消除提交后写回失败窗口。无跨任务类型冲突。

TG4复核修订：完整TradeCommand保存于Paper操作；backend_at与observed_at分离且真实事件水位不回退；清算拒绝请求但可查询；首次未来校验与服务来源固定。实际RED/GREEN和最终1450/154证据见TRADING_EXECUTION_VERIFICATION.md。
