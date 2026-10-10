# 事件 Trading Agent：离线与 Paper 运行

当前交付包含计划 Task 1–9 的离线实现。页面入口是 /event-agent，与 JEV 分别启停、分别保存钱包。默认未启用；没有沿用任何 JEV 模型费用授权。本机保护依赖进程存活，并非交易所保护单。

## 启动隔离工作台

在项目目录、现有 tradingagent Conda 环境运行：

    python tools/start-event-agent-offline.py --port 8781 --database output/event-agent-offline.sqlite3

打开 http://127.0.0.1:8781/event-agent 。上述脚本装配 Fake K 线、Fake WAIT 模型和 Paper 执行，不读取交易所私钥，也不调用收费模型。基础服务库与事件库分别是指定路径及其 .event-agent.sqlite3 文件。重启保留事实，暂停新研究/交易；Guardian 恢复已有保护。

更完整的部署通过 RuntimeConfig(event_agent=True) 装配；默认 event_agent=False。event_agent_database 可指定独立库，不能与 JEV 主库相同。现有服务不会自动开启本板块，也没有在本轮开发中重载。

## 独立账户与参数

默认 lane 为 event-lane-1，Paper session 为 event-agent-1，账户为 paper:futures:event-agent-1。它不使用 JEV 的 session、控制设置或钱包。自定义 lane 通过 build_event_agent_services 的 lane 参数传入。

页面资金配置需要用户明确填写并确认：初始 USDT、固定杠杆、最大持仓名义金额、最大亏损、数量过滤、手续费/滑点和策略要求。不配置资金也可研究/Watch，交易授权会拒绝。资金与合约规则在钱包初始化后固定；改变策略产生新版本并暂停，旧模型结果不能增加风险。V1 使用用户配置的杠杆，模型不能提高杠杆。

每个开仓意图必须给出正确方向的 protective_stop_mark。保护在发送前持久化，依据实际成交数量生效；Guardian 检查 mark 止损和 run loss，每秒调度一次。网络与进程调度不保证一秒实际响应。资金费使用现有 Paper 内核，按已发布结算事实持久去重；离线默认资金费数据为空。

未终结的开仓即使已被部分保护减仓至零，仍保留保护和订单查询，直到原订单终结；后续实际成交继续受保护。资金费维护最多占用每轮 0.5 秒，失败显示 degraded，已有可执行止损继续。资金费窗口从最近仓位变化/已结算游标之后开始，开仓前的资金费不应用于新仓；跨仓位变化的迟到历史结算尚需独立对账，不能直接用当前数量补算历史费用。

## 研究、Watch 和复核

启用后手动点击一次研究，或由 Watch 事件启动 REVIEW。不会因为有 Watch 而每 15 分钟再调用模型。默认离线模型只返回 WAIT；完整的建 Watch/触发/提交交易路径由脚本模型和离线集成测试验证，不能把 WAIT 模型视为真实交易策略。

ANALYSIS 可取行情/指标目录、建/改/取消/列 Watch。REVIEW 可取行情/账户/触发证据、预览/提交交易意图、建新 Watch。所有工具账户来自 lane，模型不能指定另一个账户。工具响应和副作用开始记录先落库；每轮最多 4 次模型请求、8 个工具调用、2048 输出 token/请求、32 KiB 消息与工具上下文、90 秒总限额。

Watch 使用 1m/5m 已收盘原生 K 线；20 个 ARMED/lane、单次触发、最多 24h。失效条件优先，旧/缺口/内容冲突数据不触发。事件有效期 120 秒。取消或替换已触发假设，也会使其新交易授权失效。Guardian 已有仓位保护不随假设取消撤销。

## 真实数据与模型装配

build_event_agent_services 可注入 WatchDataPort、FuturesMarketPort、时钟、lane 和 AgentModelPort。真实公共数据需要两种事实：已收盘 last_trade K 线用于规则，fresh mark/bid/ask 用于权益、保护和成交。market_source 必须与适配器输出一致。不能用 K 线收盘价冒充可执行报价。

收费模型必须是 BudgetedAgentModel，持有新 AgentBudgetGrant、显式价格表/有效期、lane/model/price version，以及父累计额度、lane 累计额度、单请求额度、小时次数上限。它使用现有 SqliteBudgetStore，可以与 JEV 共用费用库而不共用授权；父额度包含已存在的 JEV 花费/预留。普通页面启停不接受 grant。

OpenRouterAgentModel 实现结构化工具循环与 provider/model 身份检查；助手 tool_calls、tool_call_id 结果和 opaque reasoning_details 按 [OpenRouter 工具协议](https://openrouter.ai/docs/guides/features/tool-calling) 回传。本轮只验证 Mock HTTP 合同，没有验收任何具体收费模型的真实工具能力、价格或响应质量。使用前需要独立费用授权和有界能力验收。

## 暂停和恢复

- 暂停只影响此 lane 的新研究/风险增加，在途旧版本结果仍归档。Watch 持续保存行情事实，Guardian、资金费维护和订单查询继续。
- 重启恢复已触发事件、SENT 请求、已准备保护和未完成命令；不会自动恢复新增交易，需用户重新启用。
- 未发送事件租约到期可有界重试，最多 3 次。已发送模型或已开始工具的 run 转 RECONCILING，不重放整轮。
- 研究占用 lane 时事件保持排队，不提前消耗租约；派发前取消会结束所属运行并释放或抑制事件。重复研究 request_id 返回已保存的运行，不中断原所有者。
- 模型收费 unknown：保留预算预留；相同 request_id 永不再次收费发送。核对 provider usage 后，使用原 reservation/request/route/price 身份进行已有费用账本 settle；未确认前不要释放预留。
- 订单 unknown：只使用 TradeExecutionService.reconcile 查询原 command_id。不能创建新命令来替代未知订单。部分成交按已知实际数量保护，即使原开仓仍未完成，可信 Guardian 也只减少同一保护所属仓位。
- prepare 后、reserve 前崩溃：保护仍保留且不假设成交；核对 execution journal 与 run 事实后人工结束该未派发保护。该不确定状态不能自动重新开仓。
- 行情或存储暂不可用：保护显示 degraded，任务继续尝试；不伪造 fill。恢复执行报价后继续保护。暂停 Agent 不能解决报价缺失。

RECONCILING run 会占据 lane 的在途槽，直到人工核对并通过 store.abort 结束为 INTERRUPTED/FAILED；结束前核对费用、工具、订单和保护，不直接改 SQLite body，也不要删除未知记录。

## 审计与备份

/api/event-agent/status 展示 lane、Watch、delivery、run、模型 usage、实际 Paper 回执与保护。intent 不是 fill；filled_quantity、fee_usdt、backend_at 来自实际 Paper 执行回执。没有真实响应的 SENT/RECONCILING 表示结果/费用未知。

事件库 schema v10 包含 Watch/输入冲突、delivery、Agent run/turn/tool/budget link、intent/protection、独立 Paper 钱包/操作以及公共 execution journal。使用现有 backup_database(source, new_destination, kind="core") 做只读在线备份，不覆盖已有备份。恢复到新文件后先做完整性校验并装配为暂停状态；费用共用独立库时也须分别备份费用库。

离线故障测试不代表长时间实盘稳定性。下一验收应是有界公共行情联调，再经独立费用授权验证真实工具模型；Testnet、交易所保护单和真实资金另行开发。
