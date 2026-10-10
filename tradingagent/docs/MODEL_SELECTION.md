# OpenRouter、Flash 与 Jev 接入方案

2026-10-09当前选择：原Flash分析角色改为 **OpenRouter `anthropic/claude-haiku-5.5`**，背景供应商固定Anthropic，结构化输出、关闭额外推理、价格及累计费用守卫保留。新配置默认Haiku；原DeepSeek配置/历史档案兼容读取，不能作为Haiku的新背景缓存。JEV仍为`typesafe/jev-1.13`，强模型仍仅保存选择且禁用。正常工作台一次真实Haiku→JEV验证成功，见[JEV_MULTI_SCALE_RUNBOOK.md](JEV_MULTI_SCALE_RUNBOOK.md)。以下选型/报价及未接通描述为历史记录。

用户最终结构：Flash常态主分析、可选JevAdvice、独立JevTrader；两个主页面分别为大盘分析与操盘。模块互不绑定；本文件旧两模式表述以JEV_PARALLEL_PRODUCT.md覆盖。

核对日期：2026-10-06，Asia/Shanghai。Flash/Jev Adapter与预算执行器已离线验证。最新要求为Jev独立可关闭、建议/自动两模式，自动先testnet；配置及持久Web设置已实现，独立后台/实际执行与真实联调尚待开发。未调用收费接口。

## 1. 用户已确认与本次建议

- 用户选定 OpenRouter 与 `deepseek/deepseek-v4.1-flash`。
- 用户澄清 JEV 是 2026 年 9 月发布的专门决策模型。根据发布时间、用途及官方资料，对应 TypeSafe 的 Jev；不再将其作为待定义的交易指标。
- 建议使用固定模型 ID `typesafe/jev-1.13`，用于有明确候选项和标准的结构化决策。上线前记录服务返回的实际模型版本，不使用会自动升级的 latest 别名。
- 建议用 `anthropic/claude-opus-5.5` 做低频复杂决策复核；预算优先时改用 `anthropic/claude-sonnet-5.5`。强模型是候选方案，用户尚未选定。
- 用户最新要求（2026-10-06）：强模型做成可选择模块，暂时不调用。当前开发只保存选择并固定关闭调用，不将Jev的升级建议自动执行为强模型请求；主体框架实施见MODEL_FRAMEWORK_IMPLEMENTATION.md。
- 日预算、单次上限、首次真实联调总额仍未确认；当前实际预算保持 0、收费关闭。建议试运行上限为每日 2 USD、单次 0.10 USD、首次联调合计 1 USD，只有用户确认后才应用。

此处的推荐是架构与成本取舍，尚无本项目实测证明任何模型具有更好的 BTC 交易收益。

## 2. 模型分工与价格证据

价格为核对时的美元/百万 token；不含充值费用等账户侧费用，不假设缓存折扣。实施及真实联调前重新取得有效价格版本。

| 模型 | 拟承担的工作 | 核对价格：输入 / 输出 | 状态与依据 |
| --- | --- | --- | --- |
| DeepSeek V4.1 Flash | 常规快照分析、结构化候选建议、中文解释和复盘草稿 | DeepSeek provider 示例为 0.30 / 1.20；不同 provider 价格不同 | 用户选定；[OpenRouter 模型页](https://openrouter.ai/deepseek/deepseek-v4.1-flash) |
| TypeSafe Jev 1.13 | 候选建议与策略语义的一致性判断、弃权或升级判断 | 0.042 / 0 | 用户明确要决策模型；[OpenRouter 模型页](https://openrouter.ai/typesafe/jev-1.13) |
| Claude Opus 5.5 | 多周期信号冲突、关键持仓变化、复杂决策的独立复核 | 4 / 20 | 推荐低频启用；[OpenRouter 模型页](https://openrouter.ai/anthropic/claude-opus-5.5) |
| Claude Sonnet 5.5 | 替代 Opus 的日常复核候选 | 2 / 10 | 预算优先替代方案；[OpenRouter 模型页](https://openrouter.ai/anthropic/claude-sonnet-5.5) |

Flash 页面汇总最低价不能直接用于所有 provider 的费用上界。生产配置需要 provider 白名单、允许的价格上限、能力核对及有效期；不把促销、缓存命中或自动 fallback 当作稳定价格。

举例：若一次调用实际为 8,000 输入 token、1,000 总计费输出 token，按上表示例价格，Flash 为 0.0036 USD、Opus 为 0.052 USD、Sonnet 为 0.026 USD；Jev 若输入为 10,000 token，则为 0.00042 USD。这只是固定 token 数的算术示例，不是本项目真实请求报价。实际预留必须包含完整输入、推理与可见输出上限，以及链路中的每一次调用。

## 3. Jev 的含义及接入边界

[TypeSafe 官方说明](https://docs.typesafe.ai/concepts/system-one)将 Jev 定义为非生成式的 System One 模型：输入状态与问题，返回 Choice、Score 或 Noul 等结构化回答及概率，不生成长篇解释。它需要我们定义候选答案和判断标准；价格、数量、指标计算以及资金纪律由代码完成。

[OpenRouter Jev 文档](https://openrouter.ai/docs/guides/community/jev)确认同一个 OpenRouter API Key 可以访问 Jev，无需另外开通 TypeSafe 账户。接口有两种：

- Decisions API：`POST https://openrouter.ai/api/alpha/decisions`，使用 `model`、`state`、`questions`。本方案按该接口设计专用 HTTP Adapter，alpha 路径需要单独的版本与兼容性检查。
- System One API：`POST https://openrouter.ai/api/v1/systemone`，用于 TypeSafe SDK 切换 base URL 等场景。不能将两条接口的请求/响应 Schema 混用。

Jev 不是 `/api/v1/chat/completions` 的普通聊天模型。`typesafe/jev-router` 是另一产品，会选择其他模型和推理强度；本项目第一阶段不使用它，以免替换自身的预算与升级规则。

Jev 的 confidence 不等于概率表中最高类别概率，也不等于 BTC 上涨概率、盈利概率或交易胜率；[官方 confidence 文档](https://docs.typesafe.ai/confidence)说明二者语义不同。阈值必须在本项目标注样本上评估，不能任意规定“超过 0.8 就买入”。

## 4. 独立双路径与两种模式

用户最新要求取代默认Flash→Jev串行级联。详见JEV_INDEPENDENT_MODULE.md；独立后台与执行器未完成，当前已实现配置/开关门控及持久Web选择页。

1. 共享版本化行情、账户、特征、策略与风格快照；代码检查数据新鲜度和硬纪律。
2. Flash主分析与Jev持仓决策分别调度；Jev不等待Flash输出，输入为当前原始事实与已确认的策略标准。
3. 建议模式只呈现可验证判断，由用户执行。Jev关闭时Flash继续主分析。
4. 自动模式本阶段仅testnet：Jev有限判断→确定性风险/参数/身份核验→执行器。Jev关闭时自动执行暂停，Flash继续分析，不隐式转为Flash下单。
5. 强模型只保存选择，不自动调用。未配置策略、限额、账户或执行器时自动资格为blocked；不会将模式选择显示成正在交易。
6. 发布/提交前再次核对账户scope、风格、策略、模式、启用版本与时效。保留每次费用与审计；旧在途结果关闭后重新开启仍不能使用。

候选答案、阈值和升级条件需要版本化。Jev 不能覆盖确定性风险内核，强模型也不能绕过。0–100 风格只能影响已获验证的策略参数与解释，不能改变资金上限或数据要求。

[OpenRouter 官方级联示例](https://openrouter.ai/docs/cookbook/evaluate-and-optimize/jev-verified-cascade)可作为未来研究参考；该示例不再是本项目默认实时持仓路径，也不是BTC策略效果验证。

## 5. 当前代码缺口与开发顺序

已有单请求路由、费用预留/结算、限流、风险校验、冻结快照和发布前重验可复用。T01–T16 的离线通过不代表以下新增链路已通过。

| 阶段 | 待开发内容 | 必须先验证的行为 |
| --- | --- | --- |
| M1：OpenRouter 基础接入 | 配置/Transport/Chat Adapter已实现；启动仍不装配执行器，生产provider/有效价格待配置 | Fake HTTP、严格Schema/usage、失败费用、完整wire上界、密钥回显与禁用边界已验证 |
| M2：Jev 接入 | 独立Port、Choice/Score/Noul、Decisions Adapter与持久预算执行器已实现；真实连接/语义评测待做 | 概率/类型/问题集/实际模型版本、绕过值、晚到/价格失效及取消结算已验证 |
| M3：独立Jev与模式控制 | JI1配置门控/JI3A持久Web设置已实现；JI2独立持仓后台、完整判断展示仍待开发；强模型不调用 | 两条lane不互相等待，每个调用计入费用和小时额度；旧版本/过期结果拒绝；Jev关闭零新调用、auto暂停 |
| M4：配置与离线评测 | 用户策略/纪律/模型配置入口；标注样本、固定事实回放与模型对比 | 无策略不虚构 HOLD；无数量上限不输出数量；缺失上下文选择弃权；语义合格率、错误放行率、升级率、延迟和真实费用分别报告 |
| M5：真实联调与验收 | 用户批准限额后，最小收费样本；Binance WS/只读账户联调；真实 24h 运行 | 真实请求与账单、权限、数据时效、恢复和费用证据完整；未完成项目继续标为缺口 |

对应当前代码：

- [ports/model.py](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/agent_platform/ports/model.py)提供生成式 `generate`；[ports/decision_model.py](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/agent_platform/ports/decision_model.py)提供独立typed `decide`。两者分别有OpenRouter Adapter，不伪装Jev为自由文本。
- [application/routing.py](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/agent_platform/application/routing.py)只按目的和账户事件选择单个 economy/standard/review 档位；尚无 Jev 或候选建议驱动的升级链。
- [application/decisions.py](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/agent_platform/application/decisions.py)现有请求总截止窗口最多 15 秒；不能把慢速多调用链硬塞进去或仅延长超时。需要明确快路径与复杂复核的时效，持续保留发布前重验。
- [application/prompting.py](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/agent_platform/application/prompting.py)当前生成v2，明确Jev为独立模型、`jev_status=not_connected`；v1原文/unspecified仅用于历史重放。独立后台实际接入并验证后才显示Jev判断。

分析模型在本版本固定Flash，决策模型固定Jev；只有strong_model可选择，且不能与两者重叠或启用。Chat要求显式provider白名单、数值max_price及足够prompt_overhead_tokens；完整序列化请求（含Schema）字节数+1024超过预留时，派发前拒绝并结算确认零费用。正常离线样例使用4096 overhead；生产仍需重新核对价格/能力和实际usage，不把测试值当自动收费配置。

本机 Conda 环境已核实有 `httpx 0.28.1`，初步 HTTP 接入不需要新增 SDK。开发和测试继续使用 `C:/Users/exile/anaconda3/envs/tradingagent/python.exe`，不安装或升级依赖。

## 6. 费用、时效和数据控制

- 每个子请求先获取已验证价格并原子预留费用；Jev 输入/输出费用单独计算。超过单次、事件累计或每日上限时不派发，重启后也不抹去预留。
- 保留当前全局 60 次请求/小时约束；Jev、解释、复核和手工请求均计数。每次监控采样不自动触发整条模型链。
- [OpenRouter reasoning 文档](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)明确推理 token 计入输出费用。`reasoning.exclude` 只影响返回内容，不代表免费；输出为空或被截断也可能计费。
- Chat Adapter 使用已核对的 provider 能力与 `require_parameters` 等约束，不接受 provider 悄悄忽略结构化输出。价格上限和数据策略参考[官方 provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)，在 Jev 接口上不能未经核对复用 Chat 的全部 provider 参数。
- 不依赖模型页的 P50 或首 token 延迟承诺整个决策链 15 秒完成。过期或状态变更的结果保留费用和审计，撤去当前行动资格。
- 对模型只提供本次需要的有限快照，不发送 Key/Secret、完整日志或无关账户信息。外部文字均视为数据，不允许改变策略、费用或权限。

## 7. 用户需要补齐什么

| 用户事项 | 最晚需要的时间 | 缺失时处理 |
| --- | --- | --- |
| OpenRouter Key 与账户余额/Key 费用限制，只在本机设置，不发聊天 | 小额真实模型联调前 | 继续 Fake HTTP 与录制数据，保持 paid 关闭 |
| 日预算、单次上限、联调总额；强模型 Opus 或 Sonnet | 实际收费启用前 | 当前 0 USD；候选强模型不自动启用 |
| 实际策略：周期、允许的入场/退出条件、证据要求 | 可执行建议验收前 | 只做配置/回放，不把测试 EMA/ATR 阈值当正式策略 |
| 单次投入/最大 BTC 敞口/每日亏损等个人资金纪律，以及必要费用证据 | 具体数量建议启用前 | 数量不可评估；不能由风格推算资金比例 |
| Binance 只读 Key/Secret、实际账户范围与公共 WS 网络可用性 | 实时数据/账户验收前 | Fake/录制数据，明确非实时；密钥不进入聊天 |
| 运行主机和连续不休眠条件 | 24h 验收前 | 仅承认已实际经过的短测 |
| 完整历史库存、转入转出、费用与必要 FX 证据 | 完整历史 PnL 验收前 | UNKNOWN/PARTIAL，不填零；可先验证实时只读建议主链 |

Binance Agent OS 的真实 SDK/授权和只读工具映射仍需另行联调；当前 Direct Binance 主链可先完成首版，MCP 不阻塞模型 Adapter 的离线开发。

以上配置中的数值必须由用户明确提供或确认。API Key 到位并不意味着开发完成；仍需完成 M1–M5 对应代码、评测与真实证据。
