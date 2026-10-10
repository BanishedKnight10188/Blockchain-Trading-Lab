# USDT 永续会话验证记录

2026-10-07，正式 tradingagent Conda Python3.12。所有测试与真实公共探测分开记录；没有安装或升级依赖、收费模型请求、私人账户读取或交易所订单。

恢复后持续服务仍是8774及原 `data/jev-paper-20261007.sqlite3`，当前PID3756、日志 `market-types-persistent-server-20261007.txt`；只读状态证据 `market-types-persistent-status-20261007.json` 确认原Spot/BTCUSDT会话PAUSED、风格80/v1、钱包未配置、模型和真实订单关闭。没有自动恢复用户操盘。旧PID及running记录仅属历史。新合约Paper内核尚未装配到此服务，其离线验收单独记录。

## 恢复后的现货/合约标签补齐

用户“继续”后接续暂停的 RED。`market-types-resume-red-20261007.txt` 为2 failed/21 passed；`market-types-green-final-20261007.txt` 为28 passed。中间 GREEN 一项失败保留：字段误放模型请求，已撤销并移到查询。Node 市场默认、合约隐藏现货区及身份错误保护均有实际 RED→GREEN 日志。旧钱包 latest 已过滤 closed；历史保留断言纠正为 get(account_ref)，不将该 fixture 称为泄漏发现。

最新完整 `market-types-final-suite-20261007.txt` **1229 passed / 145 subtests passed，296.26秒**，只有既有 Starlette 弃用提示。Ruff、289文件format及JS语法通过，quote_chart_checks.cjs 的13项通过，session_analysis_ui.cjs通过。报价图首次文件名误写不计产品失败。独立只读复核无新增 P1/P2，补充旧成功/失败响应晚到不覆盖新市场的检查通过。

隔离预览 `market-types-preview-20261007.sqlite3`，测试 ETHUSDT/style80，不接公共/收费/订单；实际 JEV 页显示 ETHUSDT·USDT永续及合约模拟待接入，DOM确认 spot-paper-controls/history hidden。截图 `market-types-futures-ui-20261007.png`。测试会话与用户原持仓无关。市场钱包/执行模块未因此装配，当前1229验证只覆盖已实现范围。

## 已实现

会话不可变分析身份；全部 USDT 永续动态目录；1/7/30天完整已收盘历史；独立首次分析 Port/Flash Adapter/持久任务与后台；Web币种搜索和历史曲线；旧 Spot/账户/Paper 隔离；可选本机代理参数。

SQLite新增自己拥有的首次分析表，未改写旧会话body或核心schema v7。首次任务原子claim和完成CAS；OS文件锁贯穿首次后台生命周期，第二个实例拒绝启动后才允许恢复，进程退出自动释放。未配置模型的历史准备可在明确模型装配后接续；未知费用/失败/中断不自动再发。

## TDD与回归

- `session-market-red/green-20261007.txt`：身份、目录、完整窗口、分页、异常和限流。真实中文合约另有 `futures-unicode-red-20261007.txt` 回归；不会只维护英语币种白名单。
- `initial-analysis-guard-red-20261007.txt`：真实触碰旧Spot钱包缺陷，修复后拒绝合约会话。第一次 fixture严格参数/async标注错误只属于测试准备问题，不冒称产品RED。
- `initial-analysis-hardening-red-20261007.txt`：30天上下文过大与错误币种历史处理；压缩OHLCV上下文并清除错误scope后相关测试通过。
- `session-market-web-red/green-20261007.txt`：保护API、完整目录验证、不可变target和旧会话兼容。`futures-overview-scope-red-20261007.txt` 验证合约总览不投影Spot来源。
- `initial-flash-red-20261007.txt`：可选Flash适配器缺失。Mock HTTP和真实SQLite预算7项通过；并非真实provider价格或收费响应验证。
- `initial-analysis-prepared-red-20261007.txt`：以后显式装配模型可完成此前仅准备历史的未收费任务。相关39项通过。
- 全套中间 `session-market-full-suite-20261007.txt` 有3失败（2个旧worker数量断言、1个预算重复派发异常断言）；修正测试契约后 `session-market-full-final-20261007.txt` 实际1219项/145subtests通过（116.35秒）。这不是最后自查修复后的全套证据。
- 独立复核发现三个P2：暂停仍发布/展示、第二worker恢复抢占在途任务、身份查询失败保留旧页面。`initial-analysis-review-red-20261007.txt` 真实4失败（含脱敏诊断），修复后21项通过；`initial-analysis-ui-red/green-20261007.txt` 是Node页面身份错误/恢复实际回归。
- 自查后的第一次全套 `session-market-reviewed-final-suite-20261007.txt` 实际3失败/1221通过；`test_invalid_retention_cli_fails_before_storage_or_server` 两个web参数用例及 `test_cli_missing_account_credentials_fails_before_server_or_database` 的子进程中文stderr被UTF-8解码失败。该次未给子进程继承PYTHONUTF8，并非功能修复通过证据。
- 指定项目UTF-8环境后 `session-market-reviewed-utf8-suite-20261007.txt` 实际 **1224 passed / 145 subtests passed，124.76秒**，仅既有Starlette测试客户端弃用提示。产品代码不再修改；同轮额外响应边界补验 `futures-response-bounds-20261007.txt` **22 passed**（包括新补的重复JSON、4MiB上限、压缩拒绝3项，只是补充验证，不声称测试先行RED）。
- Ruff check通过，289个Python文件format通过；session/session-analysis/overview/paper-trader的Node语法检查通过，原报价图13项与新页面身份恢复检查通过。README SHA256仍为 `E670ED4212C6241F3FDF9B68F6868046FCC92261063F11B09D73A87898796EDD`。没有新wheel或提交/推送。

## 真实公共数据

直连合约域名连接超时，现货公共域名可读。读取用户既有代理配置后只对本程序显式使用本机7897；没有修改系统网络。真实 Provider 动态读取525个符合条件合约，包含中文符号；ETHUSDT和SOLUSDT各168根真实完整1h K线，范围2026-09-30 09:00 UTC到2026-10-07 09:00 UTC（不含结束时刻）。完整原始公共数据/摘要在 `futures-public-proxy-20261007.json`。

隔离预览数据库 `futures-preview-20261007.sqlite3` 预置测试ETH会话，直接连接公共接口，model=None。实际浏览器验证ETH标题、168点曲线、历史来源与“模型未配置”、Spot区域隐藏、无console error；截图 `futures-history-ui-20261007.jpg`。这个测试会话不是用户持仓，没有模型分析或模拟成交；临时预览已关闭。

持续8774沿用 `data/jev-paper-20261007.sqlite3`：用户会话ID `3e321aaecfea46c798193833d45228a8`、风格80/v1与running状态保留，旧会话解码为spot/BTCUSDT。最新受保护GET目录200/525，证据 `futures-persistent-review-status-20261007.json`；浏览器当前会话页显示525个合约、原风格80与锁定旧target，console error为空，截图 `usdt-session-current-20261007.jpg`。重启前曾有503，原请求未含细分诊断；最新版本重启后未再现，不能断言已证实当时503根因。

## 明确缺口

真正Flash首次分析未收费联调；强模型固定不调用；合约实时流/USDT合约账户只读、合约Paper钱包/风控与Testnet未装配；24h运行未验证。已到期JEV首次配置不续期，不启动实际资金操作。
