# 首版验收记录

用户最后的三模块/两主页面澄清已完成JI3B设置与页面阶段：大盘/Flash/可选JEV建议同页，独立JEV操盘另页；独立开关、版本与审计重启恢复已验证。最终1113测试/137subtests及实际页面、新wheel、独立复核证据见[JEV_PARALLEL_VERIFICATION.md](JEV_PARALLEL_VERIFICATION.md)。此前1105测试和旧设置页wheel保留中间范围。当前尚无真实模型/独立后台/Agent OS/testnet执行验收。

修订：2026-10-06，开发根目录D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent。新增Jev独立开关/两模式与持久Web设置；历史验收证据保留原范围。独立后台/实际模型/Agent OS与testnet执行另列缺口，完整回归证据见DEVELOPMENT_STATUS.md。

结论：T01–T16历史离线范围、F1–F4模型主体及JI1/JI3A/JI3B设置与两页阶段通过；**真实首版及新增自动操盘未验收**。JI2独立持仓运行、JI4 Agent OS映射、JI5持久订单/对账/执行器、策略纪律与价格预算均待开发或联调。默认本地Web不联网，无策略/收费模型或执行器；auto设置仍writes_enabled=false。强模型只选择、不调用。

## 自动化与实际本机证据

- 上一验证点完整 `python -m pytest -q`：975 passed、129 subtests passed、103.54秒；Ruff/232文件format通过。日志output/verification/t15-t16-retention-final-suite-20261005.txt。
- 新主体专项137项通过，独立复核7项发现已实际RED→GREEN修复，含强模型角色绕过/预留、Unicode Key回显、typed答案/模型版本、重复取消/锁等待费用、Chat损坏形状和返回前价格失效。复核记录output/verification/model-framework-review-20261006.md。
- 本轮最终全套1056 passed/134 subtests passed、223.67秒，1条既有弃用提示；Ruff check .与280文件format通过，日志model-framework-final-suite-20261006.txt。首次1055 passed/1 failed是旧恢复测试混用Fake/宿主机时钟导致回访到期，统一测试时钟后相关17项及完整回归通过；产品调度不变，保留model-framework-first-suite-20261006.txt。
- 新wheel251621 bytes，SHA256 149BCC7CD476A992483F964A46789B4EA5AB2A86B8697C361C4A3300D58A8BB1；output/verification/wheels-model-framework/btc_agent_platform-0.1.0-py3-none-any.whl。正式Python -I隔离验证模块来源、默认Web/强选择0预算、Flash/Jev Mock HTTP与SQLite费用、runtime停止通过；model-framework-wheel-smoke-20261006.txt。未安装项目/依赖。
- 原954项回归后补UDP/隐藏路由RED→GREEN形成956项；规格对照再补保留时长可配置19项RED→GREEN并独立复核，运行上述最终975项。第三方Starlette/httpx既有1条弃用提示保留，未安装或升级依赖。
- 最终wheel236848 bytes，SHA256 `BFBF83974EF213EF0494BC8386CF563AEDCCB2765C9187FF3669E9885F5B17EE`；output/verification/wheels-t15-t16-retention/btc_agent_platform-0.1.0-py3-none-any.whl。以no-index/no-deps/no-build-isolation构建，不安装构建依赖；旧wheel留作上一验证点。
- 正式Python `-I`隔离解包检查20个新旧模块路径、页面/静态资源、保护写接口、实际SQLite冻结复盘、safe预算/健康/所选5天20天配置、sidecar1天2天实际清理/pin/在线备份、1秒实际短测、offline acceptance及worker停止通过；output/verification/wheel-t15-t16-retention-smoke.py与output/verification/t15-t16-retention-wheel-smoke-20261005.txt可复现。
- MarketRetentionPolicy的默认7/90天与严格1–365天可分别配置；web/soak均传入真实辅助清理并在状态/运行报告显示。19项新增测试与相关61项/94subtests通过，实际status.js在3/120及365/1两组值下显示所选天数；t15-retention-status-check.cjs/green.txt。脚本首次未等待VM中的实际加载Promise已纠正，不计产品RED。核心审计与pin仍永久保留。
- 本地smoke `run_acceptance(RunMode.ADVISORY, database_path=...)`返回7项实际组装检查通过、AUTOMATED_OFFLINE/local_assembly_smoke/passed_with_gaps。output/verification/t16-offline-acceptance-20261005.json。此结果不是全部pytest或真实交易验收；formal_acceptance固定false。真实/PAPER模式不能冒充这个离线smoke入口。
- 两次实际独立进程重开合成事实库：原strength78/style_revision2、1笔成交、游标、复盘、24h任务、UNKNOWN0.3USD/小时计数1均保留，新实时账户not_connected；worker正常停止。t15-process-recovery.py/json。初次脚本路径及style字段名纠正是验证脚本问题，不当产品RED。
- 该核心库CLI在线备份完整性通过，192512 bytes、20个Journal事件，SHA256 `6660843D255C7E3BA6F0E50A8AF42658F22ACD3D69620F49F777792E74EAF69F`。备份后的恢复在SQLite/隔离wheel检查中验证；未将合成库当用户真实账户。
- 实际前台Web PID42936经WindowsCtrl+Break停止，Uvicorn确认Application shutdown complete且PID消失；shell中断exit1如实保留，不称普通exit0。PTY写Ctrl+C字符未传递信号。诊断runtime_stopped是日志worker停止阶段，仍需看health与Uvicorn/PID确认全部退出。
- 实际disabled短测3.031秒、2次采样、工作集64655360→64671744 bytes、0模型请求/费用、shutdown stopped；报告明确offline_short_check/formal_acceptance=false，不是24h。t15-offline-short-20261005.json。
- 用户测试预览最终更新PID13644/session79342，http://127.0.0.1:8765/；实际浏览器重开确认原测试风格1/v2/history2和paused未改，系统4个组件运行/归档未启用/默认7天90天/日志轮转，console error为空。最终截图t15-retention-final-status.png、t15-retention-final-session.png；之前304px document289无整页溢出及desktop1280截图见t15-status-304.jpg/desktop.jpg，布局未改。T14完整操作/恢复证据仍见t14-*。
- requirements.lock.txt捕获已安装Conda Python3.12.14的29个直接/传递runtime、market、dev/build版本；无URL/凭据、无安装。pip check通过。不是完整Conda求解锁或所有平台保证。

## Review Focus覆盖

| 总计划Review Focus | 具名验证 | 范围/结果 |
| --- | --- | --- |
| 成交靠近建议、人工修改、重复导入 | application/test_attribution/test_feedback/test_reports/test_reviews；adapters/test_sqlite_observations | 明确归属、保留原文、追加纠正、幂等导入/冻结；不按相近时间猜作者 |
| WS乱序/重复/断线、过期账户、成本缺口 | market/test_normalizer/test_features/test_market_stream；application/test_account_sync/test_risk/test_trade_groups；runtime/test_read_only | 缺失/过期拒绝增加风险，FIFO保持UNKNOWN/PARTIAL；未证明实时网络/完整库存及实际PnL |
| 风格或账户变化时模型在途 | application/test_advice_queries/test_decision_service；adapters/test_sqlite_decisions；runtime/test_decisions | 冻结原strength/version、发布前当前事实/TTL重验，过期结果撤去当前行动，保留原费用 |
| 超时/供应商故障/日切/重启 | application/test_routing/test_decision_service；adapters/test_sqlite_budgets；runtime/test_recovery/test_supervisor；跨进程证据 | 持久预留/UNKNOWN/小时额度、不重发原claim、超估冻结、恢复事实；供应商为Fake/录制响应 |
| Schema变化/恶意输出/意外高权限 | adapters/test_mcp_discovery/test_user_stream及只读client；application/test_prompting；integration/test_security/test_offline_slice；Web保护测试 | Schema哈希与metadata无权限、固定GET、有限Prompt、cookie/Host/Origin/CSRF、隐藏资金路由也拒绝；没有实际MCP/Key权限实测 |

附加基础检查：tests/architecture/test_dependency_boundaries.py与隔离wheel验证层级独立/模块来源；adapters/test_owned_io验证连续取消IO所有权；adapters/test_retention/test_operations及runtime/test_market_archive/test_diagnostics/test_soak验证精确保留/pin、schema隔离/回补ID哈希、轮转、固定read snapshot备份和有界退出。它们不替代上述五条或实际24h。

测试主进程默认阻止外部DNS/TCP/UDP，允许用于本机验证的loopback。Fake网络门控没有发送真实UDP包。此pytest保护不是操作系统防火墙；子进程/手工真实诊断仍须各自明确范围。资金路由检查还拒绝未知Mount/路由，不只检查OpenAPI可见部分。

## 真实与产品配置缺口

| 缺口 | 当前证据 | 接续条件 |
| --- | --- | --- |
| Binance公共WS | REST服务器时间曾读取成功；25秒WS0事件/两次连接失败 | 当前主机网络条件改变后重新显式诊断，不反复同条件空跑 |
| Binance账户/USER_STREAM | Fake/录制响应/签名GET白名单通过；真实Key权限未联调 | 用户在本机提供只读配置并确认scope/权限，密钥不发送聊天；先REST，私流额外opt-in |
| 模型发布链/价格/非零预算 | Flash/Jev HTTP Adapter与typed预算执行器已Mock HTTP/SQLite验证；默认启动未装配，日预算0 | M3发布级联及M4配置/评测；生产provider/有效价格与费用上限确认后才M5小额联调。强模型本版本无调用开关 |
| Agent OS MCP | 清单/schema哈希/能力默认空通过 | 实际SDK与程序授权、具体只读工具Schema、主账户scope实测；metadata不授权资金能力 |
| Jev/策略/资金纪律 | 独立Port/typed契约/Decisions Adapter已离线验证；Prompt v2为not_connected，真实连接/语义评测未验证；数量限制未配置拒绝数量建议 | M3候选判断/发布链及M4问题/标准评测；用户确定策略、个人约束与证据供应。当前总览/smoke旧unspecified标记在M3迁移，历史保留原义 |
| 成本与完整PnL | FIFO/固定核对通过；生产InventoryCoverage未接完整历史/期初/移动/FX | 提供完整证据，UNKNOWN/PARTIAL不改为虚构成本/USDT当USD |
| 连续运行与宿主机 | CLI、实际短测/退出/持久恢复已验证 | 设备/网络/不休眠、真实24小时、采样与重启日志逐项复核；未安装Windows后台服务 |

源代码/手册/wheel/锁定快照可本机审查。未提交、推送、部署、安装包、真实下单/划转/提现或调用收费模型。用户README SHA256保持 `E670ED4212C6241F3FDF9B68F6868046FCC92261063F11B09D73A87898796EDD`。

OpenRouter/Flash/Jev的明确选择已形成新增开发条件，M1–M4离线接入不等待Key。真实联调仍按上述限额/网络/权限/证据条件推进；不重复原已验收测试或同条件WS诊断。10分钟接续仍保留，全部真实与离线任务完成前不标首版通过或停用自动化。方案见MODEL_SELECTION.md。
