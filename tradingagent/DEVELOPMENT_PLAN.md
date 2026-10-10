# Trading Agent Development Plan v0.6

2026-10-08 当前优先JEV：多会话工作台（左栏/三步向导/同屏工作与档案）已实现，后台并行JEV建议或自动Paper，独立钱包与状态、共享原预算，旧BTC/OGN保留。新4项与既有合约核心27项及静态验证通过，浏览器完成离线双会话创建/暂停隔离；未额外收费，不称真实并行/24h/自主成交验收。接续[工作台实施计划](docs/superpowers/plans/2026-10-08-jev-workbench.md)与[开发状态](docs/DEVELOPMENT_STATUS.md)。

用户最新常规Agent预设为“近期交易情况→LLM codewriting→定时分析脚本持续监测”，用于节省费用与降低延迟。仅记入[未来方案](docs/REGULAR_AGENT_SCRIPT_PLAN.md)，当前不实现常规Agent后台，重点继续JEV持仓/真实运行与后续Testnet。

2026-10-08 20:00上海：优先1/2，秒级真实有界联调与完整模拟操盘主体/严格逐笔档案已实现。最终8/8WAIT，派发1.047–1.063秒、返回0.719–1.219秒；本轮25请求24WAIT1拒绝/0.007059234USD。离线6路径16成交覆盖多空开加减平/盈亏退出，真实自主成交/长稳未验收。同事务档案、完整模型证据/前后资金、保护分页/JSONL/CSV/链核对已接页面，63相关/静态通过。公共50ms误差边界保留原时间，超界拒绝。8776原库1000USDT空仓暂停、8775保留，预算/配置未改，剩余0.088320682USD。[证据与下一步](docs/JEV_PAPER_ARCHIVE.md)。下文未秒级收费联调为旧阶段。

2026-10-08 18:41上海接续：持久校时已由用户管理员安装，实际Cloudflare网络源、64秒同步、偏差8.5849ms；免费公共WS复测8/8有效，当前行情时序阻碍解除。钱包仍1000USDT空仓暂停，无新增收费。下一真实JEV每秒发起的实际延迟/并发/费用、v3参数成交及自主退出、独立调杠杆与连续Paper；长期校时/运行稳定未验收。下文“尚未Install/时差阻断”属于安装前状态。

2026-10-08最新：JEV合约通道已改每1秒发起/3并行/3秒有效期，执行串行、旧结果拒绝；公共WS网络连通，8776 PID46588/session37217同库暂停、原费用/1000USDT空仓保留。真实行情被本机约0.415秒时差阻断，用户要求持久修复；maintain-trading-clock.ps1已准备原生64秒NTP/备份/还原，需管理员首次Install，尚未实际安装或收费验证1秒性能。[秒级规格](docs/JEV_ONE_SECOND.md)、[持续校时](docs/TRADING_CLOCK_MAINTENANCE.md)。下文60秒周期由本项覆盖。

2026-10-08当前：止盈止损时机由JEV主动决策，不要求固定比例/价格；持仓上下文与均价/盈亏、部分减仓/全平链路及离线多空退出已接。当前参数问题集v3；保护挂单和更快触发属于后续加强。动态杠杆/无到期累计费用已迁移，8776同库PID43956仍paused/1000USDT空仓，原费用保留。下一行情维护稳定后真实参数与自主退出观察、长稳及Testnet。详见[自主止盈止损](docs/JEV_MANAGED_EXITS.md)与[当前状态](docs/DEVELOPMENT_STATUS.md)；下文是前阶段记录。

2026-10-08最新：安全诊断、模型身份失败暂停、费用传播与显式只读加载完成（相关94passed）；8776已加载新版/旧钱包固定模式暂停，原8775与费用/授权保留。下一公共行情维护定位、独立调杠杆/止盈止损及新参数会话v2联调，详见[诊断与只读验收](docs/JEV_DIAGNOSTICS_READ_ONLY.md)。下文“服务未重启”是前阶段记录。

2026-10-08参数决策接续：已实现JEV完整仓位/杠杆方案选择，开仓按权益保证金比例，加减按当前数量；v2计划和通用命令可审计，Paper保证金/成交原子执行、旧固定模式兼容。相关121项、最新资金52项通过，无收费或旧服务重启。下一加载新代码/新参数会话真实联调、独立调杠杆和保护单；[规格](docs/JEV_PARAMETER_DECISIONS.md)、[实现](docs/JEV_PARAMETER_IMPLEMENTATION.md)。

2026-10-08 15:37最新：真实JEV已进入通用Paper后台，7轮有效WAIT、暂停/恢复/费用闭环及真实传输失败自动暂停通过；2次已收费结果未通过决策校验，下一优先响应分阶段诊断与代理/行情时序稳定。8776独立钱包/共享原费用账本，原8775总览与Mock保留；当前钱包暂停rev9、1000USDT空仓，总确认费用0.002459016USD、unknown预留0.002161068USD。同日更低封顶0.10USD/单次0.02USD、15:53:27上海到期，不自动续期。关键85项、九类失败9项及启动路由2项通过，未重跑全套。当前状态 [DEVELOPMENT_STATUS.md](docs/DEVELOPMENT_STATUS.md)，真实成交/长稳/Testnet/Agent OS仍待验收。

2026-10-08最新：合约持续后台、独立JEV闭环与保护Web核心已装配。按用户预算减少测试，精简80/123subtests通过，不重复全套/独立review。隔离8775预览公共行情+Mock WAIT，原用户DB/到期费用不动。运行与范围 [FUTURES_TRADING_CORE.md](docs/FUTURES_TRADING_CORE.md)，未完成项 [REMAINING_WORK.md](docs/REMAINING_WORK.md)；下文未装配文字保留前阶段历史。

2026-10-08执行基础TG1–TG4已验收：中立行情/订单事实、执行与账户Ports、Paper后端、持久通用提交/查询恢复；最终1450项/154subtests，Ruff/308文件format通过。验收 [TRADING_EXECUTION_VERIFICATION.md](docs/TRADING_EXECUTION_VERIFICATION.md)。下一持续后台/后端维护、共用风控与独立JEV，再Web；没有自动启用用户操盘。下文“尚未实现”是前次规划历史。

2026-10-08当前架构方向：先开发通用USDT合约交易主体，共用JEV决策、确定性纪律、订单生命周期、审计与运行控制；行情源和执行/账户后端分别可替换，已验收Paper内核作为首个后端，Testnet/Agent OS作为后续后端。设计 [TRADING_CORE_ARCHITECTURE.md](docs/TRADING_CORE_ARCHITECTURE.md)及[当前剩余任务](docs/REMAINING_WORK.md)覆盖下文专属Paper后台顺序。本轮仅改规划，通用接口/服务尚未实现，不改变收费、用户会话或真实资金授权。

2026-10-07数据层基线：合约公共mark/book/交易filters/已结算资金费FM1–FM3完成，真实ETH/SOL全部通过；完整1372项/150subtests。验收 [FUTURES_MARKET_VERIFICATION.md](docs/FUTURES_MARKET_VERIFICATION.md)。Provider完成不代表页面已自动操盘。

2026-10-07最新：独立USDT合约Paper FP1–FP3离线内核/SQLite完成，多空逐仓、杠杆/资金费/清算、版本门控/幂等/恢复与安全行情水位。专项60、完整1289项/148subtests通过；[内核验收](docs/FUTURES_PAPER_KERNEL_VERIFICATION.md)。下一任务是合约实时mark/book/已结算资金费/filters，随后独立JEV候选与合约Paper后台/Web；当前8774保留原Spot暂停会话，新内核未装配，不能称已合约自动操盘。费用配置到期不续期，旧阶段结论仅属历史。

2026-10-07用户“继续”后已完成现货/合约身份与默认补齐：JEV合约页面隐藏现货钱包/成交，所有Paper查询明确市场；最新1229项/145subtests通过。接续独立USDT合约Paper规格与离线内核；当前页面仍明确待接入，费用配置不自动续期。

2026-10-07最新接续覆盖下文历史默认：用户确定全部可交易USDT永续、市场/账户金额/模拟钱包统一USDT。多币种会话、1/7/30天已收盘历史、独立首次Flash分析Port/Adapter/持久任务和Web已实现并完成公共数据实测，详情[SESSION_MARKET_IMPLEMENTATION.md](docs/SESSION_MARKET_IMPLEMENTATION.md)、[SESSION_MARKET_VERIFICATION.md](docs/SESSION_MARKET_VERIFICATION.md)。当前8774保留原会话80/v1、数据库与费用事实；真实Flash未装配。下一任务是USDT合约Paper领域/钱包/风险（LONG/SHORT、保证金、杠杆、reduce-only、资金费、强平、重启）与合约实时行情，再接JEV独立决策、Testnet/Agent OS。合约只读账户仍需新增Port和只读联调，不能拿Spot余额充当合约权益。收费、强模型、实际资金操作均未开启。此前JEV费用窗口已到期，不能自动续期。运行见[SESSION_MARKET_RUNBOOK.md](docs/SESSION_MARKET_RUNBOOK.md)，旧下段阶段状态不再作为当前任务清单。

2026-10-07最新接续：先完成独立JEV本地Paper，再做Binance Testnet。JP1–JP4与JP5离线已验收（1177项/141subtests，实际Mock循环和隔离wheel）；依据docs/JEV_PAPER_SPEC.md，接续docs/JEV_PAPER_IMPLEMENTATION.md。首次累计1 USD、单次0.02、本机新Key与费用配置prepare通过；用户即时校正成功，25秒公共行情24849事件/120根K线/ready。8774已接续同一DB的持续本地服务，风格80/v1已由用户在页面确认、钱包未配置/模型费用0。总览盘口图表空白已修复，13个JS检查/23个Web专项及真实浏览器57点通过；gap/指标warming仍出现，保留硬守卫，长稳与真实付费响应尚未验收。下一步Paper资金/策略与启动明确确认，勿再问风格。首次政策data/jev-paper-20261007.sqlite3不换库、不重置费用，当前16:41:18上海截止不续期。MCP不是公共行情必需前提。启动/证据见docs/JEV_PAPER_RUNBOOK.md与docs/JEV_PAPER_VERIFICATION.md。下段“下一JI2/尚未装配”仅保留此前设置阶段范围，Flash/可选建议后台、Agent OS/Testnet执行继续后续开发。

最终页面布局以docs/JEV_PARALLEL_PRODUCT.md为准：`/overview`大盘＋Flash＋可选Jev建议，`/jev-trader`独立操盘。JI3B设置与页面已完成：JevAdvice与JevTrader独立启停及版本，Flash保持continuous配置；1113项/137subtests、新wheel与实际页面证据见docs/JEV_PARALLEL_VERIFICATION.md。下一任务JI2是三条独立后台及事实scope，模型与执行器尚未装配。此前两模式/单开关仅保留中间验证，后续执行不得将建议开关当操盘权限。

2026-10-06范围更新：用户要求Jev独立可开关、建议/自动操盘两个模式，自动按配置下单先testnet；强模型只选择不调用。本条覆盖旧只读首版和默认串行级联的冲突规划。JI1与JI3A配置/门控/持久Web设置已实现，JI2独立后台、JI4 Agent OS映射、JI5 testnet执行仍待开发；当前无实际自动订单。接续docs/JEV_INDEPENDENT_IMPLEMENTATION.md，依据docs/JEV_INDEPENDENT_MODULE.md。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 用户于2026-10-05授权自主继续推进。本计划可进入实现；依赖仍由用户安装，不因自主推进而创建或修改环境。

**Goal:** 构建本人使用的 Binance BTC 短线交易辅助 Agent：实时只读行情与账户、本地 Web、受纪律约束的建议、决策归属和可回访复盘。

**Architecture:** 自有 Domain/Application/Ports 构成内核；Direct Binance 只读 REST/WebSocket 为首版主数据链路，Agent OS MCP 为可替换工具适配器。参考 Condor 的 Session、Loop、风险和日志设计，运行状态与策略由自身管理。Replay、Fake、paper 与真实只读数据隔离。

**Tech Stack:** Conda、Python 3.12、asyncio、Pydantic 2、SQLite WAL、httpx、websockets、FastAPI、Jinja2、原生 JavaScript/SSE、可替换 LLM/MCP Client、pytest/ruff。

**Spec:** [产品与架构规格](docs/PRODUCT_SPEC.md)。输入还包括本目录 `readme.md`、外层 `AGENT_DEVELOPMENT_PLAN.md` 与 `CONDOR_CODE_WALKTHROUGH.md`。

**状态:** T01–T16原约定离线范围已验收，最近975项/129subtests与隔离wheel验证；真实WS/账户/模型/MCP及24h仍有缺口。2026-10-06新增OpenRouter/Flash/Jev接入范围，见本文追加项与MODEL_SELECTION.md，尚未实施，不重做已验证工作。正式Conda环境已就绪。

准备开始于2026-10-04；本版修订于2026-10-06（Asia/Shanghai）。

## Global Constraints

- 开发根目录固定为 `D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent`。
- 环境使用 Conda，依赖由用户安装；Agent 不自行安装、升级或改 base。
- 第一版使用本地 Web 页面并接 Binance 只读 API。
- 第一版真实交易由人执行；不接实际下单、撤单、划转或提现能力。
- 继承初步计划默认：Binance Spot、BTCUSDT、单用户、单账户、单活跃会话。
- JEV已明确为TypeSafe的结构化决策模型，拟经OpenRouter接入；专用Adapter和评测未完成前不声称已支持，不编造阈值或交易胜率。
- `domain` 不依赖 Ports；`application` 只依赖 Domain/Ports/自身；Ports 不依赖 Adapter。
- Condor、Hummingbot、MCP、HTTP/WebSocket 和 SQLite 具体类型不进入核心三层。
- 金额、价格、数量、费用使用 Decimal；时间存带时区 UTC，界面显示 Asia/Shanghai。
- 高频采样、指标、硬纪律和状态同步由确定性代码完成；模型调用低频或事件触发。
- API Secret、Token 与私钥不进入 Prompt、浏览器、数据库、日志、报告或 Git。
- 自动化测试默认离线；真实只读联调由用户提供本机凭据后单独运行。
- 保留用户现有 README 与参考 Condor 源码，移植前核对许可证和引用。

## Review Focus

- 成交靠近建议、人工修改交易、重复导入：归属不得自动推断，原始来源不可覆盖。T06/T12 约束。
- WS乱序/重复/断线、过期账户、成本历史缺口：不能发布增加风险建议或虚构盈亏。T05/T06/T07/T16 约束。
- 风格切换或账户变化时模型仍在途：过期结果不得成为当前建议。T07/T11 约束。
- 超时、供应商故障、预算日切和重启：不重复产生建议，不重置消费或绕过预留。T03/T08/T11/T15 约束。
- 工具Schema变化、恶意模型输出、意外高权限Key：不形成交易路径，不泄漏凭据。T01/T06/T10/T11/T16 约束。

---

## 1. 对初步计划的修订

| 初步计划 | 本次修订 | 原因 |
| --- | --- | --- |
| 首条链路围绕模拟下单 | 产品链路围绕只读建议，paper为测试支线 | 原始需求是提供建议 |
| Web/Telegram都靠后 | 本地Web纳入首版，Telegram后续 | 用户本次确认 |
| 先Agent OS，再Direct | Direct只读主链路，MCP不阻塞首版 | 只读API需求及MCP账户范围限制 |
| Journal靠后 | 存储与审计先于Runtime/模型 | 恢复、归属和费用需要基础事务 |
| 风控靠后 | 确定性纪律先于真实LLM | 模型输出不等于有效建议 |
| 模拟→测试网→真实下单 | 首版止于只读辅助，执行另立规格 | 不把自动交易默认为需求 |
| 偏重Session/Loop迁移 | 增加成本路由、风格修订、反馈与回访 | 补齐README要求 |

外层初步计划保留为历史输入，正式准备文件位于开发根目录。

需求覆盖表：

| 规格需求 | 负责任务 |
| --- | --- |
| R01 建议与人工执行 | T04/T06/T09/T16 |
| R02 持续监控与频率分离 | T05/T08/T15 |
| R03 模型/API成本路由 | T03/T08/T11 |
| R04 决策归属和成交时间 | T02/T06/T12 |
| R05 可回访复盘 | T03/T13/T14 |
| R06 会话风格及切换 | T07/T11/T14 |
| R07 持仓建议 | T05/T07/T08/T11；Jev接入按R14及M1–M4补齐 |
| R08 账户安全与纪律 | T01/T06/T07/T11/T16 |
| R09 独立内核与Condor参考 | T01/T02 |
| R10 Agent OS可替换 | T10/T16 |
| R11 本地Web | T09/T14 |
| R12 首版只读API | T05/T06/T09 |
| R13 Conda与用户安装 | P0环境清单/T01环境检查 |
| R14 Jev结构化决策模型 | T02/T09/T14原未接入状态；新增M2/M3专用Port/Adapter与评测 |

## 2. 里程碑与顺序

| 里程碑 | 任务 | 交付结果 | 验收门槛 |
| --- | --- | --- | --- |
| P0 准备 | 本次文档与环境定义 | 规格、计划、用户安装清单 | 用户评审、安装验证 |
| M0 离线内核 | T01–T04 | JSONL→领域模型→规则建议→审计/Replay | 离线、归属隔离、paper独立 |
| M1 只读Web闭环 | T05–T09 | Binance行情/账户→Web→规则建议 | 真实只读数据、无资金写能力 |
| M2 完整辅助流程 | T10–T14 | 可选MCP、模型路由、反馈、归属、复盘 | 预算可控、事实可追溯 |
| M3 稳定首版 | T15–T16 | 恢复、备份、持续运行、运行手册 | 完整安全与只读验收 |

M0是实现检查点，不替代用户要求的真实只读Web首版。T10不成功可禁用
MCP Adapter并继续Direct。真实账户/模型联调依赖用户配置；24小时soak
必须实际运行完整周期。按独立验收推进，不预设未经验证的工期。

```text
T01 → T02 → T03 → T04 → T05 → T06 → T07 → T08 → T09
                                                       ↓
                      T10（可选）→ T11 → T12 → T13 → T14
                                                       ↓
                                                    T15 → T16
```

准备阶段不启用多Agent；未来是否并行实施由用户选择。

## 3. 待实现文件结构

代码按已验证任务逐项创建；以下包含已实现与后续待建结构。每个文件只承担一项职责。

```text
tradingagent/
├── readme.md                       原始需求，保留
├── DEVELOPMENT_PLAN.md             总计划
├── environment.yml                 用户安装的Conda定义
├── pyproject.toml                  T01创建
├── config/
│   ├── defaults.yml                工程默认配置，无凭据
│   ├── styles.yml                  稳健/激进配置版本
│   └── model_routes.example.yml    路由/预算，收费默认关闭
├── agent_platform/
│   ├── domain/
│   │   ├── common.py               Decimal/时间/模式/来源
│   │   ├── models.py               Pydantic领域基类，不污染common
│   │   ├── market.py               Instrument/Event/Snapshot/Features
│   │   ├── account.py              Balance/Position/ObservedOrder/Trade
│   │   ├── decisions.py            Trigger/Snapshot/Advice/Feedback
│   │   ├── sessions.py             Session/StyleRevision/生命周期
│   │   ├── risk.py                 DisciplineLimits/RiskAssessment
│   │   ├── costs.py                Route/Reservation/Usage
│   │   ├── reviews.py              Attribution/TradeGroup/ReviewRevision
│   │   ├── paper.py                测试专用Intent/Fill
│   │   └── events.py               JournalEvent/SchemaVersion
│   ├── ports/
│   │   ├── market.py               行情订阅和查询
│   │   ├── account.py              只读账户/订单/成交
│   │   ├── model.py                模型请求与响应
│   │   ├── persistence.py          事件/状态/导入/预算协议
│   │   ├── clock.py                墙钟与单调时钟
│   │   ├── advisory.py             规则/JEV评估
│   │   └── notification.py         本地状态与提醒
│   ├── application/
│   │   ├── features.py             确定性指标
│   │   ├── account_sync.py         游标/成本可信性
│   │   ├── sessions.py             会话与风格修订
│   │   ├── risk.py                 纪律与硬风险提醒
│   │   ├── triggers.py             筛选与优先级
│   │   ├── decisions.py            模型请求与建议校验
│   │   ├── feedback.py             反馈与真实成交归属
│   │   ├── reviews.py              复盘与回访
│   │   └── queries.py              Web脱敏视图
│   ├── runtime/                    clock/buffer/scheduler/supervisor/review_jobs
│   ├── adapters/
│   │   ├── fake/                   行情/账户/模型/时钟
│   │   ├── rules/                  明确标记的基线规则
│   │   ├── paper/                  仅模拟成交与资金账本
│   │   ├── binance_direct/         白名单REST/WS/签名/normalizer
│   │   ├── binance_agent_os/       MCP发现/授权/严格映射
│   │   ├── llm/                    HTTP Client/预算路由
│   │   └── sqlite/                 schema/迁移/事务/投影
│   ├── replay/                     JSONL读取/时序驱动/报告
│   ├── web/                        app/routes/templates/static
│   ├── bootstrap.py                唯一装配入口
│   └── cli.py                      启动/回放/只读诊断
├── tests/                          architecture/domain/application/runtime/
│                                    adapters/market/replay/web/integration/fixtures
└── docs/                           规格/环境/源码盘点/证据/运行手册
```

不建空Condor Adapter，只有明确复用收益时再写独立计划。

## 4. 核心接口与一致性

表中标注async的方法为`async def`；stream为普通`def`返回异步迭代器。
Clock、特征计算、规则校验是同步纯函数。全部参数与返回值是自有领域DTO，
不返回Binance字典。事件带schema_version/event_id/aggregate_id，账户带account_ref。
ID为不透明字符串，不编码身份或密钥；测试注入确定性ID/Clock。

| Port | 签名 | 首个实现 |
| --- | --- | --- |
| MarketDataPort | `stream(symbols: tuple[str, ...]) -> AsyncIterator[MarketEvent]`; `async latest(symbol: str) -> MarketSnapshot` | Fake、Direct |
| AccountPort | `async snapshot(account_ref: str) -> AccountSnapshot`; `async orders(account_ref: str, symbol: str) -> tuple[ObservedOrder, ...]`; `async trades(account_ref: str, symbol: str, cursor: TradeCursor) -> TradeBatch` | Fake、REST |
| ModelPort | `async generate(request: ModelRequest) -> ModelResponse` | Fake、HTTP |
| AdvisoryPort | `evaluate(snapshot: DecisionSnapshot) -> AdvisoryAssessment` | 确定性Rule；Jev专用Decision Model Port在M2另行定义 |
| ClockPort | `utcnow() -> datetime`; `monotonic() -> float` | System/Fake |
| EventStorePort | `async append(event: JournalEvent) -> AppendReceipt`; `async scan(after_sequence: int, limit: int) -> EventPage` | SQLite |
| StateStorePort | `async load(key: str) -> StateRecord \| None`; `async save(record: StateRecord, expected_revision: int, event: JournalEvent) -> StateRecord` | 原子状态+事件 |
| ObservationStorePort | `async ingest(batch: TradeBatch, account: AccountSnapshot, event: JournalEvent) -> ImportResult` | 原子成交+游标+事件 |
| BudgetStorePort | `async reserve(request: BudgetRequest) -> BudgetReservation`; `async settle(reservation_id: str, usage: ModelUsage) -> BudgetBalance` | 原子预算 |
| NotificationPort | `async publish(notice: Notice) -> None` | 本地/SSE |

T02定义表中辅助DTO及后文应用接口使用的全部领域返回类型；它们按语义
归入market/account/decisions/sessions/risk/costs/reviews/events/paper文件。
StateRecord保存带类型标记的领域状态，不能存SDK对象或凭据。
RuntimeConfig、ApplicationServices仅供装配/Web使用，不被领域引用。

- 会话按明确生命周期转换；历史不逆写，恢复生成新运行周期。
- ObservedOrder终态不退回NEW、累计成交量不下降；新事实用对账纠正。
- 建议CREATED→PUBLISHED→ACCEPTED/REJECTED/EXPIRED/SUPERSEDED，修改为新反馈。
- 成交、导入游标、事件同事务；失败允许重拉，禁止跳过未提交成交。
- 风格/状态保存使用expected_revision，冲突明确拒绝。
- 预算按request_id幂等，超时未确定费用保留预留，不靠重启清零。
- BudgetRequest包含日预算和小时调用额度；reserve事务同时检查滚动小时计数，失败/超时已发送的请求仍计数，重启不能重置。
- 建议持久化成功后发布；审计不可写时停止新增建议。
- live/paper账号与事实独立命名空间；不以时间接近自动归属。

## 5. 分任务实施

用户已授权实现。涉及完整应用的命令在用户安装并激活项目Conda环境后运行。
环境未准备时，允许使用现有Conda Python 3.12执行纯标准库的独立架构测试，
不修改该环境；此诊断不代替正式环境和Pydantic/网络Adapter的验证。
每项测试先写先看目标失败，再实现、验收、记录证据；失败不能由安装错误
替代RED。完整检查为`python -m pytest -q`和`python -m ruff check .`。
如用户选择提交，只暂存该任务文件，不包含既有README修改或外层参考文件。

### T01：独立包与依赖边界

**Files:** Create `pyproject.toml`、`.gitignore`、各层`__init__.py`；Test `tests/architecture/test_dependency_boundaries.py`；更新`docs/CONDOR_EXTRACTION.md`。

**Interfaces:** Consumes用户验证的Conda 3.12/Pydantic 2；Produces可独立导入包与import方向规则。

- [x] 写`test_core_imports_without_condor`，参考Provider缺席仍可导入核心。
- [x] 写`test_application_rejects_adapter_dependency`，检查普通/相对/字面量动态import及Ports间接污染；同时禁止传输与SQLite依赖。
- [x] Run `python -m pytest tests/architecture -q`；Expected目标缺失。
- [x] 建最小包与工具配置；核对Condor复用/仅参考边界，不复制整个源码。
- [x] 同命令与完整检查通过，记录证据；可选commit `chore: establish independent advisory kernel`。

### T02：领域模型、状态与Ports

**Files:** Create第3节`domain/*.py`、`ports/*.py`；Test `tests/domain/test_models.py`、`test_orders.py`、`test_attribution.py`、`test_ports.py`。

**Interfaces:** Produces第4节协议/DTO、schema_version=1及状态转换；Consumes T01。

- [x] 写Decimal正数与精度测试；拒绝NaN/Infinity、无时区时间、未知市场及额外敏感字段。
- [x] 写订单终态不倒退、未知成本不归零、导入归属UNCLASSIFIED、真实执行者HUMAN测试。
- [x] 写Port契约用例，Fake结构化实现协议，消费者不感知外部类型。
- [x] Run `python -m pytest tests/domain -q`；Expected目标缺失。
- [x] 实现不可变对象与转换，建议/成交/反馈分开；所有后续任务复用同一DTO。
- [x] 同命令、架构与完整检查通过，记录证据。

### T03：SQLite事务、事件、游标与预算

进度：会话存储、通用事件、统一v3迁移/备份、预算预留/结算/小时额度已验证。
以下完整任务仍需补状态CAS、成交/游标同事务和全部接口装配，不提前勾选验收。

**Files:** Create `adapters/sqlite/{schema,migrations,events,state,observations,budgets}.py`；Test `tests/adapters/test_sqlite_events.py`、`test_sqlite_state.py`、`test_sqlite_observations.py`、`test_sqlite_budgets.py`。

**Interfaces:** Implements四个Persistence Ports；Produces装配使用的`async open_store(path: Path) -> SqliteStore`；Consumes T02。

- [x] 写重复event_id幂等、expected_revision冲突、成交/游标/事件同事务回滚测试。
- [x] 写预算1.00 USD时两个并发0.60预留只能成功一个；重新打开数据库仍保留预算/小时调用额度/游标测试。
- [x] 写迁移版本、备份、损坏数据库明确拒绝、序列化凭据字段拒绝测试。
- [x] Run `python -m pytest tests/adapters -k sqlite -q`；Expected目标未实现。
- [x] 实现WAL、唯一键、单写事务和迁移；Adapter内串行处理IO，不阻塞采样。后续数据表随各任务版本化迁移。
- [x] 同命令与完整检查通过；故障后不存在半提交，日志脱敏。

### T04：Fake、Replay与隔离paper

**Files:** Create `adapters/fake/{market,account,model,clock}.py`、`adapters/rules/baseline.py`、`adapters/paper/execution.py`、`replay/{reader,runner,report}.py`；Test `tests/replay/test_replay.py`、`tests/adapters/test_paper.py`；Fixture `tests/fixtures/btc_events.jsonl`。

**Interfaces:** Produces `async ReplayRunner.run(events: Iterable[MarketEvent], advisory: AdvisoryPort, mode: RunMode) -> ReplayReport`；paper `async simulate(intent: PaperIntent, snapshot: MarketSnapshot) -> PaperFill`；Consumes T02/T03。

RuleAdvisoryProvider实现AdvisoryPort，只用明确标记的测试规则。
M0验证接口与数据流；真实指标由T05补齐，真实纪律由T07补齐。

- [x] 写默认ADVISORY产出建议/记录而不改变账本、paper需显式模式、独立账号测试。
- [x] 写重复intent不二次成交、余额不足不成交、无网络与JSONL错行带行号测试。
- [x] 写不前视、相同输入/规则得到相同结果测试；LLM用录制响应而非真实网络。
- [x] Run `python -m pytest tests/replay tests/adapters/test_paper.py -q`；Expected目标缺失。
- [x] 实现流式JSONL/FakeClock/规则基线；paper记录手续费、滑点和成交模型版本，不把测试模型宣传为真实交易能力。
- [x] 同命令与完整检查通过；报告网络调用数=0、真实订单数=0。

### T05：真实公共行情、窗口与特征

**Files:** Create `adapters/binance_direct/{market_stream,public_rest,normalizer}.py`、`runtime/buffer.py`、`application/features.py`；Test `tests/market/test_normalizer.py`、`test_buffer.py`、`test_features.py`；Fixture `tests/fixtures/binance_market.jsonl`。

**Interfaces:** Implements MarketDataPort；Produces `MarketBuffer.append(event: MarketEvent) -> BufferUpdate`、`snapshot(symbol: str, as_of: datetime) -> MarketSnapshot`、`FeatureService.compute(snapshot: MarketSnapshot) -> FeatureSnapshot`。

- [x] 写trade/bookTicker/kline严格映射、重复ID/乱序、断线缺口与预热不足测试。
- [x] 写重复识别缓存长期运行仍有上界；同一K线的未收盘更新不能吞掉最终收盘事件；迟到历史不能改写已发布快照。
- [x] 手算EMA(period=3)：100/110/120/130，SMA预热后末值120；等量100/110，VWAP=105。
- [x] 手算ATR(period=3)：TR=3/6/3初值4，下一个TR=10后Wilder ATR=6；未预热不是0。
- [x] 收益=end/start−1；波动率为窗口简单收益的样本标准差，不足两个收益不可用；量变化为相邻完整同长窗口总量比减1。
- [x] Run `python -m pytest tests/market -q`；Expected对应目标缺失。
- [x] 实现event-time 1s/5s/1m聚合、120根1m/300个1s窗口；EMA12/26与ATR14基于收盘K线，Decimal精度34；WS断线用REST补K线，不造缺失数据或历史建议。
- [x] 乱序允许窗口默认2秒，watermark之后的太晚事件只记缺口/迟到事实；重复ID缓存采用时间与容量双上限。公开行情无事件时间的字段使用接收时间并标记质量，禁止伪造交易所时间。
- [x] 同命令与完整检查通过；确定性、不前视、有界内存、ModelPort调用数=0。
- [ ] 真实公共WS联调验收。REST服务器时间已读到，WS25秒诊断两次连接失败；不以Mock或no_data报告替代。网络条件改变后重试，离线T06继续。

### T06：账户、观察订单、真实成交与成本可信性

**Files:** Create `adapters/binance_direct/{read_client,signing,account,trade_history,user_stream}.py`、`application/account_sync.py`；Test `tests/adapters/test_binance_read_only.py`、`test_account_sync.py`、`test_trade_import.py`。

**Interfaces:** Implements AccountPort；Produces `async AccountSyncService.sync(account_ref: str, symbol: str) -> SyncReport`；Consumes T02/T03/T05。

- [x] 写签名/服务器时间偏差、分页边界重复、成本未知、余额漂移与重新订阅重对账提示测试；实际提示消费者由T15装配。
- [x] 白名单测试：GET account/openOrders/order/myTrades和指定公共路径允许；POST/DELETE order、划转、提现、任意URL发网前拒绝。
- [x] 写失效凭据不阻断公共行情、凭据不进日志、trade_id去重、UNCLASSIFIED归属测试。
- [x] 首批签名/只读/映射/分页/同步/user_stream目标缺失RED→GREEN；实际测试分别位于tests/adapters与tests/application。
- [x] 实现REST只读主路径、当前Schema/限流；账户15秒刷新，429遵守Retry-After，权限错误不盲重试。
- [ ] 按当前用户数据流文档验证WebSocket API只读订阅；失败保留REST，不扩权限，不照搬旧listenKey。
- [ ] 同命令与完整检查通过；真实联调只查询，绝不靠真实写请求验证Key权限。

T06离线代码与Mock故障链路已完成，真实Key范围/账户历史/USER_STREAM权限尚未联调。schema v6迁移与补证细节见docs/READ_ONLY_ACCOUNT_CONTRACT.md；成本核算保持UNKNOWN。T07可继续，不等待凭据。

### T07：Session、风格修订与硬纪律

**Files:** Create `application/{sessions,risk}.py`、`config/styles.yml`；Test `tests/application/test_sessions.py`、`test_risk.py`。

**Interfaces:** Produces `async SessionService.create(style: TradingStyle) -> AgentSession`、`async change_style(session_id: str, style: TradingStyle, expected_revision: int) -> AgentSession`、`RiskService.evaluate(snapshot: DecisionSnapshot, proposed: AdvisoryAssessment) -> RiskAssessment`。

- [x] 写创建必须确认0–100整数风格、修改持久化新版本、并发修订不能覆盖测试。
- [x] 写两风格均不可绕硬限制；行情>5s/账户>60s过期不能增加风险；Spot无free BTC不建议卖空。
- [x] 写未配数量上限不放行具体仓位、无LLM仍能检查硬风险测试。
- [x] 风险首批29项目标缺失RED→GREEN，追加Decimal ambient指数范围实际RED→GREEN；现有会话测试保持。
- [x] 实现ALLOW/BLOCK/UNAVAILABLE，用户配置资金纪律，风格不能修改全局风险。未定义阈值只用于明确测试场景，不假装已验证策略。
- [x] 同命令与完整检查通过；会话/风格可恢复。完整519项+83 subtests、Ruff和隔离wheel验证通过。

T07内核风险契约见docs/RISK_CONTRACT.md；TradingStyle.context是唯一style-v1来源，不重复创建styles.yml。真实纪律、过滤器/费用缓冲/日亏损证据供应及发布前重验仍需后续装配。

### T08：低频调度、触发去抖与冷却

**Files:** Create `runtime/scheduler.py`、`application/triggers.py`；Test `tests/runtime/test_scheduler.py`、`tests/application/test_triggers.py`。

**Interfaces:** Produces `DecisionScheduler.notify(event: DecisionEvent) -> None`、`async next_trigger() -> DecisionTrigger`、`TriggerService.evaluate(features: FeatureSnapshot, session: AgentSession) -> tuple[DecisionEvent, ...]`。

- [x] 写300条平静特征不逐条产生模型事件，300秒才有一次空仓定时评估测试。
- [x] 写持仓60s、同类30s冷却、一个在途评估、队列合并/过期丢弃测试。
- [x] 写风险通知不等LLM、单调计时不受墙钟回拨；日切使用T03同一上海预算时区。
- [x] 首批11项目标缺失RED→GREEN，FakeClock验证，不发网/调用模型。
- [ ] 实现max60模型请求/小时全局额度，手工模型请求和复盘同样计入；规则通知不消耗模型次数。
- [x] 同命令与完整检查通过；采样计数与模型计数独立。实际收费请求装配仍在T11。

调度内核详见docs/SCHEDULING_CONTRACT.md。全局60次/小时已经由T03 BudgetPort持久强制，T11需在实际请求处接入，不在Scheduler另做易重启清零的内存配额。

### T09：本地Web的第一条只读链路

**Files:** Create `web/app.py`、`web/routes/{queries,sessions}.py`、`web/templates/{base,overview,session}.html`、`web/static/{app.css,app.js}`、`application/queries.py`、`runtime/clock.py`、`config/defaults.yml`、`bootstrap.py`、`cli.py`；Test `tests/web/test_read_only_ui.py`、`tests/integration/test_read_only_slice.py`。

**Interfaces:** Produces Web层 `create_app(services: ApplicationServices) -> FastAPI`、`async QueryService.overview() -> OverviewView`、装配层 `async run_read_only(config: RuntimeConfig) -> None`。

- [x] 写脱敏overview/account/session、SSE重连、POST style的CSRF/Origin/Host校验测试。
- [ ] 公共行情正常但账户不可用、JEV=UNSPECIFIED、无交易路由已验证；正式规则建议发布与风险重验留T11。
- [x] 查询10项、后台6项、Web/配置14项观察目标缺失RED→GREEN；原风格回归修复装配接口。
- [x] 实现127.0.0.1总览/会话/账户：价格、持有量、数据时间、建议状态、故障清楚可见；写请求只改本地状态。
- [x] SystemClock实现ClockPort；RuntimeConfig工程开关与环境凭据分离，缺配置启动前拒绝。保持typed默认配置唯一来源，不重复创建defaults.yml。
- [x] 单后台进程维护采样与会话；浏览器关闭监控继续，SSE只推有界最新快照。
- [ ] 同命令与完整检查通过；用户真实只读联调后保存脱敏报告，达到M1，不等MCP/LLM。

T09离线装配/浏览器与本机SSE证据已通过；无凭据模式不发网。真实WS网络与账户联调仍待条件具备，不能标M1通过。

### T10：Agent OS调查与可选只读Adapter

**Files:** Create `docs/BINANCE_MCP_CONTRACT.md`、`adapters/binance_agent_os/{client,discovery,mapper}.py`；Test `tests/adapters/test_mcp_mapper.py`、`tests/integration/test_mcp_contract.py`。

**Interfaces:** Produces `async McpCapabilityProbe.inspect() -> CapabilityReport`；实测后实现MarketDataPort/AccountPort受支持子集，其余返回CapabilityUnavailable，不伪造空账户。

- [x] 核对官方HTTP/MCP与权限/主账户说明；授权续期与常驻客户端未实测，具体工具名不猜测。边界见docs/BINANCE_MCP_CONTRACT.md。
- [ ] 记录实测证据，无法满足只读/续期则禁用，不阻塞Direct。
- [x] Synthetic tools/list离线测试：Schema/metadata变更、新增/缺失工具、权限异常、超时、取消、分页与有界内存；未标为真实录制。
- [ ] Run `python -m pytest tests/adapters/test_mcp_mapper.py -q`；Expected映射目标缺失。
- [ ] 仅暴露验证的只读工具白名单；不把call_tool交给策略，不假设testnet，不试单。此阶段用户再安装确认版本的MCP SDK。
- [ ] 离线测试通过，真实MCP为单独opt-in验收；桌面客户端登录不等于程序授权。

T10离线探测15项通过，默认disabled、verified_capabilities固定为空；tools/list及readOnlyHint不授予读取权限。真实SDK/授权/精确工具映射仍待实测，不返回虚构空账户，不阻塞T11。

### T11：模型路由、费用预留与建议编排

离线验收完成：路由16项、Prompt27项、原子发布26项、决策服务29项、建议查询/快照18项、后台15项、Web3项、Supervisor1项；完整710项+99subtests，Ruff通过。具体RED/复核证据见docs/IMPLEMENTATION_LEDGER.md。

**Files:** application/{routing,prompting,decisions,snapshots,advice_queries}.py、runtime/{decisions,supervisor}.py、domain/{routing,decision_requests,advice_views,runtime_views}.py、adapters/sqlite/decisions.py、Web装配。
**Interfaces:** DecisionService.prepare(snapshot, request_id)冻结输入；async decide(request: DecisionRequest) -> DecisionResult；ModelRouter.select(snapshot, purpose) -> RouteDecision；SnapshotFactory只读本地缓存；DecisionStorePort原子claim/finish，DecisionReadPort读取已提交事实。ModelPort由Fake/录制响应实现，供应商HTTP Adapter留待用户确定供应商/价格/预算后实施和联调。

- [x] 零预算不收费、economy/standard/review、未知/过期价格关闭、最多一次免费规则回退。
- [x] 超时/取消/严格JSON/伪造evidence/未知action不发布；未知费用和token不得伪装0。
- [x] 在途风格/实质账户变化SUPERSEDED；发布前当前行情重验，原始证据时间不改写。
- [x] 预算跨重启、同request_id不重复派发/发布，估计/实际费用分离。
- [x] 有限Prompt输入保守token上界、max_output_tokens、价格版本/核对时间与单次/日/小时额度；实际超估冻结。
- [x] 对应目标缺失/实际失败RED与GREEN已记录；命令采用最终实际测试文件名。
- [x] 15s超时与审计后发布，模型慢/失败不阻塞行情与独立硬提示，Web查询不发网络。
- [x] Fake→SQLite→Web离线流程、生命周期/CAS、费用故障投影、最终时间/TTL已复核。
- [ ] 真实供应商HTTP Adapter/精确模型价格/显式非零预算与小额只读联调。当前日预算0、无模型/策略，不将离线通过当作真实接入验收。

### T12：反馈、归属与真实成交纠正

**Files:** Create `application/feedback.py`、`web/routes/feedback.py`；Test `tests/application/test_feedback.py`、`tests/web/test_feedback_routes.py`。

**Interfaces:** Produces `async FeedbackService.record(feedback: DecisionFeedback) -> FeedbackReceipt`、`async AttributionService.record(change: AttributionChange) -> AttributionReceipt`、`ReportService.record/verify`、`LedgerQueryService.current`。归属纠正使用明确operation_id和expected_revision，支持原回执重试。

- [x] 写采纳/拒绝/修改、独立人类想法、成交未归属、误关联追加纠正测试。
- [x] 写原始作者AGENT/实际决策者HUMAN/执行者HUMAN分开保存，原建议不变测试。
- [x] 写关联重试幂等、paper与真实成交不得互相关联测试。
- [x] 目标缺失RED已观察；反馈24、归属16、手工报告17、Web16项通过。
- [x] 实现用户显式关联；USER_REPORTED只按明确交易ID核实并追加关联，保留原报告；没有ID仍待核实。
- [x] 独立复核及完整回归通过；反馈、实际成交、策略建议可独立查询。完整操作页面属于T14。

### T13：版本化复盘与持久回访（离线完成）

**Files:** Create `application/reviews.py`、`runtime/review_jobs.py`、`adapters/sqlite/reviews.py`；Test `tests/application/test_reviews.py`、`tests/runtime/test_review_jobs.py`。

**Interfaces:** Produces `async ReviewService.generate(group_id: str, cutoff: datetime, kind: ReviewKind) -> ReviewRevision`、`async ReviewJobService.schedule(group_id: str, due_at: datetime, kind: ReviewKind) -> ReviewJob`。

- [x] 写部分买卖/手续费/未知非USDT费用估值/存入BTC成本缺失不虚构PnL测试。
- [x] 写初始复盘/后续版本共存，回访注明未来数据，原快照不可变测试。
- [x] 写重启/重复任务只产一份同键复盘、预算耗尽仍生成规则复盘并标模型未参与测试。
- [x] Run `python -m pytest tests/application/test_reviews.py tests/runtime/test_review_jobs.py -q`；Expected目标缺失。
- [x] 实现Spot FIFO分组，历史缺口标PARTIAL/UNKNOWN；回访1h/24h可选或手工，不自动修改策略。任务键group/kind/due_at唯一。
- [x] 同命令与完整检查通过；事实/事后解释分开，带截止时间/版本/来源。

Evidence：docs/T13_IMPLEMENTATION_PLAN.md、IMPLEMENTATION_LEDGER.md；873项/120subtests、wheel隔离通过。完整库存/资金移动/FX与成交时账户供应尚未验收；缺口明确，不声称真实盈亏。事后上下文复用本地已提交决策快照，无连续行情recorder。

### T14：完整建议、反馈、复盘与系统状态Web

**Files:** 实际为web/review_routes.py、application/{review_queries,system_queries}.py、web/templates/{records,reviews,status,workbench,navigation}.html及本地JS/CSS/quote-chart.js；Test tests/web/test_advisory_flow.py与tests/adapters/test_budget_read.py。

**Interfaces:** Consumes T09/T11/T12/T13服务与Query DTO；不新增同义领域类型。

- [x] 已确认风格→建议/反馈→导入成交→归属→复盘→手工回访离线集成；风格创建沿用既有会话回归。Fake浏览器操作/刷新已通过，不称真实联调。
- [x] 零预算/过期/模型不可用/JEV未接入明确UNAVAILABLE，既有建议回归与新系统投影通过；该验证点使用旧unspecified状态，不代表Jev适配器已实现。
- [x] 本地assets/转义/敏感投影；实际浏览器恶意HTML普通文本，无注入节点。
- [x] 目标缺失RED→GREEN；Web14+预算1项，独立API及UI门控复核通过。
- [x] 本地assets/REST/SSE/有界SVG，无CDN或产品Node依赖；页面只写本地记录。
- [x] 完整888项/122subtests、Ruff、wheel隔离与1280/304px实际浏览器操作通过，无Binance写请求。

Evidence：docs/T14_IMPLEMENTATION_PLAN.md、IMPLEMENTATION_LEDGER.md，output/verification/t14-*。任务列表明示前50项限制，图表只记录当前浏览器最近120个报价，无历史回放。继续T15。

### T15：Supervisor、恢复、保留策略与24/7验证

**Files:** `agent_platform/runtime/{supervisor,account_signals,market_archive,diagnostics,soak}.py`、`agent_platform/adapters/sqlite/{retention,backup}.py`、`agent_platform/adapters/{owned_io,operational_log}.py`；更新bootstrap/CLI及系统页；对应runtime/adapters集成测试与`docs/OPERATIONS.md`。

**Interfaces:** 实际采用 `async RuntimeSupervisor.start() -> None`、`async stop() -> None`、`async health() -> RuntimeHealth`；恢复复用既有SQLite Ports及进程组装，不另造recover接口。`async SqliteMarketArchive.prune(as_of, *, limit=1000) -> RetentionReport`负责独立辅助库；backup/soak使用显式CLI，核心Journal永久保留。

- [x] 写恢复会话/风格/游标/预算/回访，重同步账户前不发布持仓建议测试。
- [x] 写数据库不可写、断网/重连、优雅取消、有界窗口与单实例防重复收费测试；断网/重连为Fake/录制证据。
- [x] 写7天raw/90天分钟聚合清理不删复盘依据，磁盘/备份错误显式降级测试。
- [x] 保留时长可在web/soak分别配置1–365天整数，默认7/90；实际SQLite/pin/重启与状态显示验证。
- [x] 目标缺失RED→GREEN、独立复核和最终完整回归完成；具名记录见IMPLEMENTATION_LEDGER.md。
- [x] 实现生命周期/健康/计数/日志轮转/在线备份与手册；实际两次跨进程恢复、Windows前台退出和3秒短测分别记录。
- [ ] 完成实际24小时只读soak，报告内存/费用/重连/错误；不能以短测冒充。用户选宿主机后才配置后台服务，Windows隐藏窗口。

### T16：首版全链路与安全验收

**Files:** `tests/integration/{test_offline_slice,test_security}.py`、`tests/offline_network.py`、`agent_platform/{domain,runtime}/acceptance.py`、`docs/ACCEPTANCE_REPORT.md`；已有建议与故障测试复用，不新建重复slice；更新手册/状态/锁定快照。

**Interfaces:** 装配层 `async run_acceptance(mode: RunMode, *, database_path: Path) -> AcceptanceReport`仅接受ADVISORY离线smoke；区分AUTOMATED_OFFLINE/MANUAL_READ_ONLY/SOAK，formal_acceptance固定false，不以smoke或单元测试冒充真实联调。

- [x] 将Review Focus五类失败逐一映射到测试，补遗漏覆盖。
- [x] 写全链路默认断网、真实写接口不可达、凭据脱敏、Prompt注入无权限效果测试；补UDP及隐藏路由实际RED→GREEN。
- [x] 最终完整975项与129个subtest通过（103.54s）、Ruff/232文件format通过；最终wheel的Python -I隔离验证通过（含可配置保留）。
- [ ] 用户本机只读凭据完成真实行情/账户/成交查询验收；模型/MCP无凭据明确未验证。
- [ ] 补齐实际24小时及真实联调证据后复核首版；恢复、预算、风格、复盘的离线/本机证据已记录，不能据此标首版通过。
- [x] 交付源码、已安装环境的requirements.lock.txt快照、手册、验收报告及未决项；真实交易仍由用户操作。

2026-10-05：T15/T16离线范围完成，实际首版仍未验收。完整结果与外部配置条件见[ACCEPTANCE_REPORT.md](docs/ACCEPTANCE_REPORT.md)。配置或网络状态不变时，不重跑已验证测试或真实诊断。

## 6. 验收组织

| 层级 | 重点 | 外部访问 |
| --- | --- | --- |
| Architecture | import与独立性 | 无 |
| Domain | Decimal/时间/状态/归属不变量 | 无 |
| Application | 特征/纪律/决策/反馈/复盘 | Fake Ports |
| Adapters | 签名/白名单/Schema/事务/限流 | 脱敏fixtures |
| Offline integration | Replay/恢复/预算/故障 | 默认断网 |
| Manual read-only | 当前权限/账号范围/实时数据 | 用户配置只读联调 |
| Soak | 实际24小时、内存/费用/重连 | 公共行情与只读账户 |

真实验收单独marker/命令，启动前验证READ_ONLY和白名单；不能用环境变量
悄悄切到真实写模式。LLM确定性回放使用录制响应/Fake，不承诺随机模型逐字相同。
测试侧重实际行为；不为目录、文字或常量写无价值断言。

## 7. 风险、未决项与扩展

| 项目 | 默认处理 | 需要决定的节点 |
| --- | --- | --- |
| Spot是否改为Futures | 继承Spot；不能混用余额/仓位 | 领域实现前 |
| 实际账号与钱包范围 | 验证只读Key范围 | 账户联调前 |
| 强模型/provider/价格/预算 | OpenRouter/Flash/Jev已明确；离线接入可推进，收费0预算 | 首次收费调用前 |
| 个人资金纪律 | 不生成具体数量 | 数量建议启用前 |
| Jev问题标准与升级阈值 | 模型含义已明确，专用接入与评测未完成 | Jev建议发布前 |
| 长期运行机器/通知 | 本地Web；休眠不能持续监控 | 24/7使用前 |
| MCP续期/主账户能力 | 未通过则禁用，Direct继续 | MCP联调前 |
| 成本历史缺口 | UNKNOWN/PARTIAL，不填0 | 用户导入旧记录后核实 |

Jev按本次新增模型接入规格推进；Futures、Telegram/系统通知、远程Web、多设备仍按独立规格扩展。
未来如果用户要求代理执行，先另立testnet计划：ExecutionPort、幂等、
UNKNOWN_SUBMIT对账、人工确认、Kill Switch、持久恢复、资金上限。
完成只读首版不自动授权真实下单，也不因已有Order模型就启用资金写能力。
Condor/Hummingbot迁移只按明确收益做，不照搬其所有功能。

## 8. 准备阶段交接

- [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)：需求/边界/默认值/验收。
- [CONDOR_EXTRACTION.md](docs/CONDOR_EXTRACTION.md)：源码耦合与参考边界。
- [ENVIRONMENT_SETUP.md](docs/ENVIRONMENT_SETUP.md)：用户的Conda安装命令。
- [environment.yml](environment.yml)：基础依赖定义，无密钥/项目安装。
- [DEVELOPMENT_STATUS.md](docs/DEVELOPMENT_STATUS.md)：准备状态及纠偏记录。

推荐以后按任务在当前会话逐项实施，独立评审方式另由用户选择。
按用户最新授权，由当前Agent逐项自主实施，必要时设置本聊天的持续推进任务。
仍不创建Conda环境或安装依赖，不启动真实交易能力。
项目环境未就绪时记录明确验证缺口；正式运行前核对Conda检查输出。

## 2026-10-05 风格追加要求

用户已安装项目Conda环境并要求继续推进。风格以0–100整数滑杆作为唯一保存值，详见[会话风格控制](docs/STYLE_CONTROL.md)。本轮先实现T02基础模型及T03/T07/T09的会话设置子集，不把子集标记为完整任务完成。

## 2026-10-06 模型选型与新增范围

用户选定OpenRouter与DeepSeek V4.1 Flash，并澄清JEV为TypeSafe Jev，固定版本typesafe/jev-1.13；历史测试/快照不改写。完整分工与验收条件见[MODEL_SELECTION.md](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/docs/MODEL_SELECTION.md)。用户最新要求强模型可选择、暂不调用，本版本ID配置与CLI/API状态已实现且调用固定禁用；Opus/Sonnet仅是候选推荐。

- [x] M1离线主体：OpenRouter配置/Transport/Flash Chat Adapter、完整Schema请求报价、费用/错误/超时/凭据边界通过Mock HTTP专项验证；启动仍不装配收费模型，生产价格待M5。
- [x] M2离线主体：Jev独立Port、Choice/Score/Noul契约/Decisions Adapter、持久预算执行器与问题/版本绑定验证；真实连接/语义评测待M4/M5。
- [ ] M3：独立Jev持仓后台与Flash并行；两模式/开关/费用/审计/身份重验。JI1/JI3A已实现，JI2和完整状态迁移待做，取消默认串行级联要求。
- [ ] M4：真实策略/个人纪律/模型配置入口与标注样本评测；不把测试策略或模型confidence当交易胜率。
- [ ] M5：用户明确限额和本机凭据后最小真实联调，再完成Binance真实WS/只读账户与实际24h验收。

M1/M2主体细化计划见MODEL_FRAMEWORK_IMPLEMENTATION.md；F1–F4源码/Mock HTTP/真实SQLite已实现。新增独立模块实施见JEV_INDEPENDENT_IMPLEMENTATION.md；JI1/JI3A后接JI2后台，JI4工具映射、JI5自动testnet需独立验证。离线开发不依赖Key或非零预算；收费默认关闭，强模型不调用。保留现有时效门，不直接延长并发布过期结果。
