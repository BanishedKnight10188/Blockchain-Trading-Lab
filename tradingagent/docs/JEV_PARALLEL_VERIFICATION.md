# 三模块与两主页面阶段验证

2026-10-06，开发目录内实施；正式解释器`C:/Users/exile/anaconda3/envs/tradingagent/python.exe`，不安装或升级依赖。

## 已交付范围

`/overview`保留行情/报价图/只读持仓与挂单，Flash常态分析区和可选JEV建议同页；`/jev-trader`独立操盘开关与建议/auto方式、testnet环境和执行状态。`/agent`为总览别名。会话0–100风格、记录、复盘、系统状态仍是辅助入口。

建议与操盘各有开关及配置版本，分别通过`/api/controls/advice`和`/api/controls/trader`保存；另一模块的设置/版本保留。确认、Origin/CSRF/cookie、CAS与审计同事务、重启恢复沿用已验证基础。Flash配置始终continuous，强模型仍不调用。

## 证据

| 检查 | 结果与本地证据（output/verification下） |
|---|---|
| 初始拆分RED→专项GREEN | jev-three-modules-red-20261006.txt、jev-three-modules-combined-green-20261006.txt；83专项通过 |
| 复核补充RED→GREEN | jev-three-modules-review-red-20261006.txt真实3失败；jev-three-modules-review-green-20261006.txt相关82项通过 |
| 最终全套 | jev-three-modules-reviewed-final-suite-20261006.txt；1113 passed、137 subtests passed、198.64s；1条既有Starlette/httpx弃用提示 |
| 真实JS逻辑 | node jev-three-modules-ui-check.cjs；两个原始JS作用域请求、保存/刷新竞态、乱序读丢弃、失败恢复通过 |
| 独立复核 | 15专项通过；原始workbench/两模块JS及总览脚本顺序、SSE/报价共存的内存DOM验证通过；复核后无剩余P1/P2 |
| 浏览器 | jev-three-modules-browser-20261006.json；建议开→操盘auto开→建议关/刷新→操盘仍开且版本不变；console error为空 |
| 页面截图 | jev-three-modules-overview-desktop.jpg、jev-three-modules-trader-desktop.jpg；独立离线预览库，未修改用户数据库 |
| 打包/隔离运行 | jev-three-modules-reviewed-wheel-build-20261006.txt、jev-three-modules-reviewed-wheel-smoke-20261006.txt；Python -I导入实际wheel，两页/资产、独立保存/重启、Mock HTTP与SQLite费用、停止状态通过 |

最终wheel：`output/verification/wheels-jev-three-modules-reviewed/btc_agent_platform-0.1.0-py3-none-any.whl`，**260604 bytes**，SHA256 `8DD152D99A1373CE9F381E052471F3B3FF2B7518EA5CE0CA778FD970A58FB3C4`。

复核发现并修复：旧整组API省略trader_enabled会隐式关闭操盘，现在拒绝含糊请求；scoped方法第一次读取旧状态、future expected_revision恰好匹配第二次读取时会覆盖另一模块，现在首次快照版本与最终CAS均验证。旧持久状态仍默认trader=false，不将旧建议开关转换成操盘权限。

隔离wheel首次验证发现历史setuptools build目录残留本轮已删除的agent.html/js；已核对工作区绝对路径后，将旧build移动到独立证据目录保留，重新打包。新wheel明确验证两个旧资产不存在，未覆盖历史wheel。此问题是打包缓存残留，不将首次失败算作验收通过。

README SHA256保持`E670ED4212C6241F3FDF9B68F6868046FCC92261063F11B09D73A87898796EDD`。无Git提交、推送、部署、收费模型请求或订单调用。

## 尚未完成

设置和页面完成不表示模型或自动操盘已运行。独立三lane后台、JEV结果发布、Agent OS工具/账户映射、testnet持久订单与UNKNOWN查询对账均待JI2/JI4/JI5。当前paid=false、writes_enabled=false，模型未连接，无实时判断或订单。

当前仅有一套可选生产只读来源，auto仍拒绝混用该来源；JI2必须分别明确生产分析与testnet操盘事实scope，不能用生产账户持仓生成测试网卖单。真实24小时、网络/账户/供应商/策略限额联调未验收。历史1105结果/旧页截图/旧wheel保持原范围。
