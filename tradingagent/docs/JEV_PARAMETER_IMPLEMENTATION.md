# JEV 参数决策实现计划

> For agentic workers: 使用 superpowers:executing-plans 在本会话原地执行；用户已授权自主推进与减少测试，不创建Agent、工作树或提交。

**Goal:** JEV选择带比例、数量、杠杆的完整合约方案，Paper通过通用执行口完成操作。
**Architecture:** 保留v1；v2候选生成是纯函数，模型只选ID，plan进cycle，杠杆进入通用命令与Paper原子账本。
**Tech Stack:** Python 3.12 / Pydantic / SQLite / FastAPI / 原有JS；现有tradingagent Conda。
**Spec:** [JEV_PARAMETER_DECISIONS.md](JEV_PARAMETER_DECISIONS.md)

## Global constraints

- Python `C:/Users/exile/anaconda3/envs/tradingagent/python.exe`；不安装依赖、不收费、不改旧试验上限/会话。
- 开仓按净权益保证金占比；加减仓按当前合约数量。
- 旧模式与旧命令指纹兼容；杠杆、资金、仓位、版本和行情守卫不得绕过。

## Review focus

- 等待不得调整杠杆；低置信度不得交易。
- 增加/释放逐仓保证金不创造资金，不清除资金费。
- 减仓20%不得全平；整数步长取整不得增大数量。
- 杠杆和候选越限在派发前拒绝；执行前行情/账户仍需复查。
- 同一命令不同杠杆必须冲突；旧无杠杆命令查询保持兼容。

## Task 1: 参数领域、候选与Paper杠杆

Files: `domain/trading_runtime.py`, `domain/trading_execution.py`, new `domain/trading_plans.py`, `domain/futures_paper.py`, `domain/futures_paper_engine.py`, `adapters/paper/futures.py`, `adapters/sqlite/futures_paper.py`, `domain/decision_models.py`。

Interfaces: `build_plans(run, account, quote) -> tuple[FuturesTradePlan, ...]`；TradeCommand.target_leverage；TradingLimits/FuturesPaperSettings.max_leverage；账户快照leverage；v1字段默认不变。

- [x] 写定向测试，确认纯内核1项实际RED；异步首次沙箱阻塞不计RED，装配另有2项实际失败。
- [x] 实现候选、有效比例、硬杠杆上限、原子保证金调整、旧命令指纹兼容。
- [x] 核对开仓20%/10倍、加减20%、杠杆越限与现金守恒。

## Task 2: JEV周期与Web

Files: `application/futures_trading.py`, `adapters/fake/futures_trading.py`, `web/templates/jev-trader.html`, `web/static/futures-trader.js`。

Interfaces: v2问题集`futures-plan-v2`，candidate ID映射到保存的plan；同一次请求、同一版本，只有通过风控的候选。新的TradingCycle.plan由公开API返回。

- [x] 离线实际SQLite多空周期验证开仓→加仓20%→减仓20%→全平，以及重启账本保持。
- [x] 页面暴露模式/比例/杠杆候选和实际方案，旧钱包不修改；离线HTTP200，旧服务未重启。
- [x] 定向回归、Ruff/JS语法；相关121项、最后参数/资金52项通过，不跑完整套件。

## Task 3: 接续记录

- [x] 更新DEVELOPMENT_STATUS / IMPLEMENTATION_LEDGER / REMAINING_WORK与本计划勾选。
- [x] 如实区分已实现的离线参数执行和仍待真实JEV联调/退出保护/Testnet。
