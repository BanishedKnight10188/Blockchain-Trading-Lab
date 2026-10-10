# Event-driven Trading Agent MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. 用户已授权继续开发；Task 1–9 离线/Paper 实现与最终复测完成；全量仍有 8 项失败，详见验证报告。真实收费模型与真实资金未启用。

**Goal:** 在现有平台增加与 JEV 并列的事件驱动 Agent，完成研究、Watch、唤醒、二次判断、独立 Paper 交易及复盘。

**Architecture:** 延续 Domain/Application/Ports/Adapters。独立 Watch 与 Agent worker 复用行情、费用账本和 Paper；扩展决策证据，在持久执行通道加入公共授权，独立 Guardian 维护新板块仓位。

**Tech Stack:** 现有 Python 3.12/asyncio/Pydantic 2/SQLite/FastAPI/Jinja2/pytest/ruff；不增加框架依赖。

**Spec:** [架构分析与 MVP 规格](../specs/2026-10-09-event-driven-agent-design.md)。

## Global Constraints

- 主项目为 Blockchain-Trading-Lab/tradingagent。
- 两个板块分别启停，使用独立调度槽与 HTTP 并发限额。
- MVP 使用不同 session_id，延续 paper:futures:<session_id>，不修改旧钱包身份。
- 既有 JEV 费用授权不自动授权新 Agent。
- 不修改原 JEV 调度、退出策略、钱包与费用事实；旧证据保持可读。
- Domain/Application 不导入具体 Binance/OpenRouter/SQLite；Ports 使用自身领域类型。
- Watch v1：1m/5m、已收盘、最多 20 个有效 Watch/lane、单次触发、最长 24h、事件 TTL 120s。
- AgentRun：1 个在途/lane、最多 4 次模型请求/8 次工具调用、单请求最多 2048 输出 token、上下文最多 32 KiB、整轮最多 90s。
- 开发默认 Fake 模型与隔离 SQLite；不安装依赖、不启动用户既有服务、不产生收费。

## Review Focus

- 触发提交后、消费前崩溃：事件仍可领取，Watch 不再生成第二个触发。
- 模型已发送但响应/收费未知：租约到期不能重复收费或丢弃预留。
- 用户暂停、改风格/规则或取消假设时模型仍在运行：旧结果归档，不能增加风险。
- 部分成交后 Agent 停止：Guardian 从持久事实恢复已成交仓位保护。
- Event Agent 与 JEV 同时活动：账户、开关、费用子限额和 evidence 不串用。

## 文件与任务组织

新增代码仍位于 agent_platform；每个任务给出准确创建/修改文件。大型用例按业务拆开，不复制另一套 FuturesTradingService。新事件存储初版独立类型/表，不为接入而扩张所有旧 Journal payload。

测试在现有 tradingagent Conda 环境、项目目录执行。先证明专项失败，再实现并通过；每个任务结束检查 diff、记录结果，只提交该任务文件。当前有大量既有未跟踪文件，不能整体 git add。

验证命令使用 python -m pytest。收口只跑新 MVP、受影响 JEV/执行/架构兼容检查，避免反复重跑未受影响全套。真实网络/模型检查单独有界执行。

## Task 1：Watch 契约与纯规则

**Files**

- Create: agent_platform/domain/watches.py、watch_rules.py、watch_features.py。
- Test: tests/domain/test_watch_rules.py、test_watch_features.py；fixtures 在 tests/fixtures/watch_cases.py。

**Interfaces**

- watches.py 定义 WatchDefinition/WatchRecord/WatchFrame/WatchEvaluation，字段按规格第 6 节；WatchInterval=Literal["1m", "5m"]。
- WatchEvaluation 包含 next_state、emit_trigger、reason、evaluated_values、input_hash。
- evaluate_watch(watch: WatchRecord, frame: WatchFrame, now: datetime) -> WatchEvaluation。
- evaluate_conditions(conditions: ConditionGroup, frame: WatchFrame) -> ConditionEvaluation；两个类型也在 watches.py 定义，三值结果 true/false/unavailable，供执行前单独复验失效条件。
- calculate_watch_features(candles: tuple[Candle, ...], interval: WatchInterval) -> WatchFeatures。
- WatchFeatures 包含 volume_ratio_20/ema_12/ema_26/atr_14、availability、algorithm_version；EMA 需对应 period 根、ATR 需 14 根、量比需 21 根连续 bar。只要求规则实际引用的指标可用。

- [x] 先写 test_volume_baseline_excludes_current：当前 volume=150、此前 20 根各 100，未来数据改变不影响当前结果。

~~~python
assert features.volume_ratio_20 == Decimal("1.5")
assert features == features_after_future_data_changes
~~~

- [x] 写 test_invalidation_precedes_trigger、test_no_unclosed_or_gapped_trigger、test_same_candle_semantics、test_unknown_metric_and_nan_rejected。断言同时满足时 INVALIDATED/emit_trigger=False；恰好 expires_at 为 EXPIRED；非法跨根配置拒绝，分母零 unavailable。
- [x] Run: python -m pytest tests/domain/test_watch_rules.py tests/domain/test_watch_features.py -q。确认实现前失败。
- [x] 实现契约与纯函数；沿用现有 EMA/ATR 算法，新增周期适配而不改旧 FeatureService；重复上述命令通过。

**Deliverable:** 无模型/网络/数据库也能判定规则；1m/5m 有一致语义。

## Task 2：原子 Watch 与持久事件领取

**Files**

- Create: domain/agent_events.py、ports/watches.py、ports/agent_events.py、adapters/sqlite/watches.py、adapters/sqlite/agent_events.py（均在 agent_platform 下）。
- Modify: agent_platform/adapters/sqlite/schema.py、migrations.py、backup.py。
- Test: tests/adapters/test_watch_store.py、test_agent_event_delivery.py。

**Interfaces**

- agent_events.py 定义 WatchEvent/EventDelivery/EventLease；EventLease 包含 event、lease_token、lease_until。
- WatchStorePort.create(definition: WatchDefinition) -> WatchRecord。
- WatchStorePort.replace(definition: WatchDefinition, expected_revision: int) -> WatchRecord。
- WatchStorePort.cancel(watch_id: str, expected_revision: int, at: datetime) -> WatchRecord。
- WatchStorePort.list_active(lane_id: str) -> tuple[WatchRecord, ...]。
- WatchStorePort.get(watch_id: str) -> WatchRecord。
- WatchStorePort.commit_evaluation(watch_id: str, expected_revision: int, frame: WatchFrame, result: WatchEvaluation) -> WatchRecord。原子更新游标/状态并派生唯一事件/outbox。
- AgentEventStorePort.claim(lane_id: str, now: datetime, lease_seconds: int) -> EventLease | None。
- AgentEventStorePort.renew(lease: EventLease, now: datetime, lease_seconds: int) -> EventLease。
- AgentEventStorePort.complete(lease: EventLease, run_id: str, now: datetime) -> None。
- AgentEventStorePort.fail(lease: EventLease, reason: str, retryable: bool, now: datetime) -> EventDelivery。
- AgentEventStorePort.get(event_id: str) -> WatchEvent。
- AgentEventStorePort.recover(now: datetime) -> tuple[EventDelivery, ...]。

- [x] 写 test_trigger_commit_and_outbox_are_atomic：注入中断必须全回滚；提交后重开库，事件仍能领取。

~~~python
assert rolled_back_watch == original_watch
assert rolled_back_outbox_count == 0
assert reopened_claim.event.event_id == committed_event_id
~~~

- [x] 写 test_duplicate_candle_one_event、test_stale_lease_cannot_ack、test_watch_revision_and_capacity。断言两次同根仅一个事件；旧 token 完成拒绝；第 21 个 Watch 拒绝；同 key 不同 hash 冲突；过期事件不调用模型。
- [x] Run: python -m pytest tests/adapters/test_watch_store.py tests/adapters/test_agent_event_delivery.py -q。先失败。
- [x] 实现短事务与 CAS/租约，网络不占写锁；通过上述检查，并补受影响迁移/备份测试。备份恢复覆盖新表，旧账目保留。

**Deliverable:** Journal 事实与消费状态分开；领取恢复不丢触发。

## Task 3：Watch 数据、持续监控与回放

**Files**

- Create: agent_platform/ports/watch_data.py、adapters/binance_direct/watch_data.py、application/watches.py、runtime/watches.py、replay/watches.py。
- Test: tests/runtime/test_watch_runtime.py、tests/replay/test_watch_replay.py、tests/adapters/test_watch_data.py。

**Interfaces**

- WatchDataPort.stream(symbol: str, interval: WatchInterval, after: str | None) -> AsyncIterator[WatchFrame]。
- WatchDataPort.latest(symbol: str, interval: WatchInterval) -> WatchFrame。
- WatchService.process(frame: WatchFrame, now: datetime) -> tuple[WatchRecord, ...]，调用 Task 1 evaluator/Task 2 Store。
- WatchRuntime.start()/stop() 为异步方法；stop 不关闭其他 lane 共用资源。
- replay_watches(definitions: tuple[WatchDefinition, ...], frames: Iterable[WatchFrame]) -> WatchReplayReport；该报告在 replay/watches.py 定义，含事件/终态/输入与规则哈希。

- [x] 写 test_live_and_replay_equal：同输入、时钟和规则，实时与重放同事件身份/终态。

~~~python
assert live.event_ids == replay.event_ids
assert live.final_states == replay.final_states
~~~

- [x] 写 test_gap_recovery_suppresses_old_signal、test_stream_duplicate_and_conflict。超过 120s 的回补触发只 suppressed；创建前暖机不触发；缺口/内容冲突阻断；UTC 边界、收盘、合约/市场混入校验。
- [x] Run: python -m pytest tests/runtime/test_watch_runtime.py tests/replay/test_watch_replay.py tests/adapters/test_watch_data.py -q。先失败。
- [x] 实现基于现有客户端的 Adapter 和独立 worker，按当前官方协议建立离线合同 fixtures；通过上述检查。

**Deliverable:** Watch 持续运行无需 LLM；相同 evaluator 用于历史回放。真实公共数据联调留收口阶段。

## Task 4：Agent 协议、运行档案与费用

**Files**

- Create: agent_platform/domain/event_agent.py、agent_tools.py、ports/event_agent.py、agent_model.py、adapters/sqlite/event_agent.py、adapters/openrouter/agent_chat.py。
- Modify: 新子限额涉及的 adapters/sqlite/budgets.py/schema.py/migrations.py，保留原累计账本语义。
- Test: tests/domain/test_agent_contracts.py、tests/adapters/test_agent_chat.py、test_agent_runs.py。

**Interfaces**

- event_agent.py 定义 EventAgentLane/AgentRun/AgentContext/AgentTurnRequest/AgentTurnResponse/AgentFinalDecision/AgentBudgetGrant。
- agent_tools.py 定义 ToolSpec/ToolCall/ToolResult；ToolCall 有稳定 tool_call_id/name/结构化 arguments。
- AgentModelPort.turn(request: AgentTurnRequest) -> AgentTurnResponse，返回 tool_calls 或 final（二者不并存）及 ModelUsage/ProviderMetadata。
- AgentRunStorePort.claim(event: WatchEvent, lane: EventAgentLane, at: datetime) -> AgentRun，event_id/lane 唯一。
- AgentRunStorePort.claim_analysis(lane: EventAgentLane, request_id: str, at: datetime) -> AgentRun，独立研究请求持久幂等。
- AgentRunStorePort.record_turn(run_id: str, request: AgentTurnRequest, response: AgentTurnResponse | None, status: str) -> AgentRun。
- AgentRunStorePort.record_tool(run_id: str, call: ToolCall, result: ToolResult) -> AgentRun。
- AgentRunStorePort.finish(run_id: str, decision: AgentFinalDecision, at: datetime) -> AgentRun。
- AgentRunStorePort.abort(run_id: str, status: str, reason: str, at: datetime) -> AgentRun；status 仅 FAILED/EXPIRED/INTERRUPTED/RECONCILING。
- AgentRunStorePort.get(run_id: str) -> AgentRun；recover(lane_id: str, at: datetime) -> tuple[AgentRun, ...]。
- BudgetedAgentModel 实现 AgentModelPort，包装既有预留/结算，叠加 AgentBudgetGrant 子限额；未配置 grant 不发送收费请求。

- [x] 写 test_tool_call_protocol：Mock HTTP 输出 tool_calls，下一请求正确回传 tool_call_id；模型身份/畸形协议拒绝。

~~~python
assert next_message.tool_call_id == prior_call.tool_call_id
assert settled_usage.request_id == sent_request.request_id
~~~

- [x] 写 test_request_sent_unknown_is_not_reissued、test_parent_and_lane_budget_caps。重启后 RECONCILING/预留保留/send_count==1；两个限额都约束；无新 grant 时 paid_calls==0；原 JEV 账目不改。
- [x] Run: python -m pytest tests/domain/test_agent_contracts.py tests/adapters/test_agent_chat.py tests/adapters/test_agent_runs.py -q。先失败。
- [x] 实现新 Adapter/运行档案；复用 HTTP、价格/费用解析，旧 OpenRouterChatModel 仍拒绝 tool_calls；通过检查。

**Deliverable:** 有界工具协议及持久费用恢复；现有摘要模型仅作候选，能力验收前不声称已支持。

## Task 5：Context、工具权限与 Agent 编排

**Files**

- Create: agent_platform/application/agent_context.py、agent_tools.py、agent_orchestrator.py、runtime/event_agent.py。
- Test: tests/application/test_agent_tools.py、test_agent_orchestrator.py、tests/runtime/test_event_agent.py。

**Interfaces**

- AgentContextBuilder.for_event(event: WatchEvent, lane: EventAgentLane) -> AgentContext；for_analysis(lane: EventAgentLane) -> AgentContext，均异步。
- ToolRegistry.available(mode: str) -> tuple[ToolSpec, ...]。
- ToolRegistry.execute(call: ToolCall, run: AgentRun, lane: EventAgentLane) -> ToolResult。
- AgentOrchestrator.analyze(lane: EventAgentLane) -> AgentRun；review(lease: EventLease, lane: EventAgentLane) -> AgentRun，均异步。
- EventAgentRuntime.start()/stop() 异步；租约 120s/每 30s 续租，90s bound 内维持领取所有权，关闭只影响所属 lane。存储 Ports 的上述方法均为异步。
- 初次分析用唯一持久 analysis request ID，不假造 WatchEvent；交易工具在 Task 7 装配前返回 unavailable。

- [x] 写 test_analysis_creates_watch_then_review：FakeModel 经工具建 Watch，evaluator 触发，review 读取原假设/条件值/最新事实。

~~~python
assert review.context.trigger.event_id == emitted.event_id
assert review.context.latest_market.captured_at >= emitted.occurred_at
~~~

- [x] 写 test_mode_and_scope_permissions、test_pause_or_revision_change_discards_inflight、test_loop_bounds_and_no_self_wake_storm。ANALYSIS 交易拒绝，跨账户参数拒绝；在途旧结果不产生意图；第 5 次模型/第 9 次工具拒绝；重投复用 run；有效 Watch 不产生额外定时请求。
- [x] Run: python -m pytest tests/application/test_agent_tools.py tests/application/test_agent_orchestrator.py tests/runtime/test_event_agent.py -q。先失败。
- [x] 实现上下文压缩/哈希、模式工具与工作流；遵守 32 KiB/90s 限额。领取失败的自动重试最多 3 次，已发送模型或已有副作用时转恢复，不重放整个 run。通过检查。

**Deliverable:** 研究→Watch→事件→二次判断可离线运行，保留 Task 2/4 恢复语义。

## Task 6：证据兼容与公共执行授权

**Files**

- Create: agent_platform/domain/agent_trade_evidence.py、domain/trade_authorization.py、ports/trade_authorization.py、application/trade_authorization.py。
- Modify: domain/trading_execution.py、application/trading_execution.py、application/futures_trading.py 及相应 bootstrap 调用点。
- Test: tests/domain/test_agent_trade_evidence.py、tests/application/test_trade_authorization.py、tests/integration/test_jev_authorization_compatibility.py。

**Interfaces**

- 新文件定义 EventAgentTradeEvidence/GuardianTradeEvidence；旧 TradeDecisionEvidence 存档结构不改。
- TradeCommand.decision_evidence 兼容三种证据，各自验证 command ID/scope/版本/数量。
- domain/trade_authorization.py 定义 FuturesRiskSettings，字段 policy: TradingPolicy、limits: TradingLimits、qty_step/min_qty/max_qty/min_notional: Decimal；仅承载纪律，不含 JEV 决策来源。
- TradeAuthorizationPort.authorize(command: TradeCommand, snapshot: FuturesMarketSnapshot, account: TradingAccountSnapshot, at: datetime) -> None，为异步方法。
- SharedFuturesRisk.evaluate(settings: FuturesRiskSettings, original: FuturesQuote, current: FuturesQuote, account: TradingAccountSnapshot, action: str, quantity: Decimal, leverage: int | None) -> None，为纯校验函数，抽取原 _risk，保留计算/原因。JEV 从原 TradingRun 投影设置，新 Agent 从自己的 lane 配置投影。
- TradeExecutionService 在后端 submit 前必经 authorizer；submit 的 preflight 保留作补充，更新现有装配/Fake 依赖。

- [x] 写 test_old_jev_evidence_roundtrip、test_event_agent_cannot_forge_jev_or_other_scope。

~~~python
assert parsed_legacy_evidence == original_legacy_evidence
assert parsed_legacy_command.command_id.startswith("jev:")
~~~

- [x] 写 test_backend_never_called_without_authorization、test_risk_extraction_preserves_jev_cases、test_triggered_hypothesis_can_be_cancelled_or_invalidated。无可选 preflight 时也受限，越限 backend.calls==0；原手算案例继续通过；触发后用户取消或最新数据满足失效条件时，新开仓拒绝。
- [x] Run: python -m pytest tests/domain/test_agent_trade_evidence.py tests/application/test_trade_authorization.py tests/integration/test_jev_authorization_compatibility.py -q。先失败。
- [x] 实现兼容扩展与授权注入，新增 Agent 证据必填；旧 nullable evidence 的使用方只允许明确的旧权限路径，不能靠空证据启动新 lane。通过专项及受影响原执行/JEV 检查。

**Deliverable:** 新 Agent 不套 JEV 请求，公共纪律是必经执行检查；不改 JEV 阈值或算法。

## Task 7：独立 Paper 意图与 Guardian

**Files**

- Create: agent_platform/domain/trade_intents.py、position_protection.py、application/agent_intents.py、position_guardian.py、adapters/sqlite/position_protection.py、runtime/position_guardian.py。
- Modify: 新保护表相应 SQLite schema/migrations/backup，及 Task 5 工具装配。
- Test: tests/application/test_agent_intents.py、tests/runtime/test_position_guardian.py、tests/integration/test_event_agent_paper.py。

**Interfaces**

- trade_intents.py 定义 TradeIntent/TradePreview；position_protection.py 定义 PositionProtection/ProtectionStatus。
- AgentIntentService.preview(intent: TradeIntent, lane: EventAgentLane) -> TradePreview。
- AgentIntentService.submit(intent: TradeIntent, lane: EventAgentLane) -> ExecutionRecord，使用 Task 6/现有执行通道。
- ProtectionStore.prepare(intent: TradeIntent) -> PositionProtection，发送前持久保存；recover(scope: ExecutionScope) -> tuple[PositionProtection, ...]。
- PositionGuardian.step(scope: ExecutionScope) -> ProtectionStatus，不调用模型；独立 runtime 每 1s 调度，不承诺网络实际响应时延。
- 所有方法异步；intent/run/event/command ID 稳定关联；每个开仓有合法 protective_stop_mark，减仓不扩大保护范围。

- [x] 写 test_paper_open_reduce_and_archive、test_duplicate_event_one_command。

~~~python
assert archived.command_id == executed.command.command_id
assert backend.submit_count_after_recovery == 1
assert account.quantity_after_full_reduce == 0
~~~

- [x] 写 test_guardian_survives_agent_pause、test_partial_fill_crash_restores_protection、test_missing_quote_is_degraded_not_fake_fill。Agent/模型停止后保护仍执行所属账户 reduce；部分成交可恢复；缺行情 degraded，不伪造成交，不操作 JEV 钱包。
- [x] Run: python -m pytest tests/application/test_agent_intents.py tests/runtime/test_position_guardian.py tests/integration/test_event_agent_paper.py -q。先失败。
- [x] 实现意图预览/提交/保护，复用 Paper 内核；可信保护减仓与普通暂停权限分别验证，通过检查。

**Deliverable:** 完整 Paper 闭环与独立保护；模型不在线也能处理已配置的保护条件。

## Task 8：产品入口与运行装配

**Files**

- Create: agent_platform/bootstrap_event_agent.py、web/event_agent_routes.py、web/templates/event-agent.html。
- Modify: config.py、bootstrap.py、web/app.py、web/templates/navigation.html。
- Test: tests/web/test_event_agent_routes.py、tests/integration/test_event_agent_isolation.py。

**Interfaces**

- build_event_agent_services(...) -> EventAgentServices，定义于 bootstrap_event_agent.py，包含 lane/watch/agent/guardian；输入为前述 Ports、clock、lane 配置，默认不开启。
- 页面 /event-agent；状态 /api/event-agent/status；有界研究 /api/event-agent/analyze；Watch CRUD 和 lane 启动/暂停。
- 写操作沿用 LocalBrowserSession、同源/CSRF、expected_revision；普通运行开关不授予真实模型费用权限。

- [x] 写 test_controls_are_lane_scoped、test_status_has_evidence_and_actual_capability。

~~~python
assert jev_after_agent_pause == jev_before_agent_pause
assert status.execution_environment == "paper"
assert status.protection_status in {"ready", "degraded", "not_configured"}
~~~

- [x] 写 test_csrf_revision_and_restart：跨源/旧版本拒绝；重启恢复但暂停新增交易，保护继续。界面展示实际 Watch/事件/run/风险拒绝/订单/成本/保护状态，不将 intent 显示为 fill。
- [x] Run: python -m pytest tests/web/test_event_agent_routes.py tests/integration/test_event_agent_isolation.py -q。先失败。
- [x] 实现装配及界面，通过检查；人工查看隔离服务，不重载用户既有服务，不开展无关 UI 重设计。

**Deliverable:** 与 JEV 并列、能独立操作的产品入口。

## Task 9：故障验收与交付说明

**Files**

- Create: tests/integration/test_event_agent_recovery.py、tests/architecture/test_event_agent_boundaries.py。
- Create: docs/EVENT_AGENT_RUNBOOK.md、docs/EVENT_AGENT_VERIFICATION.md。

**Interfaces:** 使用 Task 1–8 已定义接口，不新增第二套交易流程。

- [x] test_fault_windows 覆盖触发后/领取后/模型发送后/工具提交后/部分成交后崩溃，分别核对事件、收费、订单与保护。

~~~python
assert recovered.external_order_submissions == 1
assert recovered.unknown_fee_reservations == original_unknown_reservations
assert recovered.protected_filled_quantity == backend.actual_filled_quantity
~~~

- [x] test_slow_model_does_not_block_jev_or_guardian 使用可控慢异步模型，断言独立调度继续；test_architecture_and_backup_restore 约束 Domain/Application 不导入 Adapter、新表可恢复、来源不混。
- [x] Run: python -m pytest tests/integration/test_event_agent_recovery.py tests/architecture/test_event_agent_boundaries.py -q；之后跑前述 MVP 和受影响兼容专项，python -m ruff check 本次实际修改的 Python 文件。每条命令必须记录真实结果。
- [x] 写 runbook：独立钱包/风险/保护/费用配置、只读、暂停、恢复、积压、unknown/degraded。报告列证据与局限，不用旧测试计数充当本轮结果。
- [ ] 后续有界真实公共行情/真实模型验收：能力、价格、预算预检，明确新 Agent 的费用授权后执行；允许 WAIT，不强迫成交。缺收费授权不阻断上述离线开发。

**Deliverable:** 可复现、可审计的第一阶段 MVP；长稳、Coding/Sandbox、Testnet 不随一次闭环自动启用。

## 顺序、估计与后续

依赖：Task 1→2→3；4 在契约稳定后推进；5 依赖 2/3/4；6 基于现有交易基础；7 依赖 5/6；8/9 收口。当前采用本人逐任务实现的方式，不委派子 Agent。

粗估单人工作量：1–3 为 4–6 日，4–5 为 4–6 日，6–7 为 4–6 日，8–9 为 2–4 日，合计 14–22 工作日。首个可靠 Watch 交付后重新校准，持续运行观察另计。

MVP 后优先运行加固。自主研究单独规划 Sandbox 权限/数据/输出、共享规则回放、指标版本/晋升；Testnet 单独规划实际订单、保护单和账户对账。真实资金不属于本计划。

本文件是可评审草案。进入开发时结合用户最终选择和评审意见调整；新增 Agent 资金/风险/费用参数在相应启动阶段配置，不阻断现在的设计交付。
