# Trading Agent 产品与架构规格 v0.6

2026-10-09模型更新：用户将DeepSeek Flash角色改为OpenRouter `anthropic/claude-haiku-5.5`，包括新会话首次历史分析及JEV四层背景整理；JEV快速决策保持独立。正常AER工作台已真实验证Haiku背景→六层JEV输入，使用方式与证据见[JEV_MULTI_SCALE_RUNBOOK.md](JEV_MULTI_SCALE_RUNBOOK.md)。常规Agent codewriting仍仅记录、不开发。本文后续Flash名称和早期接入状态保留历史，以此更新及DEVELOPMENT_STATUS顶部为准。

2026-10-08 当前范围：JEV左侧多会话栏＋统一分步创建＋工作区，多个JEV建议/自动Paper独立后台、显示切换不停止其他任务、共用原费用上限、逐笔严格存档。采用索引＋每任务独立SQLite和内核，旧会话与钱包保留。建议仅存决策、不成交，当前明确使用参考虚拟账户。详见[JEV_WORKBENCH_SPEC.md](JEV_WORKBENCH_SPEC.md)。

常规Agent未来把近期行情与交易情况交给LLM，通过codewriting编写定时分析脚本；脚本持续监测以减少重复调用费用和延迟。当前仅记录、不开发，后台并行以JEV为主。见[REGULAR_AGENT_SCRIPT_PLAN.md](REGULAR_AGENT_SCRIPT_PLAN.md)。下文Flash常态循环/可选建议后台不作为本轮实施任务。

2026-10-08 20:00上海：用户要求先完成真实秒级联调/完整模拟操盘主体并逐笔严格存档。当前每次预测在收费前持久保存完整请求，返回后保存绑定结果/耗时；成交证据随通用命令，在Paper同事务保存报价、比例数量杠杆、费用盈亏和前后资金持仓，档案失败回滚。资金费/清算入SHA-256链，旧记录标注，保护分页和JSONL/CSV已接页面。真实最终8WAIT，1.047–1.063秒发起/0.719–1.219秒返回；多空16离线成交闭环通过，实际自主成交仍未观察。公共行情允许50ms校时误差并保存原时间，超界/5秒过期拒绝。原钱包暂停/预算保留，运行证据与限制[JEV_PAPER_ARCHIVE.md](JEV_PAPER_ARCHIVE.md)，下文未真实秒级联调为前阶段历史。

2026-10-08 18:41上海运行核对：持久校时安装完成，实际Cloudflare时间源和64秒同步，偏差8.5849ms；免费公共WS 8/8有效。原1000USDT空仓钱包仍暂停、费用/会话/策略保留，无新收费。框架支持1秒发起/最多3并行，但真实JEV v3参数决策的延迟、成交/自主退出与连续稳定仍待验收。下文“Install尚未执行/校时阻断”是前阶段状态。

2026-10-08最新用户要求：合约JEV每秒发起预测，允许最多3并行，执行串行；3秒时效、账号/风格/控制版本及有效结果序号守卫拒绝过时结果。报价使用book/mark@1s和30真实样本，JEV继续决定动态仓位/杠杆及退出时机。已在8776暂停载入，原资金与累计费用保留；模型完成延迟未承诺1秒，真实性能待校时后验证。用户要求从根源解决反复时差，持久Windows原生64秒同步工具已备，管理员Install尚未执行。[当前规格](JEV_ONE_SECOND.md)、[持续校时](TRADING_CLOCK_MAINTENANCE.md)。下文60秒为旧阶段。

2026-10-08最新用户确认：止盈、止损时机暂交给JEV，不要求用户先制定固定比例或价格。每轮持仓决策显式注入该职责与均价/盈亏/资金费/手续费，选择部分减仓或全平；程序只保留资金纪律、执行/费用守卫和故障暂停。参数问题集已到futures-plan-v3，当前60秒周期，没有交易所保护挂单。动态杠杆和无到期累计费用已迁移，8776新版同库、原账户/费用保留且暂停；真实自主退出未收费验证。当前规范[JEV_MANAGED_EXITS.md](JEV_MANAGED_EXITS.md)与[JEV_CONTINUOUS_DYNAMIC_PLAN.md](JEV_CONTINUOUS_DYNAMIC_PLAN.md)，下文到期/固定2倍及旧缺口属于历史。

2026-10-08 15时接续：真实JEV通用Paper周期、共享费用账本和独立Web已联调，7轮有效WAIT、暂停/恢复及真实接口失败自动暂停通过。当前是虚拟USDT合约执行，原总览/Flash/可选JEV建议仍独立，未开启真实资金或Testnet后端。模型预算接续追加不可变grant，不清空原消费/未知预留；页面给出封顶、剩余、到期和固定错误码。当前因传输失败暂停，响应校验细节与公共时序/代理长稳待完善；2次已收费不合格结果不能计为有效决策。最新范围 [JEV_FUTURES_REAL_TRIAL.md](JEV_FUTURES_REAL_TRIAL.md)、[REMAINING_WORK.md](REMAINING_WORK.md)，以下未调用真实模型的描述属于此前阶段。

2026-10-08最新：通用USDT合约主体、独立维护/模型任务、Paper后端和合约Web已经装配；四候选、确定性数量/纪律、历史摘要/账户/0–100版本、费用门控与未知回执恢复接上。用户要求精简测试，80/123subtests通过；未做全套或24h。当前预览公共行情+Mock WAIT，真实模型/资金不启用。说明 [FUTURES_TRADING_CORE.md](FUTURES_TRADING_CORE.md)，剩余任务 [REMAINING_WORK.md](REMAINING_WORK.md)。下文为阶段历史。

2026-10-08通用执行基础已交付，中立事实/执行账户Ports与Paper后端通过共用持久通道工作，保留旧模拟计算；最终1450项/154subtests。验收 [TRADING_EXECUTION_VERIFICATION.md](TRADING_EXECUTION_VERIFICATION.md)。持续维护/JEV/合约Web与Testnet仍待装配，费用与真实资金未启用。下段规划状态仅保留前次讨论。

2026-10-08架构修订：用户要求通用交易主体。合约操盘共用决策、纪律、订单生命周期和运行控制，行情输入与执行/账户分别使用可替换Ports；本地Paper模拟内核是首个执行后端，后续Testnet/Agent OS通过后端接入。仅切换行情不能产生虚拟成交或钱包。设计 [TRADING_CORE_ARCHITECTURE.md](TRADING_CORE_ARCHITECTURE.md)，优先级 [REMAINING_WORK.md](REMAINING_WORK.md)。本轮只是规划修订，通用接口与服务尚未实现，Paper计算与公共数据的历史验证不作为解耦证明；真实资金及到期费用仍未启用。

2026-10-07当前：独立合约运行公共数据层已完成，所选USDT永续的mark/book、公开交易filters和已结算Regular资金费有Typed Port/Adapter与真实ETH/SOL证据；[行情规格](FUTURES_MARKET_SPEC.md)、[验收](FUTURES_MARKET_VERIFICATION.md)。正在接续合约Paper后台/JEV/Web，数据层没有启动用户操盘。当前未完成列表 [REMAINING_WORK.md](REMAINING_WORK.md)。

2026-10-07最新：USDT合约Paper离线领域与SQLite已交付，支持单向逐仓LONG/SHORT、1–20倍、reduce-only、资金费与清算；规则来源明确为固定模拟，资金/风险/风格和操盘版本均受确定性控制。[合约Paper规格](FUTURES_PAPER_SPEC.md)、[内核验收](FUTURES_PAPER_KERNEL_VERIFICATION.md)。尚需实时合约Provider、独立JEV候选/后台及Web装配；该完成项没有开启用户交易或自动续期到期费用。下文“合约Paper仍待开发”指本节之前的阶段，后续接入缺口继续有效。

2026-10-07补充：合约为新会话默认，Spot仍可显式选择且旧事实保留。JEV界面逐处区分市场/币种；合约隐藏现货钱包与成交，身份读取失败禁用操作。已完成界面/查询隔离，接续USDT合约Paper内核，尚不能自动执行合约模拟。

2026-10-07最新范围：新会话支持全部正在交易的Binance **USDT永续合约**，报价、资金/权益、模拟钱包统一USDT；数量与多空方向、保证金、杠杆按合约记录。币种在创建时选定，默认近7天已收盘1h历史，可选1/7/30天。首次历史交给独立Flash分析接口，不阻塞可选JEV建议或独立JEV操盘。详细规格[SESSION_MARKET_SPEC.md](SESSION_MARKET_SPEC.md)。本文后文Spot默认/是否改合约的未决项只保留原阶段历史，已由本项选择覆盖。

当前已交付动态目录、真实历史、首次任务与页面；Flash收费调用尚未装配，合约只读账户和USDT合约Paper仍待开发。旧BTCUSDT现货会话/钱包不重解释为合约，新合约会话拒绝旧Spot交易路径。缺模型配置只准备历史并明确显示未调用；缺完整历史不调用模型。用户已授权先Paper再Testnet的自动操盘验证，未授权真实资金交易。

2026-10-06最新补充：独立JEV操盘优先本地Paper，完成后接Testnet。当前JEV_PAPER_SPEC.md定义并已交付独立后台、持久虚拟钱包与硬纪律、真实模型显式配置/预算及本地操作页面；真实试跑待累计预算和本机Key。单次收费上限0.02 USD，不自动提高或跨上海预算日续期；强模型仍禁用。模型/报价/钱包来源必须明确保留，不用Mock冒充真实；实际交易所订单和Agent OS操控尚未装配。

最新两页面/三模块补充以JEV_PARALLEL_PRODUCT.md为准：Flash常态主分析；JevAdvice可选建议；JevTrader独立操盘，两开关互不控制。先前将建议开关与自动模式绑定的语义已撤销。

状态：准备规格已交付；用户于2026-10-05授权自主继续推进，从基础架构实现开始。运行环境和依赖仍由用户安装。
准备开始：2026-10-04；修订：2026-10-06，Asia/Shanghai。
开发根目录：`D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent`。

本规格将项目内 `readme.md`、外层初步计划和本次补充选择合并。
若存在冲突，以用户最新明确要求为准，再以本规格为准。

## 1. 已确认的产品目标

这是辅助本人在 Binance 上做 BTC 短线交易的单用户 Agent。
系统持续读取市场与账户，输出可追溯的建议。用户追加建议/自动操盘两个模式；自动执行先testnet，真实资金自动执行未授权。建议模式由本人执行。
建议、实际成交和复盘之间建立可查询的关系，帮助检验交易纪律。

| ID | 要求 | 来源 |
| --- | --- | --- |
| R01 | 提供建议，真实交易由人执行 | 原始 README |
| R02 | 24/7 监控，高频数据处理与低频模型调用分离 | 原始 README、初步计划 |
| R03 | 按场景选择模型/API，控制费用 | 原始 README |
| R04 | 区分 human-made 与 agent-made 决策，记录真实成交时间 | 原始 README |
| R05 | 复盘可以在以后重新分析，但不能覆盖原始事实 | 原始 README |
| R06 | 创建会话时用0–100整数滑杆选择保守到激进，运行时可修改 | 原始 README |
| R07 | 持仓时输出 HOLD/SELL/BUY 建议，保留 JEV 能力 | 原始 README |
| R08 | 账户安全第一，确定性纪律不能由模型或风格绕过 | 原始 README、初步计划 |
| R09 | 独立内核；Condor 是代码与设计参考，Hummingbot 非核心依赖 | 初步计划 |
| R10 | Binance Agent OS 为可替换的能力 Provider | 初步计划 |
| R11 | 第一版入口为本地 Web 页面 | 本次用户确认 |
| R12 | 第一版接 Binance 只读 API | 本次用户确认 |
| R13 | 环境使用 Conda，依赖由用户安装 | 本次用户要求 |
| R14 | JEV 是 TypeSafe 的结构化决策模型；需专用接入与评测，不能冒充交易指标或声称已接通 | 用户2026-10-06澄清及官方核对 |
| R15 | Jev为独立可开关持仓决策模块，不等待Flash；关闭后Flash继续分析 | 用户2026-10-06追加 |
| R16 | 两个模式：建议/自动操盘；自动按配置下单，先验证testnet，强模型暂不调用 | 用户2026-10-06追加 |

R15/R16覆盖后文历史只读首版与默认串行规划中的冲突部分。具体边界见JEV_INDEPENDENT_MODULE.md；实现接续见JEV_INDEPENDENT_IMPLEMENTATION.md。模式/Jev持久Web设置已实现，真实模型/独立后台/实际testnet执行仍未接通。

## 2. 默认假设与范围

继承初步计划的默认市场：Binance Spot，首个交易对 `BTCUSDT`。
这不是本次对 Futures 的确认。若首版改为合约，需要先修订账户、仓位、
杠杆、保证金、资金费率、强平与风险规格；不能将现货余额当作合约仓位。

首版包括实时公共行情、只读账户和成交同步、确定性特征、建议与解释、
会话风格、本地 Web、决策归属、复盘版本、费用统计及恢复。
离线 Replay 是开发和测试基础，paper 是明确标注的验证模式。

首版不接真实下单、撤单、划转、提现，不接 DEX/钱包，不做多用户、
多交易所、复杂多 Agent、Telegram 或复制 Condor Dashboard。
交易自动化需要单独规格和授权，不因完成首版而自动开启。

## 3. 用户主流程

1. 通过 Conda 启动后端，打开本地 Web。
2. 查看行情、账户同步状态和数据时间；没有凭据时仍可查看公共行情。
3. 创建 Agent 会话，选择交易风格；按个人纪律配置约束。
4. Agent 在定时或事件触发时生成建议，展示证据、失效条件和限制。
5. 用户可记录采纳、拒绝、修改或独立做出的交易想法。
6. 用户在 Binance 手工交易；系统只读同步订单和真实成交。
7. 用户将成交与先前建议关联，或标为独立决策；系统生成交易复盘。
8. 到达复盘回访时间或手工请求时，生成新复盘版本并保留旧版本。

数据缺失时展示“不可评估”及具体原因，不能用 HOLD 表示系统没有数据。
SELL 在现货中表示减持已有 BTC，不代表做空；BUY 表示增持建议。
建议的数量可为空。未配置资金与风险约束前，不提供具体仓位数量建议。

## 4. 内核与 Provider 边界

```text
Binance 公共 WebSocket ─┐
Binance 只读 REST ──────┼→ Adapter → 自有 Market/Account/Trade 模型
可选 Agent OS MCP ─────┘                 ↓
                                聚合与确定性特征
                                         ↓
                           Scheduler → Session → Decision
                                         ↓
                                硬纪律与建议校验
                                         ↓
                      SQLite Journal / Web / 反馈 / 复盘

Fake / Replay 使用相同 Ports，脱离 Binance、MCP、LLM 仍能运行。
```

- `domain`：标准库、Pydantic 和自身；不得依赖 Ports 或具体 Provider。
- `ports`：领域模型与结构化接口；不得隐藏 Adapter 依赖。
- `application`：自身、领域模型及 Ports；不导入传输或存储实现。
- `runtime`：生命周期、调度及事件分发，通过服务与 Ports 工作。
- `adapters`：REST、WebSocket、MCP、LLM、SQLite、通知的具体实现。
- `bootstrap`：唯一的依赖装配入口；Web 仅调用应用服务。

Binance Agent OS 不拥有本系统的策略、Memory、Loop、Session 和业务状态。
MCP 支持的账户与授权范围须实际验证；第一版 Direct 只读接入是主链路。
生产运行不依赖 Codex 桌面已经连接的 MCP，也不继承其授权令牌。

优先参考 Condor Session 锁与取消、Tick 的 gather/decide/persist、
风险与权限分离、确认和日志设计；不复制 ConfigManager、SDS、
fetchers、Hummingbot MCP 或 Bot/Controller/Executor 体系。
具体源码盘点见 [CONDOR_EXTRACTION.md](CONDOR_EXTRACTION.md)。

## 5. 领域对象与事实归属

金额、价格、数量、费用使用 `Decimal`，持久化与 API 用十进制字符串。
时间存带时区 UTC；界面展示 Asia/Shanghai。区分交易所事件时间、
接收时间、建议时间、用户反馈时间、成交时间和复盘时间。

| 对象 | 必需语义 |
| --- | --- |
| Instrument | venue、market_type、symbol、base/quote、交易过滤器和版本 |
| MarketEvent | 来源事件 ID、事件/接收时间、类型、规范化价格与数量 |
| MarketSnapshot | as_of、窗口、有效性、缺口与数据来源 |
| FeatureSnapshot | 指标值、算法版本、预热状态、输入快照引用 |
| AccountSnapshot | account_ref、钱包范围、余额、as_of、同步状态 |
| PositionView | 现货持有量、估计成本及成本可信性；不伪装交易所原生仓位 |
| ObservedOrder / ObservedTrade | 交易所 ID、账号与品种、状态/成交事实、手续费 |
| AgentSession / StyleRevision | 状态、风格、修订号、纪律配置及变更时间 |
| DecisionSnapshot | 有限市场/特征/账户上下文、风格修订、触发原因 |
| Recommendation | BUY/SELL/HOLD 或 UNAVAILABLE、证据、风险、有效期、引用 |
| DecisionFeedback | 采纳/拒绝/修改、用户说明、关联建议 |
| TradeAttribution | 原始建议者、最终决策者、执行者和用户确认的关联 |
| ReviewRevision | 基于哪些事实、数据截止时间、父版本、追加说明 |
| ModelUsage | 请求 ID、路由原因、模型版本、token、估算/实际费用 |
| JournalEvent | schema_version、event_id、aggregate_id、payload、UTC 时间 |

导入成交默认 `UNCLASSIFIED`，不能因成交靠近建议就自动归为 agent-made。
真实首版执行者始终为 HUMAN；“agent-made”只代表想法/建议来源。
用户采纳或修改建议也不能改写其原始作者。归属纠正用追加事件记录。
重复成交用 `(venue, account_ref, symbol, trade_id)` 去重，不以时间去重。

充值、转账或未覆盖的旧成交会造成成本不完整。余额仍可显示，但成本和
盈亏应标为 UNKNOWN/PARTIAL；不得强行将成本设为 0 或把余额变化当成成交。
未来 paper、testnet、live 事实必须有独立 mode 和账号命名空间。

## 6. 数据、特征与调度

首版订阅 BTCUSDT 的成交、最优买卖价和 1 分钟 K 线。
先用 best bid/ask、价差和挂单量作盘口摘要；完整深度同步为后续扩展。
WS 实时接收，内部 1 秒生成快照；聚合 5 秒和 1 分钟窗口。
窗口默认保留 120 根已收盘 1 分钟 K 线和 300 个 1 秒采样。
WS 重连用 REST 补齐可获取的 K 线；补不齐的窗口标缺口，不造数据。

初始指标：区间收益、EMA(12/26)、ATR(14)、窗口 VWAP、
收益波动率、成交量变化、best bid/ask 与价差。
算法与窗口定义见后续实施计划；预热不足不填 0。所有指标由代码计算。
离线回放使用事件时间，不访问未来 K 线；同一输入和版本结果可重复。

建议调度默认：空仓每 300 秒评估，持仓每 60 秒评估；事件可提前触发。
每会话同类事件冷却 30 秒，最多一个在途模型请求；重复事件合并，
过期排队触发丢弃。使用单调时钟计时，用 UTC 记录事实。
不同会话共享行情缓存，首版只运行一个活跃交易会话。

行情超过 5 秒未更新标过期；账户超过 60 秒未成功同步标过期。
账户初始每 15 秒轮询并按 API 限额调节，用户数据流验证后可减少轮询。
这些是工程默认值，不是交易策略参数；可配置并以明确测试验证。

## 7. 模型、预算与纪律

流程：确定性过滤 → 触发 → 选择路由 → 预算预留 → 模型调用 → 严格解析 → 风险校验。
平静市场优先规则与缓存；普通解释可走 economy 模型；复杂持仓变化可
配置 standard 模型；复盘走 review 模型。用户已选择 OpenRouter 与 DeepSeek V4.1 Flash，Jev 指 TypeSafe 决策模型；生产 provider/有效价格配置、强模型和费用上限仍待确定。
规则决定是否升级，模型不得自行购买或切换到更贵的模型。
拟采用 Flash 起草、Jev 结构化判断，详见[模型选型与接入方案](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/docs/MODEL_SELECTION.md)。用户2026-10-06要求强模型可选择但暂不调用；本版本保存ID且固定禁用，无启用开关，需要升级时由人工复核。主体Adapter/预算执行器已离线实现，完整发布级联待做。

- 日预算默认 0 USD，显式配置后才允许收费请求。
- USD 费用账本使用 Asia/Shanghai 日边界，记录已花费和在途预留。
- 路由配置含端点白名单、模型名、单次上限、每日上限、超时和回退顺序。
- 单次模型请求超时默认 15 秒，最多一次有预算覆盖的回退。
- 不确定是否计费的超时保守计入预留，不因重启或重试抹去费用。
- 默认最多 60 次模型请求/小时，全局共享；复盘和手工请求也计入。
- 调用次数与预算一起持久化；未确定计费的超时不能通过重启逃避小时额度。
- 费用预留采用输入保守上界与最大输出token，模型价格必须有版本；实际账单与估算分开，超出估计时冻结后续收费并明确记录。
- 输入只包含有限结构化快照；API Key、Secret、Token 和完整日志不得入 Prompt。
- 输出声明 evidence_ids、有效期及数据不足项；无有效证据时不可输出增持建议。
- 失效、解析失败、预算耗尽、模型不可用时降级为规则提醒或 UNAVAILABLE。

风险规则独立于模型，禁止未经配置的具体仓位、超出用户上限的建议、
现货卖空建议及基于过期数据增加风险。硬风险提醒由代码触发，不依赖 LLM。
“绝对纪律性”落实为软件可执行约束；首版无法阻止用户在 Binance 手工操作，
违规实际交易应记录并在复盘中明确指出。

风格使用0–100整数强度：0最保守，100最激进，标签不替代原始数值。风格带版本，不能放宽硬风险上限。
初次创建必须明确确认滑杆值；显示初值50不代表默认同意。运行中修改由用户发起，原子记录版本变化，详见STYLE_CONTROL.md。
旧请求返回时若风格或账户余额/持有量等实质状态已经变更，标 SUPERSEDED，
不发布为最新建议；仅刷新相同账户数据的采样时间不会形成无意义的状态修订。
具体信号阈值需通过历史样本验证，未定义前只改变解释和明确标记的测试配置。

## 8. JEV、反馈与复盘

用户于2026-10-06明确 JEV 为2026年9月发布的专门决策模型，官方核对对应 TypeSafe Jev。拟使用 OpenRouter 上固定版本 `typesafe/jev-1.13`，通过独立 Decisions API 与专用 Port/Adapter 接入；它返回有限候选项、评分和概率，不承担价格/数量计算或自由文本解释。
问题、判断标准、模型版本与升级阈值需要版本化并经过标注样本验证。模型 confidence 不能显示成交易胜率；Jev 和强模型均不能覆盖资金纪律。
当前Prompt v2使用 `jev_status=not_connected`，v1历史重放与独立Replay保留unspecified；现有总览/离线smoke仍保留旧unspecified状态，M3迁移配置/连接/能力投影。专用Port/Adapter与预算执行器已离线验证，尚无真实Jev结果。历史快照不改写，禁止以EMA/ATR或Fake结果声称Jev已接通。

复盘由真实成交与当时可见的建议/特征构建。原始决策快照不可覆盖。
回访默认由用户选择手工或成交结束后 1 小时/24 小时；调度任务持久化，
任务键 `(trade_group_id, review_kind, scheduled_for)` 防止重启重复生成。
新版本注明“事后补充”和数据截止时间，区分当时证据与后见信息。
评价盈亏之外还比较：纪律遵守、偏离建议、过期建议、成本缺口与费用。
复盘不自动修改正在运行的策略或风格。

## 9. 本地 Web 与安全

首版 FastAPI + Jinja2 + 原生 JavaScript，REST 操作、SSE 更新。
无需 Node.js 或 React。图表先用轻量 SVG/Canvas，数据格式保持可替换。

页面包括总览、当前会话/建议、账户/成交、决策归属、复盘版本和系统状态。
状态页显示数据时间、断线、预热、预算、模型路由与同步缺口。
不提供 Binance 下单按钮、API Key 回显或浏览器侧签名。

默认绑定 `127.0.0.1`。本地会话 cookie 使用 HttpOnly/SameSite，
写入本地状态的请求要求 CSRF 与 Origin/Host 验证；默认不开放跨域。
持久化内容须输出转义，MCP/模型结果是非可信数据，不能触发新权限或任意命令。
本地 HTTP 下不假装启用了 HTTPS-only cookie；未来远程访问须单独配置 TLS 和认证。

Binance 凭据只保存在用户控制的运行环境或系统 keyring，后端读取。
只读 API Key 关闭交易、划转和提现；可用时配置 IP 限制。
应用对签名 REST 请求实施 GET 与具体路径白名单，不能根据 LLM 文本调用任意 URL。
即使 Key 意外有交易权限，首版程序也不注册实际交易能力。
签名查询、API Key、授权头、账户敏感标识和模型密钥都必须从日志/导出中清除。
密码、Token 和 Secret 不进入 SQLite、浏览器状态、截图报告或 Git。

## 10. 存储、恢复与 24/7

单机 SQLite WAL，事件 append-only、查询投影可重建；领域层不依赖 SQLite。
数据库迁移有版本，升级前备份。敏感业务数据本地存储，备份由用户控制。
初始保留：原始采样 7 天、1 分钟聚合 90 天、建议/成交/复盘长期保留；
这是容量默认值，可配置。清理原始数据前保留复盘所需快照和来源哈希。

运行状态：STARTING/RUNNING/DEGRADED/PAUSED/STOPPING/STOPPED。
重启恢复活跃会话、风格版本、导入游标、预算预留和复盘任务；
重新拉账户后才恢复持仓建议。SQLite/审计写入失败时停建议并显示错误。
只读服务暂停后可继续显示缓存，但不能标记为实时。

24/7 的前提是设备开机、不休眠、网络可用。Windows 开发阶段用 Conda
前台验证；长期运行阶段再由用户设置后台 supervisor/任务计划程序。
Windows 后台服务不弹窗口。关闭浏览器不应停止监控；通知首版为本地 Web，
浏览器关闭期间不能承诺实时送达。Telegram/系统通知为后续独立选择。

## 11. 首版验收

- 真实 Binance 公共行情与只读账户能在本地 Web 展示，数据时间准确。
- 行情、模型、MCP 的故障相互隔离；Agent OS 不可用不阻断主链路。
- 人工/Agent 建议、真实成交、paper 结果及未知归属不会混淆。
- 1 秒采样不导致 1 秒一次模型请求；费用在重启后仍受预算约束。
- 风格切换不会发布旧请求结果；过期/缺失数据明确不可评估。
- 同一离线输入有相同指标和规则结果；LLM 回放使用录制响应或 Fake，
  不承诺实时模型输出逐字一致。
- 24 小时只读 soak 验证恢复、内存、日志轮转和重启，全部凭据不出日志。
- 自动化测试默认禁止网络；真实只读验收由用户提供本地凭据后单独运行。
- 首版没有任何真实资金写入能力。

## 12. 待用户后续决定

| 决定 | 默认处理 | 何时需要 |
| --- | --- | --- |
| Spot 是否改为合约 | 继承初步计划的 Spot 默认 | 领域模型实现前 |
| 实际使用的账户/钱包 | 账号范围明确后才联调 | 只读账户联调前 |
| 路由强模型、provider/价格及预算 | OpenRouter/Flash/Jev已明确；离线接入可推进，收费关闭 | 首次收费调用前 |
| 个人资金纪律和数量上限 | 输出观点，不输出具体仓位数量 | 数量建议启用前 |
| Jev 问题标准与升级阈值 | 模型含义已明确，专用接入与评测待做 | Jev 建议发布前 |
| 长期运行机器和通知渠道 | 本地 Web 开发验证 | 24/7 使用前 |
| Agent OS 授权及主账户读取可用性 | Direct 主链路，MCP 可选 | MCP 账户联调前 |

## 13. 核对过的官方资料

核对日期：2026-10-04至05。接口应在实施与联调前再次核对，文档介绍不等于账户实测。

- [Binance Agent OS 官方公告](https://www.binance.com/zh-CN/support/announcement/detail/07d45cdd3831498f8a4ff339031a8480)：Agent OS 包含 MCP 和多种工具，MCP 是其中的连接层。
- [Binance MCP 官方文档](https://developers.binance.com/en/docs/agent-native/mcp-server/agentic)：专用 Agentic 子账户与可选主账户只读视图；权限和账户范围不能当作普通 REST Key 的同义物。此处不假设测试网或长驻程序刷新凭据的能力。
- [Spot REST 账户接口](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account)：`GET /api/v3/account`、`GET /api/v3/openOrders`、`GET /api/v3/order`、`GET /api/v3/myTrades` 是只读查询，账户接口需要签名。
- [Spot 公共行情流](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-streams/~)：成交、bookTicker、Kline 的当前 Schema 是 normalizer 的依据。
- [Spot 用户数据流](https://developers.binance.com/en/docs/products/spot/user-data-stream)：当前文档从 WebSocket API 订阅入口说明用户事件；实施时验证受限凭据能使用的方法，失败后保留只读 REST 轮询。不能照搬旧 listenKey 流程。
- [Conda 环境管理](https://docs.conda.io/projects/conda/en/latest/user-guide/tasks/manage-environments.html)：独立环境、YAML 创建和环境导出。
