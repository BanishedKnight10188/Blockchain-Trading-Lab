# USDT Futures Paper Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline, task-by-task with TDD. 用户已授权在原目录自主持续开发；依赖由用户管理，不提交、不新建工作树。本计划仅内核与持久，不包含模型/行情/Web装配；接续集成另有规格。

**Goal:** 交付可以确定性计算并原子保存 USDT 单向逐仓模拟的离线内核。
**Architecture:** 独立领域 DTO 和纯执行函数，SQLite Port 在事务中调用同一函数并验证现有会话/操盘控制。Spot 领域与表不变。
**Tech Stack:** 正式 Conda Python3.12、Pydantic2、Decimal、既有SQLite IO策略、pytest。无新依赖。
**Spec:** [FUTURES_PAPER_SPEC.md](FUTURES_PAPER_SPEC.md)。

## Global Constraints

- 身份 `paper:futures:<session_id>`、USDT永续、单向逐仓，初始暂停，杠杆整数1–20。
- 显式初始资金/持仓与亏损上限/手续费滑点/固定模拟维持率；风格不能提高硬限额。
- 5秒mark/book，未来/币种冲突拒绝；资金费按已结算时刻，不硬编码8h。
- 80位上下文，现金1e−12、入场成本1e−24；资金/持仓不得伪造，亏损不动自由现金。
- 仅本地模拟，收费/订单关闭、到期费用不续期。

## Review Focus

- SHORT买回、部分平仓成本分配与手续费：FP1验证数值与资金守恒。
- 低精度Decimal上下文、指数炸弹和model_copy绕过：FP1重新验证与边界测试。
- 跳空强平/资金费损耗不能扣自由现金：FP1独立缺口与冻结测试。
- 同ID重试/并发CAS/事务失败：FP2原子记录与真实SQLite恢复测试。
- 关闭/风格或trader变更/Spot错身份：FP2事务内重验，旧请求不能执行。

## FP1：领域和纯执行内核

**Files:** `agent_platform/domain/futures_paper.py`、`agent_platform/domain/futures_paper_engine.py`；`tests/domain/test_futures_paper.py`。
**Interfaces:** FuturesPaperSettings/Rules/State/Quote/Order/Funding/Transition/Valuation；create_account(session_id,symbol,settings,rules,at)、value(state,quote,at)、execute(state,order,quote,at)、settle_funding(state,funding,at)。返回不可变Transition含新状态及操作明细。

- [x] 写测试：1000USDT、ETH2000、5倍、5bps，开1ETH后可用599、保证金400；0.4在2100平仓后可用798.58/保证金240/已实现40；全部平仓后1097.95。SHORT镜像、同向成本、翻转/超量拒绝。
- [x] 验证缺模块RED；添加实现，运行该文件GREEN。
- [x] 增加过期/未来/错误币种、杠杆/资金输入、风险上限、跳空隔离损失、正负资金费和重复/乱序RED→GREEN。
- [x] Ruff和原领域/架构回归；记录实际命令和结果。

## FP2：原子持久与当前控制

**Files:** `agent_platform/ports/futures_paper.py`、`agent_platform/adapters/sqlite/futures_paper.py`；`tests/adapters/test_futures_paper_store.py`。
**Interfaces:** SqliteFuturesPaperStore(existing database path). initialize/create/get/start/pause/execute/funding/mark/recover/recent；命令ID、wallet revision、style revision、trader revision显式提供。execute/funding复用FP1；mark独立记录安全行情水位，非清算观察不改变资金版本；不接受调用方任意替换资金状态。

- [x] 真实 SQLite 测试创建/暂停启动/原子执行；相同ID同参数重试返回原操作，不重复扣款；不同内容同ID冲突。
- [x] 实际缺模块RED，实现独立表和事务后GREEN；并发两个请求同版本只有一个成功。
- [x] 增加Spot/已关闭/旧style/trader/禁用/错误环境/重启暂停/历史钱包保留；失败不留下钱包变动或操作。RED→GREEN。
- [x] 原Spot持久/会话/架构回归，Ruff，无私有账户或网络。

## FP3：离线验收和接续

**Files:** `docs/FUTURES_PAPER_KERNEL_VERIFICATION.md`、DEVELOPMENT_STATUS、IMPLEMENTATION_LEDGER。
- [x] 真实 SQLite Fake行情驱动开多→资金费→部分平仓→重启，保存不含Key的证据；无收费/真实订单。
- [x] 当前全套测试、格式/架构检查；独立只读复核，必要修复有RED→GREEN。
- [x] 状态标明内核完成和实时行情/JEV/Web未装配；不把旧wheel、目录历史或UI截图称模拟成交验收。

2026-10-07 FP1–FP3离线范围完成。专项60 passed，完整1289 passed / 148 subtests（251.15秒）；Ruff与295文件format通过，最终独立复核无剩余P1/P2。完整证据 [FUTURES_PAPER_KERNEL_VERIFICATION.md](FUTURES_PAPER_KERNEL_VERIFICATION.md)。接续实时行情/JEV/Web集成，不把本计划关闭视为整个项目完成。
