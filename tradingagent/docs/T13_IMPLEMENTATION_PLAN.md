# T13版本化复盘与持久回访实施计划

> 使用superpowers:executing-plans逐段执行。用户授权持续自主推进；不额外等待重复审批，不提交或推送。

**Goal:** 用不可变真实成交、明确成本缺口和追加复盘版本支持复访，重启不重复任务。
**Architecture:** 确定性FIFO先计算事实；SQLite原子保存冻结分组、复盘版本和回访任务。解释与事实分离，后见信息只进入新版本。
**Tech Stack:** 正式Conda Python3.12、Pydantic2、SQLite WAL、pytest；不安装包。
**Spec:** DEVELOPMENT_PLAN.md T13、docs/PRODUCT_SPEC.md第5/8/10节。

## 约束与审查重点

- Spot/BTCUSDT/live命名空间；USER_REPORTED未核实事实不参与真实FIFO。
- 金额限制128位/指数±128，计算使用独立Decimal Context；极端精度/环境不能改变成本事实。
- TradeBatch.history_complete仅描述成交历史；还需明确期初库存与资金移动覆盖才可声称完整FIFO成本。存入BTC或无法对账的余额保持UNKNOWN/PARTIAL。
- 实际quote_quantity优先且缺失明确；BTC手续费减少买入净库存/增加卖出消耗，USDT手续费计入成本/扣除收入，非USDT/BTC费用无明确估值时保持未知。
- 原始ReviewRevision美元PnL缺少USDT/USD汇率证据时保持null；FIFO另存USDT计价事实，不假定USDT=USD。
- 同时间不同成交无确定顺序、跨scope、截止后数据、重复身份、数量/手续费矛盾必须拒绝或显示缺口；不按时间猜归属。
- 复盘/任务事实与审计同事务；event-only、CAS竞争、重启重试不能伪成功。任务键(group,kind,due_at)唯一。
- 默认免费规则复盘、model_participated=false；收费供应商未配置不调用模型，预算不足仍有规则说明。1h/24h显式选择，复盘不自动改风格。
- 原建议核对冻结实际方向/期限、原strength/version/usage与手续费；没有完整订单或执行时账户证据时保留不可评估。事后行情使用已有T11提交的publication snapshot及完整request/completion审计证明，最多一帧、截止前已知、严格晚于原分组截止；缺失为null，之后新增证据不覆盖旧版本。

## 第一段：FIFO事实计算

**Files:** 新建domain/review_facts.py、application/trade_groups.py、tests/application/test_trade_groups.py。
**Interfaces:** InventoryCoverage保存明确期初量/资金移动覆盖和期末对账量；FifoAnalyzer.analyze(batch: TradeBatch, cutoff: datetime, coverage: InventoryCoverage | None = None) -> FifoResult。

- [x] 写部分买卖FIFO、BTC/USDT手续费、残余成本守恒与负盈亏测试。
- [x] 写非报价手续费、quote_quantity缺失、历史/资金移动不完整、存入BTC/余额不符无虚构PnL测试。
- [x] 写确定成交顺序、跨scope、截止时间、浮点/极端金额/ambient Decimal压力测试。
- [x] 正式Python运行tests/application/test_trade_groups.py观察实际缺失RED，再最小实现、验证、独立复核。

## 第二段：冻结分组与复盘版本

**Files:** 新建ports/reviews.py、application/reviews.py、adapters/sqlite/reviews.py、tests/application/test_reviews.py；修改domain/events.py与统一SqliteStore装配。
**Interfaces:** ReviewStorePort注册已导入完整身份分组、原子追加ReviewRevision及FifoResult；ReviewService.generate(group_id, cutoff, kind)返回版本。

- [x] 写已导入原文匹配、未归属分开标识、版本链/截止/父版本/事实冻结测试。
- [x] 写规则复盘不调用模型、初始与回访共存、截止后原事实不覆盖、同键精确重试及不同内容冲突测试。
- [x] 写原子审计失败回滚、两个Store并发及重启/event-only伪事实测试。
- [x] 观察缺失RED→实现→定点验证→独立复核；有界查询供T14，不导出敏感scope。

## 第三段：持久回访与装配

**Files:** 新建runtime/review_jobs.py、tests/runtime/test_review_jobs.py；修改bootstrap及复盘Store。
**Interfaces:** ReviewJobService.schedule(group_id, due_at, kind) -> ReviewJob；run_due有界轮询持久键与复盘原子完成，免费规则模式无付费重派风险。

- [x] 写同group/kind/due_at重复schedule、可选1h/24h、重启待办、取消/失败、只产生一个版本测试。
- [x] 写竞争执行、处理中断/审计失败、数据截止绑定scheduled_for，不用后来数据伪装当时结果测试。
- [x] 观察RED→实现→验证→独立复核；完整pytest/Ruff、更新文档/wheel后才标T13离线完成。

## 暂未验收

真实账户历史/资金移动与汇率证据供应、收费复盘供应商、小额真实模型联调独立记录；Fake不能标真实验收。T14表单/T15恢复/T16实际24h soak继续。

