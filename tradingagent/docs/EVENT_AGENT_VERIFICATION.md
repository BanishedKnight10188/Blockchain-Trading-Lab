# 事件 Agent MVP：验证记录

范围：2026-10-09 开始、2026-10-10（北京时间）完成，计划 Task 4–9 的离线/Paper 实现，接续已交付的 Watch Task 1–3。本轮开发及审查修复完成；完整测试仍有下述失败，真实模型尚未验收。

## 已执行证据

| 验证 | 实际结果 | 证据 |
| --- | --- | --- |
| Task 4 协议、收费预留和运行历史 | 14 passed，先观察缺模块失败 | output/verification/agent-task4-green-02 |
| Task 5 研究/Watch/事件/复核、循环限额 | 5 passed，先观察缺 ContextBuilder 失败 | output/verification/agent-task5-green-02 |
| Task 6 强制授权与类型证据 | 2 passed，先观察无证据/无授权错误放行 | output/verification/agent-task6-green |
| 受影响旧执行/JEV 兼容 | 54 passed | output/verification/agent-task7-green |
| Task 7 独立钱包/实际回执/暂停保护 | 3 passed | output/verification/agent-task7-green-02 |
| Task 8 装配、隔离和浏览器写操作 | 2 passed | output/verification/agent-task8-green |
| Task 9 部分成交恢复及受影响契约 | 14 passed，先观察未知开仓阻止可信保护减仓 | output/verification/agent-task9-green |
| 最大亏损和工具身份 | 6 passed，先观察 2 项失败 | output/verification/agent-limits-green |
| 已触发事实冲突阻止提交 | 8 passed，先观察错误成交 | output/verification/agent-conflict-green |
| 新 Agent 专项汇总（冲突回归之前） | 35 passed，1 条既有 FastAPI 警告 | output/verification/event-agent-mvp-final-02.log |
| 完整离线测试（最终审查之前） | 1767 passed、7 failed、193 subtests passed，1 条既有警告 | output/verification/event-agent-full-suite.log |
| Ruff（本次 Python 文件） | All checks passed | 文件范围见 output/verification/event-agent-files.json，另含 ports/watches.py 和 tools/start-event-agent-offline.py |
| 最终审查回归 | 14 passed，先复现 13 项失败及领取取消 1 项失败 | output/verification/agent-review-green-01.log |
| 预览只读边界 | 先复现预览触发强平，再在最终专项通过 | output/verification/agent-preview-pure-red.log → event-agent-final-05.log |
| 最终受影响专项（含全部审查修复） | 111 passed，1 条既有 FastAPI 警告 | output/verification/event-agent-final-05.log |
| 实际 JEV 与 Guardian 并行 | 慢 Agent 模型在等待中，实际 JEV Paper 成交、Guardian 平仓均完成；1 passed | output/verification/event-agent-real-jev-parallel.log |
| 最终完整离线测试 | 1782 passed、8 failed、193 subtests passed；1 条既有警告；494.77s | output/verification/event-agent-full-final-02.log |

测试使用现有 tradingagent Conda 环境，禁止外部 DNS/网络，全部数据库为唯一的 output/verification 子目录。隔离页面已在 localhost:8783 打开并人工检查：暂停、Fake WAIT、offline_replay、未配置资金/保护、未启用收费调用。用户现有服务未重载。

## 全量测试中的 7 项失败

1. Futures wire 时间测试：既有实现允许小幅未来时钟偏差，旧案例仍要求 1ms 偏差被拒绝。
2. 两个既有分层架构子案例：futures_trading 引用 config、jev_tasks 引用 Adapter/bootstrap。
3. 两个 archive bootstrap 案例：装配产生两个客户端，旧断言要求只有一个被关闭。
4. offline acceptance 汇总：报告前述既有架构问题。
5. JEV read-only 费用视图：期望保留的 reserved_usd 为 0.03，实际为 0。

原始失败标识：

    tests/adapters/test_futures_market.py::test_snapshot_rejects_invalid_or_stale_wire_facts[book-time-1791374400001]
    tests/architecture/test_dependency_boundaries.py::DependencyBoundariesTest::test_kernel_dependencies_follow_layer_direction (application/futures_trading.py)
    tests/architecture/test_dependency_boundaries.py::DependencyBoundariesTest::test_kernel_dependencies_follow_layer_direction (application/jev_tasks.py)
    tests/integration/test_archive_bootstrap.py::test_public_archive_is_separate_and_explicit_disable_is_respected[True]
    tests/integration/test_archive_bootstrap.py::test_public_archive_is_separate_and_explicit_disable_is_respected[False]
    tests/integration/test_offline_slice.py::test_acceptance_checks_actual_offline_assembly_and_records_remaining_gaps
    tests/test_jev_read_only.py::test_expired_read_only_assembly_preserves_shared_fee_and_policy

隔离目录 output/verification/event-agent-baseline-before 复制测试和源文件后，覆盖本轮编辑前捕获的 11 个既有生产文件，复现相同 7 个失败测试名（baseline-failures-03.log）。这不是整个工作区的时间点快照；工作区同时存在未纳入快照的 provider-managed 费用开发。为避免其接口缺字段遮蔽原失败，仅在隔离基线 RuntimeConfig 补入当前两个 provider-managed 配置字段，未补入 event_agent 字段，单独复测 JEV 案例，确认相同 reserved_usd 0 对 0.03 断言失败（baseline-budget-04.log）。上述差异均未通过修改用户的其他源码消除。

最终全量多出现一项 tests/web/test_jev_input_mode.py::test_switch_preserves_wallet_identity_and_is_durable：输入模式切换前后的 account.quote 不同。该测试未启用 event_agent；既有 OfflineFuturesMarket 的报价带实时采集时间，后台维护可以更新报价。当前代码单独重跑 1 passed（event-agent-jev-input-isolated.log），编辑前副本单独重跑也 1 passed（jev-input-before-check.log）。这是时间/调度敏感的推断，尚未稳定复现第 8 项失败，也没有用重跑通过覆盖全量失败记录；不声称全项目无回归。

## 能力边界

- 完整 Paper 下单路径由受控脚本模型验证；默认 Fake 模型返回 WAIT，不能用它评估策略质量。
- OpenRouter 仅做 Mock HTTP 协议测试；具体真实模型的工具能力、价格、预算、延迟和响应质量未验收。
- 本轮没有新增真实模型费用授权，没有真实交易所订单。
- Guardian 是进程内 Paper 保护，缺报价时 degraded；不提供交易所保护单或进程停止后的执行保障。
- 开仓尚未终结时，即使账户暂时被保护减仓至零，也持续查询并保护后续实际成交。资金费失败或超过 0.5 秒不挡住可执行止损；跨仓位变化的迟到历史结算仍需独立对账。
- schema v10 在线备份恢复已检查实际回执、钱包与保护；与 JEV 共用独立费用库时需另备份费用库。
- unknown 模型/订单不能自动重发；人工核对接口和步骤见 EVENT_AGENT_RUNBOOK.md。

原始日志和源码 SHA-256 保存于 output/verification；event-agent-final-sources.json 记录 48 个生产/页面源码文件，最终验证后已检查哈希未变。

## 最终代码审查修复

Fresh reviewer 确认 6 项 Important，没有 Critical 或实质 Minor。修复均有失败回归及通过证据：开仓前资金费/资金费失败挡住止损、部分成交保护过早结束、研究占用时事件租约被消耗、派发前取消遗留 RUNNING、错误工具参数终止 worker、JEV 调用错误占用 Agent 小时额度。作者同一修复轮还补上重复研究所有者、领取提交后取消，以及预览只读边界。

生产修复后按 output/verification/event-agent-files.json 所列测试文件执行受影响专项，111 passed；Ruff check 和 format --check 均通过（69 个 Python 文件）。最终完整测试使用独立目录 event-agent-full-final-02，1782 passed、8 failed。此前一次全量复测在发现预览问题后主动中断，不能用它声称通过。

保留决定及其代价见 docs/superpowers/implementation/2026-10-09-event-agent-progress.md 的 Ruling 行。代码与证据保留在现有工作区，没有整体暂存、提交、推送或合并。
