# 通用合约执行通道一期验收

2026-10-08，Asia/Shanghai。依据 [TRADING_CORE_ARCHITECTURE.md](TRADING_CORE_ARCHITECTURE.md) 与 [TRADING_EXECUTION_IMPLEMENTATION.md](TRADING_EXECUTION_IMPLEMENTATION.md)，TG1–TG4执行基础范围完成。正式Conda，无依赖安装或升级。

## 已实现

- 中立 `FuturesQuote/FuturesFunding` 和数值契约，公共Provider引用它们；旧Paper名字保留兼容别名。
- 严格的执行scope、命令、回执、账户快照与Ports，无live环境值；支持pending/accepted/partial/filled/rejected/canceled/unknown。
- `PaperFuturesBackend` 包装现有模拟引擎/SQLite，保留资金计算与事务内权限守卫；按完整命令查询持久操作，不受最近50条限制。
- `TradeExecutionService` 与具体后端解耦，显式固定行情来源，首次接收和提交前校验；同一服务驱动Paper或延迟Fake执行。
- 独立SQLite命令占位/CAS/审计，提交前保存pending，未确定结果阻止同账户新命令。提交后超时、取消或写回失败，恢复只查询，不自动重提。
- 回执区分 `observed_at` 本次观察时间与 `backend_at` 后端事件时间；有成交必须有后端时刻，回退事件仍拒绝。执行前清算返回请求rejected/account_liquidated，清算资金事实单独保留且可查询。

## 测试先行与独立复核

| 阶段 | 实际RED | GREEN/证据 |
| --- | --- | --- |
| TG1 | 32缺模块失败 | 164 passed/118subtests，`trading-execution-tg1-green-unsandboxed-20261008.txt` |
| TG2 | 20缺模块失败 | 112 passed，6.67秒，`trading-execution-tg2-green-20261008.txt` |
| TG3 | 19缺模块失败 | 78 passed/119subtests，8.16秒，`trading-execution-tg3-green-20261008.txt` |
| 独立review修复 | 正式6 failed/39 passed（含新增事件水位） | 227 passed/119subtests，12.30秒，`trading-execution-review-green-20261008.txt` |
| 无账户quote也固定来源 | 1 failed（实际accepted） | 最终专项228 passed/119subtests，12.93秒，`trading-execution-final-targets-20261008.txt` |

一次独立只读review发现TG-R1/P1未知恢复时间、TG-R2/P1清算伪装成交、TG-R3/P2命令生命周期、TG-R4/P2首次未来报价、TG-R5/P2混源。报告 [review-report.md](../output/verification/review-execution-20261008-a/review-report.md) 是修复前裁定，探针5 failed/23 passed。主Agent逐项补正式RED/GREEN并完成全量验证；没有再次派遣review或宣称修复后得到第二次独立批准。

首次全量1443/154只是修复前中间证据。修复后最终全量为 **1450 passed，154 subtests passed，136.45秒，exit0**，唯一提示是既有Starlette/httpx TestClient弃用。日志 [trading-execution-final-suite-20261008.txt](../output/verification/trading-execution-final-suite-20261008.txt)。Ruff check和format `agent_platform tests tools` 通过，**308 Python文件**；不将生成output脚本或不可访问旧缓存纳入产品检查。

## 独立数据库运行证明

[最终证明报告](../output/verification/trading-execution-proof-final-report-20261008.json)，[运行日志](../output/verification/trading-execution-proof-after-review-20261008.txt)。使用产品通用服务、中立命令、真实SQLite及Paper后端；行情是明确标记的offline_replay，虚拟设置只用于验证。

1000 USDT、5倍、5bps手续费：ETH在2000开多1，费用1，可用599/保证金400；在2100减仓0.4，已实现盈亏40、费用0.42；剩余数量0.6，可用798.58/保证金240、浮盈60、权益1098.58。重开保留同一资金及命令终态，钱包暂停；两个命令共6次持久状态/审计更新。没有重复成交。

原始证明与首次失败日志保留；首份证明脚本漏填必需jev设置，修正后通过，属于验证脚本问题而非产品RED。验证数据库各自独立，无用户数据库、账户网络、收费模型或交易所订单操作；费用0。

## 边界与接续

本阶段完成通用执行基础，不等于持续JEV合约操盘已经在用户页面运行。仍需共用运行风控/JEV候选、持续行情及后端维护（模拟资金费游标/迟到/同刻纪律、清算任务）、进程锁/恢复调度、Web装配、有界真实Paper和24h验收。Testnet/Agent OS只有预留身份和接口，尚无网络后端；真实资金执行没有启用。

Paper模拟存储保留旧操作读取，旧记录缺完整TradeCommand时不能凭空绑定为新的通用命令。后续Testnet须提供可信账户/成交/事件时间与权限映射；本地固定模拟维持率不得覆盖交易所资金事实。未开启新轮收费、安装包、用户服务重启、wheel、提交、推送或部署。
