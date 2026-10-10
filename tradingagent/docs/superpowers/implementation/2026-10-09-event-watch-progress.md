# SDD ledger — plan: docs/superpowers/plans/2026-10-09-event-driven-agent-mvp.md

Scope: 用户本轮仅授权 Task 1–3。Task 4–9 不实施。
Spec: docs/superpowers/specs/2026-10-09-event-driven-agent-design.md
Branch: codex/event-watch-core

Pre-flight: Task 1 WatchRecord/WatchFrame/WatchEvaluation → Task 2 原子存储 → Task 3 runtime/replay，接口一致；任务 3 将用同一个 evaluator。
Ruling: 现有核心源码尚未纳入 Git，不复制到缺少这些文件的 worktree；在新工作分支原 checkout 实现，并保存改动清单/证据 — 保留用户现有实现 — 隔离弱于独立 checkout，变更仅限本次清单。
Ruling: 使用 Windows 原生步骤与此持久 ledger 替代 Unix shell 的 SDD 脚本；未跟踪基础不作整体提交 — 避免收录用户其他工作 — 需要后续由用户选择完整 Git 基线。

Baseline: 原指标/SQLite/决策/备份专项 58 passed in 5.48s，2026-10-09；使用现有 Conda，禁止测试外网，临时目录 output/verification/event-watch-baseline-02。
Environment: 首次命令使用错误测试路径，已按 rg 改正；沙箱默认临时目录不可写，显式基准目录中的异步 SQLite 运行阻塞；终止后相同离线检查在受审查的非沙箱执行下通过。没有修改业务代码或用户数据处理该环境问题。

Task 1: complete — missing-module RED observed; domain tests GREEN 34/34 (pytest, no cache).
Task 2: complete — missing-module RED; additional conflict/TTL dispatch boundaries RED→GREEN. Isolated store/delivery tests plus old SQLite/decision/backup/retention compatibility passed.
Task 3: complete — missing-module RED; runtime/data/replay tests GREEN 14/14. Integrated Tasks 1–3 + affected compatibility suite GREEN 116/116 (7.82s).

Task 2: Ruling: Core schema migrates 7→8; old v7 backups remain supported; five legacy assertions use current SCHEMA_VERSION — new permanent tables require explicit versioning — cost: existing databases create a pre-v8 SQLite backup on next initialization.
Task 2: Ruling: Delivery names QUEUED/COMPLETED/FAILED/SUPPRESSED map to the spec's PENDING/DONE/DEAD_LETTER/EXPIRED, with DISPATCHED and RECONCILING explicit — avoid ambiguous pre/post-dispatch recovery — cost: Task 4 must consume these typed states rather than the illustrative names in the vision document.
Task 3: Ruling: Identical candle facts deduplicate independently of changing warmup history/reception; a repaired window may re-evaluate a previously unavailable same bar — otherwise recovery could never resolve a persisted gap — cost: input hash can change on repair, while event identity remains watch/version/bar.
Task 3: Ruling: REST reconnect repair is bounded to 120 bars; after review, indicator window also uses up to 120, matching existing EMA/ATR math instead of reseeding EMA26 every bar — no unbounded HTTP loop — cost: older omitted history needs a separate historical replay, not a promise of exhaustive recovery.
Compatibility: Existing architecture aggregate reports two unchanged legacy violations in application/futures_trading.py and application/jev_tasks.py. This scope does not modify them or relax the checker. New Watch architecture test passes. Full architecture aggregate is not claimed green.

Final review: one fresh reviewer (gpt-6-astra, xhigh) completed read-only review. No Critical or Minor; five Important findings reproduced in eight failing regressions (RED 8 failed / 20 passed), then all Watch tests GREEN 69/69.
Final: fixed post-trigger conflict blindness — service/store monitor pending triggered watches; runtime preserves the subscription until event retirement/TTL. Direct service and running-source regressions RED→GREEN.
Final: fixed restart same-cursor repair omission — Adapter outputs cursor bar plus repaired history before socket; Adapter→Runtime→Store invalidation regression RED→GREEN.
Final: fixed duplicate-candle expiry override — duplicate suppression applies only when it does not override a state transition; expiry regression RED→GREEN.
Final: fixed Decimal representation conflict — frame/SQLite/replay share exact coefficient/exponent hashing without ambient-context rounding; real value changes still conflict; numeric equivalence regressions RED→GREEN.
Final: fixed EMA26 seed-only production window — REST and streaming retain up to 120 bars, algorithm version records lookback120; nonconstant EMA/reconnect regression RED→GREEN.
Final: Ruling: The review's deferred judgments (request/fee reconciliation, pause/style execution retirement, Guardian fill recovery, real wallet/evidence segregation, live Binance/UI/bootstrap and exhaustive >120-bar catchup) remain Tasks 4–9/final live acceptance — outside user-authorized 1–3; dispatch leases and lane isolation are implemented here — cost: this foundation alone cannot run the full trading agent or authorize trading.
Git provenance: Current checkout contains many pre-existing staged additions; Watch additions remain separate. No index reset/add/commit was performed. Index snapshots also reproduce both known legacy architecture violations.
Demonstration: examples/event_watch_offline.py completed in a unique output/verification/event-watch-demo SQLite with one TRIGGERED event, COMPLETED delivery and identical live/replay event ID.
Final integrated verification: 199 passed / 1 known legacy architecture aggregate deselected / 19 subtests passed / 1 existing FastAPI-httpx deprecation warning, 33.86s, output/verification/watch-final-01. New/affected source and tests Ruff clean.
Final supplemental validation: enhanced nonconstant EMA test now covers an actual next closed WebSocket candle and reconnect, including identical live/recovered content hash; test_watch_data.py 11/11 passed. All five review fixes remain covered; no further reviewer dispatched.
Final: baseline architecture findings separately reproduced against Git index and workspace; no test checker was relaxed. Windows sandbox also blocks some async tests without SQLite; existing non-sandbox offline execution resolves it.
Finish: Local Task 1–3 delivery remains on codex/event-watch-core; no commit, push, merge or worktree removal requested or performed. Global architecture suite is not green due to known pre-existing violations, so branch integration is not claimed complete. Persist ledger and file-hash manifest instead of deleting the only record while baseline source is uncommitted.
Task 4–9: remain unimplemented; plan checkboxes 1–3 are complete, later checkboxes stay open.
