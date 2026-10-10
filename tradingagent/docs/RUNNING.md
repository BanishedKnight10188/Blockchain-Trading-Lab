# 本地交易工作台

2026-10-07：新会话现已支持完整USDT永续目录和1/7/30天已收盘历史；当前持续实例8774沿用用户数据库和风格80，具体命令/本机代理见[SESSION_MARKET_RUNBOOK.md](SESSION_MARKET_RUNBOOK.md)。旧会话不会自动变为合约；合约Paper和真实Flash收费仍未装配。下段为原BTC现货Paper启动说明，其旧费用窗口已过期，不能用来自动重新授予预算。

独立JEV本地Paper已新增可启动/暂停的模拟后台；先按[JEV_PAPER_RUNBOOK.md](JEV_PAPER_RUNBOOK.md)使用`--paper --paper-mock`。真实JEV配置和费用约束已就绪，单次0.02 USD；累计预算与本机Key缺失时不开收费。下文默认Web仍不装配模型；“后台未接通”仅适用于旧设置阶段及未完成的Flash/建议、Agent OS/Testnet执行。

使用用户已安装的 tradingagent Conda 环境：

```powershell
conda activate tradingagent
Set-Location -LiteralPath 'D:\develop\tradingagent\Blockchain-Trading-Lab\tradingagent'
python -m agent_platform.cli web
```

打开 [本地工作台](http://127.0.0.1:8765/)。服务默认只监听本机。
两个主页面是[大盘与分析](http://127.0.0.1:8765/overview)与[JEV 操盘](http://127.0.0.1:8765/jev-trader)。会话与0–100风格、记录、复盘、系统状态保留为辅助入口。
默认启动不连接外网，行情和账户显示未启用；不会把缺失余额/价格填成零。
关闭浏览器不会关闭后端；终端按 Ctrl+C 停止服务。

## 两个主页面与独立开关

大盘与分析页保留行情、报价图、只读持仓与挂单，Flash是常态主分析；同页的JEV建议可单独开启或关闭，保存需明确确认。JEV操盘页有独立开关，并可选择给出建议或自动操盘（testnet）；不等待Flash或建议模块。关闭JEV建议不会关闭操盘，关闭操盘也不会关闭JEV建议。`/agent`只是大盘页的旧地址别名。

两个页面各自提交受保护的设置接口，保留另一模块配置及其版本；全局CAS版本与审计同事务保存，刷新和重启恢复，多页面冲突需重新载入。旧整组接口必须显式提供两个开关，省略操盘开关会拒绝请求；旧数据库中没有操盘字段时仍默认关闭。

当前实现页面、持久设置与派发门控，独立持仓后台、模型连接和testnet执行器尚未接通。页面显示未连接与执行未就绪；保存auto不会开始下单，预算仍为0，强模型不调用。完整模块关系见[JEV_PARALLEL_PRODUCT.md](JEV_PARALLEL_PRODUCT.md)。

新数据库的启动默认值可通过CLI指定：

```powershell
python -m agent_platform.cli web --no-jev --no-jev-trader
python -m agent_platform.cli web --jev --no-jev-trader
python -m agent_platform.cli web --mode auto --no-jev --jev-trader
```

`--jev`只选择建议模块，`--jev-trader`只选择操盘模块，`--mode`只选择操盘方式。**已保存设置优先于启动默认值**；改变已有选择请在对应网页确认。

目前运行装配仍只有一套生产只读数据来源，auto只接受testnet，暂不能与`--live-public/--live-account/--live-user-stream`混用。需恢复生产只读数据时先在操盘页保存给出建议方式、停服务，再启用生产读取。后续独立后台将分别使用生产分析与testnet操盘事实，才能同时运行这两种环境；当前未接通Agent OS或Direct Testnet。

设置复用核心库schema v7通用状态与审计，不改写旧事实；旧版本包无法读取新增状态类型。正式库启用新版前按OPERATIONS.md备份，当前开发验证仅使用独立测试库。

## 会话风格

1. 拖动0–100滑杆，0最保守，100最激进；方向键、Home/End也可操作。
2. 确认当前选择，点击“创建会话”。显示的初值50不会自动创建。
3. 后续拖动并再次确认，点击“保存风格”。每次实际修改形成新版本。
4. 刷新页面或重启服务恢复会话；“重新载入”可解决另一个页面修改造成的版本冲突。

已有一个未结束会话时只修改当前会话，不创建重复会话。
数值是判断风格，不是仓位比例。保存后点击“启动评估”；暂停/结束会话会撤去当前建议，数据只读同步继续。状态操作保留审计和风格版本，过期页面须重新载入。
Web建议与风险重验已接入。默认无模型/规则且日预算0；数据未就绪或策略未配置时明确不可评估，不能把它当作HOLD。真实模型供应商尚未配置；不提供下单/撤单/划转/提现入口。

## 可选择的强模型模块

模型主体框架已实现，当前分析/决策身份分别固定Flash/Jev。强模型可以保存选择，例如：

```powershell
python -m agent_platform.cli web --strong-model anthropic/claude-opus-5.5
```

该参数只设置当前进程配置，`/api/status`显示所选ID及`calls_enabled=false`；重启需沿用自己的启动参数。不会装配模型客户端、读取OpenRouter Key或增加模型费用，日预算仍为0。strong_model不能与Flash/Jev身份重叠；本版本没有开启强模型调用的开关。soak也支持该选择参数。
OpenRouter Chat/Decisions Adapter和Jev预算执行器已通过Mock HTTP/SQLite验证；CLI尚不装配实际模型运行，不能仅设置Key或JEV开关启用。当前环境已有httpx，无需安装；打包的models extra声明可选HTTP依赖，依赖仍由用户管理。

## 显式启用只读数据

公共行情不需要账户凭据：

```powershell
python -m agent_platform.cli web --live-public
```

账户读取需由你在该进程的本机运行环境中设置BINANCE_API_KEY和BINANCE_API_SECRET；
不要把凭据放进浏览器、命令行参数、项目文件或聊天。Key关闭交易/划转/提现权限。
随后可单独启用账户，或与公共行情一起启用：

```powershell
python -m agent_platform.cli web --live-public --live-account
```

两个开关相互独立；未启用账户时后端不读取凭据。
缺失/格式错误的账户配置会在启动前明确拒绝，不开启半配置的账户任务。
客户端固定GET白名单；即使Key意外有交易权限，程序仍没有资金写入路径。
以上是已实施启动入口，不代表当前机器真实WS/账户联调已通过。

后台只有一个公共行情消费者，1s生成最新帧，账户默认15s同步并遵守RetryAfter。
浏览器请求只读缓存；SSE慢读和重连只取最新帧，不积累历史队列。
报价、盘口分别按5s、账户/挂单按60s显示过期；断线时已确认事实保留时间和故障说明。
指标预热不足、持仓成本未知和Jev未接入保持明确状态；当前Prompt v2使用not_connected，历史v1/Replay以及尚未迁移的总览/离线smoke仍为unspecified。Jev专用Adapter已离线验证，发布级联与总览能力迁移待M3，详见[MODEL_SELECTION.md](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/docs/MODEL_SELECTION.md)。
本地存储失败停止账户写入；请恢复存储后重启服务。关闭浏览器不停止后台任务。

## 数据与验证

默认数据库：开发根目录 data/agent.sqlite3，包含会话状态及修改记录。
数据库不保存API Key、Secret或Token。
使用 --database 可指定独立文件，用于隔离开发预览和实际个人设置。
本轮浏览器测试使用 output/verification/browser-preview.sqlite3，测试值不作为个人策略。

```powershell
$env:PYTHONUTF8 = '1'
python -m pytest -q
python -m ruff check .
```

只读与建议装配已通过Fake/录制响应→SQLite→Web离线验证。建议显示原始风格版本/证据时间/有效期；过期、风格或账户变更后停止显示为当前行动。模型在途费用未结算，UNKNOWN保留预留，账本不可读不宣称零费用。
反馈/归属/冻结复盘/回访已完成离线验证；真实Binance/供应商联调与实际24小时仍待配置。边界与证据见[验收报告](ACCEPTANCE_REPORT.md)。

完整工作台导航：/overview总览、/会话与风格、/records记录与归属、/reviews复盘版本与回访、/status数据和预算状态。写入口沿用浏览器cookie、Origin、CSRF，并要求confirmed=true；按钮只有明确确认后才可用。
POST /api/feedback保存采纳/拒绝/修改/独立想法；采纳重验当前建议和风险，记录不表示已执行。
POST /api/attributions使用operation_id和expected_revision追加真实成交来源或纠正；不会按时间自动猜测。
POST /api/reports保存USER_REPORTED；POST /api/reports/{report_id}/verify只检查本地已导入的明确成交ID。缺失ID待核实，字段矛盾冲突，核实不覆盖原报告或账户余额。
GET /api/trading-records支持after_sequence游标与1–50条分页，明确disabled/fake来源，不输出账户敏感标识。

复盘页先填写分组名称并确认冻结截止前已实际导入的本地BTCUSDT窗口（最多4096笔），再生成初始/手工/事后版本；后续成交不改写旧分组。人工报告不直接参与真实FIFO。组列表每页最多50，版本最多10，每版本成交检查每次20笔，按钮继续分页；回访任务明确仅显示前50项。
成本缺少完整历史、期初库存或BTC资金移动覆盖时保持UNKNOWN/PARTIAL；不把USDT当作USD、不虚构完整盈亏。原建议方向/期限/风格/费用冻结保存；缺成交时账户或整单证据的纪律/数量检查不可评估。
回访由用户明确选择成交结束后1h/24h；以组内最后成交executed_at计算，已过期的新任务拒绝，可改用手工回访。任务与复盘同事务完成，重启不会产生同键重复版本。后见行情只引用截止前已实际提交的本地决策快照，缺失为null。默认规则复盘免费、模型未参与，不修改会话风格。

总览SVG仅保留当前浏览器收到的最近120个有效报价，数据缺口处断开；刷新后重新积累，没有历史回放或完整K线含义。金额/费用仍按原Decimal字符串显示。系统页的“重新载入”读取最新诊断，证据时间与费用采样时间各自保留；预算读失败明确不可读，不将未知费用填0。

## 离线回放

```powershell
python -m agent_platform.cli replay --input tests/fixtures/btc_events.jsonl --style 72 --threshold 60000 --output output/replay/demo.json
```

style必须显式提供0–100整数。threshold是明确的M0测试价格阈值，不能当成个人交易策略。
报告保留每次快照/原风格/评估，JEV=unspecified、特征未预热；不发布真实建议或发起模型收费。
已有输出文件不会被覆盖，请换文件名。坏JSON/UTF-8输入返回行号，不回显原内容。
命令默认ADVISORY，不模拟成交。PaperSimulator及ReplayRunner的显式PAPER接口已通过离线验证，使用独立paper:余额与PaperIntent；当前工作台面向辅助交易，未提供paper页面或paper余额的跨进程持久恢复。

## 运行维护与环境快照

可选私流用`web --live-public --live-account --live-user-stream`显式启用；仅提示REST对账，不能直接更新余额。公共行情默认写独立辅助库，`--no-market-archive`可关闭；默认离线模式不创建行情库。日志固定字段、1MiB加3份轮转，系统页显示健康与降级。

在线备份和短测入口：

Web与soak可加`--raw-retention-days 3 --minute-retention-days 120`，分别配置1–365天整数；默认7/90天，系统页显示实际值。仅改变辅助行情保留，已固定记录/永久核心审计不清理。重启时沿用自己的启动参数。

```powershell
python -m agent_platform.cli backup --database data/agent.sqlite3 --output data/backups/core-new.sqlite3 --kind core
python -m agent_platform.cli soak --seconds 60 --database output/offline-run.sqlite3 --output output/offline-run.json
```

先创建备份目录，输出使用新文件名。`soak`默认离线；真实24小时需要独立显式联网开关及已验证账户/网络。不要与同核心路径的Web同时运行。完整保留策略、备份/恢复、Windows退出和24小时验收条件见[运行维护手册](OPERATIONS.md)。

requirements.lock.txt记录本机已安装的29个直接/传递依赖版本，生成时pip check通过；它是环境版本快照，不是完整Conda求解锁。当前开发不安装或升级包，后续依赖仍由用户管理。

## 公共行情诊断

只有显式传入--live-public才访问固定的Binance公共行情端点；不使用密钥或账户。

```powershell
python -m agent_platform.cli market-probe --live-public --seconds 25 --output output/verification/public-probe-new.json
```

1–60秒，已有输出不覆盖。先读取服务器时间并检查5秒时差，再启动REST分钟预热和WS；报告保存最近实际快照、指标和脱敏传输状态。
captured表示收到公共事件，是否预热完成另看features.warmup_ready；no_data/blocked不代表联调成功。
当前机器REST时间读取已成功，WS尚未建立连接。改善该机器到固定WS域名的网络后再重试；不需要安装新依赖。
该命令用于有界开发诊断；Web长期读取使用上述显式启动开关，不会在默认启动时重复网络诊断。
