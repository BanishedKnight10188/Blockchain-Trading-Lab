# 真实 JEV 合约 Paper 联调

## 当前接续：真实 Paper 周期和有界服务

**15:31:41（上海）最新：服务在线，钱包自动暂停rev9。** 9次已知费用返回中7轮有效WAIT，另2次invalid_model_assessment被拒绝；未取得它们的具体解析失败阶段。Paper确认0.002213526USD，含先前诊断确认总0.002459016USD；5笔unknown总预留0.002161068USD，剩余0.095379916USD。15:31网络传输失败自动停止新模型请求，持仓报价维护继续，1000USDT/空仓/模拟手续费0。真实自动暂停已验证，未继续收费重试。最新逐笔记录 `output/verification/jev-paper-vpn-final-b-20261008.json`；下一处理响应分阶段诊断及网络/时序稳定。下面数字为15:11和15:27的过程记录。

2026-10-08已从单次诊断接到通用交易后台。查看 <http://127.0.0.1:8776/jev-trader>，原 <http://localhost:8775/overview> 仍使用Mock预览。模型独立调用JEV，行情来自Binance公共USDT合约接口，执行后端仅Paper虚拟账本。BTCUSDT、原始风格92/v1、1000虚拟USDT、逐仓2倍；策略和500持仓/250单次名义/20亏损等纪律为沿用的试验规则。

15:00–15:03连续4轮真实WAIT，各1.36–1.43秒，轮次间隔至少60秒。暂停221.30秒无新增请求或费用而报价更新；15:09恢复后15:11再有1轮真实WAIT。5轮Paper确认消费0.001229382USD，含先前诊断确认总0.001474872USD。15:09/15:10两笔provider_error的预留0.000864192USD保留，连旧未知总held0.001728720USD；其具体HTTP原因当时仍未保存，不能猜零费用或伪称WAIT。

新授权 `data/jev-paper-vpn-run-20261008.local.json`：grant `vpn-paper-20261008`，总上限 **0.10 USD**、单次 **0.02 USD**，截止 **2026-10-08 15:53:27（上海）**。这是依附根授权的独立接续记录；旧13:56到期文件不重写，不另开费用库或清空预留。所有同日请求按持久最低封顶执行，预算不足/冻结/频次、接口故障或到期暂停新决策，已有持仓继续维护。页面提供剩余额度与截止时间。

启动工具（仅在当前配置尚有效、服务未运行时使用；不要同时启动同一钱包）：

```powershell
Set-Location 'D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent'
& 'C:/Users/exile/anaconda3/envs/tradingagent/python.exe' -X utf8 tools/start-jev-paper-live.py
```

工具在准备阶段免费核对Key上限，Key来自已有本机环境，只写入当前进程，文件和命令行没有Key。`--prepare`只创建一次具名本轮授权；已有文件拒绝替换，不能用重启扩展有效期。启动后保留钱包/费用/策略并暂停，通过页面确认运行才允许后续模型。正式通用CLI的 `--model-budget-database` 可选择已有共享账本；不能省略后用新的钱包DB重置费用。

公共数据故障现在免费拒绝启动或跳过本轮，不进行模型派发。实际恢复遇market_unavailable，NTP只读测量中位+0.129秒，免费quote有52–59毫秒未来mark及部分盘口时间顺序拒绝，也有通过样本；未修改Windows时间或放宽证据守卫。15:27再次通过守卫恢复。接口失败停止保护经13项RED及85项相关回归验证；HTTP数字状态/传输失败/超时/无效响应分别显示，未知费用保留，不循环自动重发。

当前状态/账本以 `output/verification/jev-paper-vpn-final-20261008.json` 为准。每个真实cycle关联同一共享账本的request_id，Pause证据、原会话/根policy、余额和费用均逐项只读核验。证据另包括jev-paper-vpn-{start-b,cycle-a,pause,paused-check,resume-b,provider-failure-pause,after-public-probe-start}-20261008.json、jev-run-final-20261008.txt、jev-failure-stop-green-20261008.txt、jev-start-reason-green-20261008.txt。

已经验证真实WAIT周期、持续调度、暂停/恢复和费用落账。**WAIT没有成交；真实JEV驱动开仓/减仓、24h稳定性、Testnet及Agent OS后端尚未验收。** 不为制造成交而放宽策略或置信阈值。以下单次诊断和旧配置内容保留为历史。

2026-10-08，用户明确要求“真实 JEV Paper 联调”。沿用累计1 USD、单次0.02 USD；这次指令作为新的有界联调授权，未自动续用10月7日到期文件，也未增加累计预算。

## 当前结果：VPN 代理已取得真实 typed 决策

2026-10-08 **14:31:06（上海）**，显式使用用户既有 `http://127.0.0.1:7897`，`POST /api/alpha/decisions` 返回 **HTTP 200**。实际模型 `typesafe/jev-1.13-20260917` / TypeSafe；BTCUSDT、168根真实7天1h历史、用户92/v1风格，结果 **WAIT，confidence 0.92**。这是实际JEV返回，不是Mock，confidence也不是测得的交易胜率。

请求 `a1922e3a55c84aceae9fee4f6ef80737`，provider ID `gen-dec-1791441066-NfOrhw1GpPSWa3EZTXkd`。输入5845/输出48 token，响应实际费用 **0.00024549 USD**，原8775共享账本已结算。旧13:40未知0.00043218与14:21直连403未知0.000432348仍预留，总held **0.000864528 USD**，合计本地费用占用0.001110018USD。免费Key接口显示limit0.10USD、usage0，汇总不作为单笔推翻或清零依据。

原直连请求14:21:39返回403：`This model is not available in your region.`。客户端原先 `trust_env=False`，不继承系统代理；本轮新增显式模型代理配置，按用户VPN指令修复。不能据此断言13:40旧请求一定相同。免费空请求/删model校验均400，无推理；同state/questions只报model缺失，未发现必填Schema错误。

独立虚拟钱包仍1000USDT/空仓/pausedrev3，原用户session/BTCUSDT/92v1不变，交易所订单0。8775仍公共行情+Mock预览，页面200，未自动启用持续实盘或Paper操盘。**当前验收为单次真实决策和本次费用结算；真实Paper完整周期、模拟成交、持续运行与Testnet仍未验收。**

配置方法：正式真实JEV启动命令增加 `--openrouter-proxy http://127.0.0.1:7897`；它独立于 `--futures-proxy`。代理仅接受无凭据的本机HTTP地址，不修改系统网络，也不重试模型请求。403保留明确失败码并暂停新决策，已有持仓维护继续，页面显示拒绝访问说明。

诊断工具 `tools/diagnose-jev-real.py` 默认为免费预检，`--execute`独占创建报告/单次授权，拒绝重放。`data/jev-diagnostic-proxy-20261008.local.json` 是单次诊断封套，**不能直接用作 `--paper-model-config`**；不修改旧Paper不可变策略或续用到期文件。每笔推理保留账本。已有费用限额、凭据和代理无需重新向用户索取。

最新证据：

- `output/verification/jev-real-diagnostic-20261008.json`：直连403具体原因及传输阶段。
- `output/verification/jev-proxy-free-preflight-20261008.json`：代理免费Key200/空请求400、远端限额0.10。
- `output/verification/jev-real-proxy-diagnostic-20261008.json/.txt`：真实200/typed WAIT/usage及已结算预算。
- `output/verification/jev-proxy-final-verification-20261008.json`：3笔持久费用、原状态与钱包保留、无Key；8775为200，8774连接拒绝，未干预旧服务。
- `output/verification/jev-forbidden-{red,runtime-red,green}-20261008.txt`：拒绝码及暂停/持仓维护，最终48项通过。
- `output/verification/openrouter-proxy-{red,green}-20261008.txt`：配置/凭据/诊断定向验证，37项通过；Ruff/JS语法/CLI参数检查通过，无全套或新依赖。

下一步继续按原预算完成真实Paper完整周期与持续运行配置，保留旧unknown费用供单笔核对；不把诊断WAIT伪称模拟成交。下文保留代理修复前过程证据，历史“未响应/等待Activity”不再是当前链路状态。

## 历史：首次接口失败，费用未知

- 本机系统环境变量存在Key，免费 `GET /api/v1/key` 认证200；Key只读入进程，没有写入脚本、配置、报告或聊天。
- 官方[JEV模型页](https://openrouter.ai/typesafe/jev-1.13/)和[Decisions文档](https://openrouter.ai/docs/guides/community/jev)确认 `typesafe/jev-1.13`、`POST /api/alpha/decisions`，价格0.042 USD/百万输入token、输出0。普通 `/api/v1/models` 未列出JEV，不能据此认定Decisions模型不可用。
- 准备时原10月7日与8775预览的 `budget_requests` 均空。试验使用8775数据库的持久费用账本，独立钱包不建立新的收费预算。
- 所选BTCUSDT/7天/1h真实公共历史168根、所需filters已取得。当前用户会话和92/v1完全保留。
- 首次执行因未来mark报价被阻断，模型派发0；用户校时后免费quote守卫通过，没有放宽时效或改时间戳。免费复核请求中点差282.85ms含网络误差，不能当作NTP精确时差。
- 第二次启动被持久权限守卫拒绝，模型派发0。独立DB中agent-controls行数为0，试跑工具只有内存默认值；通过现有CAS服务保存已授权auto/paper操盘选择后继续，未绕过守卫。
- **13:40:56上海实际模型尝试1次**，request `fee44499cf6d40cbb29eb1a2ce759c40`。结果 `rejected/provider_error`，无typed answer，不是JEV的WAIT建议。具体HTTP状态/错误体当时未保留，根因尚未确定。
- **账本保留0.00043218 USD预留，确认消费0，实际费用unknown。** 13:42:37免费 `GET /api/v1/key` 返回200，累计/日/周/月usage均0；这是当前汇总读数，不作为该笔最终账单。未知预留没有清零，未再次发送推理。
- 独立钱包free1000 USDT、qty0、手续费0、rev3、已暂停，真实交易所订单0。原session/BTCUSDT/92v1不变，原8775页面和会话只读200。**真实JEV成功响应、费用对账、模拟成交和持续运行尚未验收。**

## 已准备的有界运行

`tools/run-jev-futures-trial.py` 默认免费预检；`--execute`才允许一轮。独立数据库 `data/jev-futures-real-20261008.sqlite3`，独立会话 `9961cbdeb05c47388abcb08eb7cfc894`，复制用户已确认BTCUSDT和原始92风格。仅通过现有通用操盘服务和Paper执行后端，不接任何交易所写接口。

试验虚拟本金1000 USDT、逐仓2倍、单次名义250 USDT、总持仓名义上限500 USDT、模拟亏损上限20 USDT；手续费4bps、滑点2bps、价格漂移20bps、判断置信阈值0.7。策略为有明确历史趋势才考虑开仓、证据不足WAIT、方向失效REDUCE；这是联调默认规则，没有收益验证。原用户会话的本金与策略没有被固化。

`SingleDispatch` 在成功或失败后都拒绝第二次请求；实际免费检查两种路径均只调用底层一次。模型HTTP自身没有重试，预算预留和真实费用走已有门控。结束/异常时recover暂停钱包并关闭公共/模型客户端、释放进程锁。后续工具补数字HTTP状态观察，不保存响应体/头/Key；离线200/402/429/503四路径均每条一次。`integration_accepted`明确区分正常返回与联调验收，执行未验收退出2；此次旧工具exit0不能作为成功证据。

本轮配置 `data/jev-futures-real-20261008.local.json` 有效期：2026-10-08 **13:26:41至13:56:41（上海）**，总1 USD、单次0.02 USD。期限不随重启改变；过期不运行。收费前曾用`--resume`继续同一账户；现在已有收费预留，明确拒绝恢复，避免重复收费。资金、会话、费用不清零，不覆盖首次报告。

## 历史：首次无 Activity 的核对计划

已请用户在[OpenRouter Activity](https://openrouter.ai/activity)或Logs中查看2026-10-08 **13:40:56（北京时间）**前后JEV记录，提供错误码、错误说明、该笔费用；若无对应记录，说明无记录即可。Key仍只保留本机。当前没有可用于单笔查询的provider generation ID，因此不能通过generation接口猜测账单。

普通Key汇总usage不等同单笔对账；官方[Activity API](https://openrouter.ai/docs/api/api-reference/analytics/get-user-activity-grouped-by-endpoint)需要管理Key且汇总已完成UTC日期，不适合现在定位该笔。核对前保持钱包暂停及未知预留；核对后按已消费/剩余额度和有效配置处理，不重放未知请求、不修改不可变policy、不增加累计预算。

完成标准仍为：实际JEV typed answer与usage.cost、持久预留/结算一致、Paper WAIT/拒绝/模拟成交结果、原用户状态不变、最终暂停。WAIT或低置信拒绝不能伪称成交。

## 证据

- `output/verification/jev-futures-preflight-20261008.txt`：免费历史、Key存在性、原预算0。
- `output/verification/jev-futures-real-20261008.txt/.json`：首轮公共报价失败、钱包暂停、模型与费用0、原会话未变。
- `output/verification/jev-futures-resume-preflight-20261008.txt`：同配置可恢复预检、服务器领先872.6ms、公共报价仍不可读。
- `output/verification/jev-futures-clock-synced-preflight-20261008.txt`：校时后quote/168根历史免费通过。
- `output/verification/jev-futures-real-resumed-20261008.txt/.json`：持久操盘设置缺失，0模型派发。
- `output/verification/jev-futures-real-final-20261008.txt/.json`：实际一次尝试、接口错误、unknown预留、最终暂停；名称final不表示验收通过。
- `output/verification/jev-futures-key-usage-20261008.json`：免费Key汇总usage0，未擅自释放预留。
- `output/verification/jev-futures-real-verification-20261008.json`：持久预留1项、原会话不变、新试跑在网络前拒绝。
- `output/verification/jev-futures-response-diagnostics-20261008.txt`：未来状态码记录四条离线路径、无Key/重试。
- `output/verification/jev-futures-exit-verification-20261008.json`：免费/已验收exit0、未验收execute为exit2；全部Fake，无真实推理。
- `output/verification/jev-futures-preview-preserved-20261008.json`：原8775页面/会话200，目标与style保留。
- 工具Ruff/format通过，免费单次上限两种路径检查通过；按用户预算没有重跑全套、安装依赖、派Agent或更新原预览进程。原8774和8775继续保留。
