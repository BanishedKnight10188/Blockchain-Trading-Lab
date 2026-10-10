# Event Watch foundation (Task 1–3)

本阶段提供：已收盘 1m/5m K 线规则、持久 Watch/触发事实/outbox、租约领取、独立监控和确定性回放。事件进入队列后，Agent 二次决策与 Paper 执行由 Task 4–9 接入。

## 离线示例

在 tradingagent 项目目录和既有 tradingagent Conda 环境执行：

~~~powershell
python -m examples.event_watch_offline
~~~

示例创建 output/verification/event-watch-demo 下唯一数据库，打印 TRIGGERED、单一事件、COMPLETED 领取结果和 replay_equal=true。仅使用合成 K 线，没有外部请求；领取完成仅表示示例消费完成，不代表模型运行或交易。

## 使用接口

~~~python
watches = SqliteWatchStore(isolated_database_path)
events = SqliteAgentEventStore(isolated_database_path)
await watches.initialize()
await watches.create(definition)
service = WatchService(watches, lane_id="event-agent")
runtime = WatchRuntime(service, source, clock)
await runtime.start()
# 持续任务按 lane 隔离；新增、更新、取消由持久 Watch 接口处理。
lease = await events.claim("event-agent", clock.utcnow(), 120)
await runtime.stop()
~~~

source 实现 WatchDataPort。BinanceWatchData 支持独立公开客户端的 REST 暖机/重连修复和 kline_1m/kline_5m 收盘订阅；后续装配应为它创建专用 FuturesPublicClient，隔离 JEV 的并发槽。runtime.stop 只终止自己的任务，共用客户端的关闭由装配层负责。监控覆盖 ARMED Watch，以及仍有未过期待处理事件的 TRIGGERED Watch；list_active 继续只返回 ARMED。此阶段没有加入 bootstrap、UI 或自动启动。

规则是平面 ALL/ANY，每组 1–8 个条件。支持 candle.open/high/low/close、volume_ratio_20、ema_12、ema_26、atr_14；运算 GT/GTE/LT/LTE/BETWEEN，数字按十进制字符串输入。创建最多 20 个 ARMED Watch/lane，有效期最多 24h，单次触发。更新使用 expected_revision 和递增 definition_revision，终态不能重开；观察后继假设使用新 watch_id 和 parent_watch_id。

## 时间、数据与恢复

- 只接受 UTC 对齐、原生含末毫秒的已收盘 K 线；历史发生时间为 opened_at+interval。创建前打开的 bar 仅用于暖机。
- 有效期为半开区间；失效判断优先于触发。只要求规则引用的指标可用；未知值不被当成 false。
- EMA12/26 分别需要 12/26 根，ATR14 需要 14 根；量比当前 volume 除以前 20 根均量，要求 21 根，零分母 unavailable。均使用 Decimal34；完整指标窗口为最多 120 根，算法版本包含 lookback120。超过 EMA period 后继续平滑，不将 EMA26 当成滚动 SMA26。
- 缺口阻断判断，修复后允许同根复验；重启输出保存 cursor 的当前根及其修复窗口，正常重复由 Store 去重。事实身份使用无损数值规范化，因此 105 与 105.00 相同；实际数值改变仍持久标记分区 data_conflict。冲突不会自动清除；核实来源和修复流程留待后续运维接入。
- 重连最多读取 120 根，指标窗口最多 120 根；较长停机只恢复已知连续窗口，不能代替完整历史回放。超过 120s 的触发保存事实但标记 SUPPRESSED，不可领取。
- 事件 ID 由 watch/version/bar 派生，Watch 状态、游标、事实及 delivery 同事务提交。重开数据库可继续领取。
- delivery 使用 QUEUED→LEASED→COMPLETED；失败为 FAILED，取消、冲突、过时为 SUPPRESSED。派发前失败最多领取 3 次；每次租约最长 120s，建议 30s 续租。旧 token 无法续租/完成。
- 在未来 worker 发送模型前必须持久化 mark_dispatched；DISPATCHED 超时进入 RECONCILING，保留 run_id、不重领。此接口不替代 Task 4 的请求档案和费用预留事务。

replay_watches 接收 definitions 和按 received_at 排序的 frames，返回事件、suppressed_event_ids、终态、规则/输入哈希和冲突分区。回放使用接收时钟，能复现迟到与 TTL；输入帧只包含当时已知的历史。

## 数据库与验证

Core SQLite schema 为 v8。v7 初始化前使用 SQLite 在线备份创建 .pre-v8-*.bak，然后事务迁移；原 journal/费用事实保持。备份验证兼容 v7/v8，新备份包含 Watch 和 delivery 表。

专项和受影响兼容测试离线运行；Windows 若沙箱阻塞 SQLite 异步 I/O，使用审批后的测试执行和项目内独立 basetemp。未改测试网络封锁。两个已有 JEV 文件的 aggregate 架构检查失败记录在开发 ledger；新 Watch 的依赖边界检查通过。
