# JEV Paper 启动与本机配置

开发根目录：`D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent`。使用已有 tradingagent Conda 环境，不需要安装或升级依赖。2026-10-07用户确认首次真实 JEV 累计试跑上限 **1 USD**、单次上限 **0.02 USD**。收费启动仍要求有效时间/价格、本机Key和页面明确确认。

## 先运行本地模拟

在 PowerShell 执行：

```powershell
Set-Location 'D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent'
& 'C:/Users/exile/anaconda3/envs/tradingagent/python.exe' -m agent_platform.cli web --paper --paper-mock --trader-environment paper --database data/paper-demo.sqlite3 --port 8774
```

打开 `http://127.0.0.1:8774/`，用 0–100 整数滑杆明确确认新会话风格。进入 `/jev-trader`，保存“开启 JEV 操盘模块 / 自动操盘 / 本地 Paper”。然后填写虚拟资金、每次 BTC 数量、持仓和亏损上限、手续费、滑点、置信度及本轮策略，确认后建立虚拟账户，再确认启动循环。

默认每 60 秒独立判断，BUY / SELL / WAIT 是离线 Mock 脚本，报价是明确标注的演示价格；它们只验证交易流程，不衡量策略收益。页面显示余额、权益、手续费、判断耗时与最近 50 条记录。暂停需要明确确认；余额在后台更新也能暂停同一次运行。启动后发生风格、会话或操盘配置变化时，旧判断不能成交。

同一会话只能建立一次虚拟账户。服务重启保留资金和历史，默认暂停；重新运行需在页面确认。关闭 PowerShell 服务可用 Ctrl+C。不要将演示数据库用作真实模型试跑库。

## 真实 JEV 配置

首次累计预算已确认 **1 USD**，无需再次回答。请在本机配置 OpenRouter Key。Key 只从进程环境变量 `OPENROUTER_API_KEY` 读取，不放进 JSON、聊天、日志或截图。程序不自动读取 `.env`；已经设置的环境变量须让启动进程继承。真实 Paper 不需要 Binance 账户 Key，只读取公共行情和自身虚拟持仓。

已进入聊天的Key应在OpenRouter撤销并替换。新Key可以在启动服务的同一个PowerShell中用隐藏输入设置，以下命令不会把Key写进命令历史或文件：

```powershell
$jevLocalKey = Read-Host '输入新 OpenRouter Key（隐藏）' -AsSecureString
$env:OPENROUTER_API_KEY = [System.Net.NetworkCredential]::new('', $jevLocalKey).Password
Remove-Variable jevLocalKey
```

然后在该终端执行下文启动命令。另一终端或已经运行的Codex进程不会自动继承这个临时变量；如果由Codex继续联调，需要让它的启动进程也继承新Key。只报告“已配置”，不再粘贴Key内容。

`configs/jev-paper.example.json` 的1 USD现在来自用户真实授权，但空时间仍使它不可执行。`data/jev-paper.local.json`（data 已被 Git 忽略）保存本次无密钥配置；启动前核对有效期，过期后不能偷偷续期或重复授予1 USD。测试fixture的1 USD与这次授权事实分别记录。

| 字段 | 要求 |
| --- | --- |
| trial_total_usd | `"1"`，2026-10-07明确批准的首次累计 USD 上限 |
| single_call_usd | `"0.02"`，并且不能超过累计上限 |
| issued_at / expires_at | 带时区的 ISO 时间；开始早于截止，截止不超过本次开始日的上海午夜 |
| price.version | 本次核对价格的独立版本标识 |
| price.input_usd_per_million / output_usd_per_million | 核对后的 USD / 百万 token 价格，字符串金额 |
| price.verified_at / valid_until | 带时区的价格有效窗口，完整覆盖本次试跑窗口 |

例如上海时间可写 `2026-10-07T16:00:00+08:00`，程序转成 UTC 保存。2026-10-07重新核对 [JEV 1.13 官方模型页](https://openrouter.ai/typesafe/jev-1.13) 的输入价 0.042 USD/百万 token、输出价 0；运行前仍需核对，不能无限延长价格有效期。使用 [OpenRouter Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)，固定模型 `typesafe/jev-1.13`。

本次已创建的local配置窗口截至2026-10-07 16:41:18（上海时间）。2026-10-07已确认系统环境变量有新Key、prepare校验通过；当前Codex旧进程没有继承新增变量，联调子进程显式读取Machine环境并注入，未输出或保存Key。用户自行启动时可新开PowerShell让它继承变量。

配置完成后的启动命令：

```powershell
& 'C:/Users/exile/anaconda3/envs/tradingagent/python.exe' -m agent_platform.cli web --paper --paper-model-config data/jev-paper.local.json --live-public --trader-environment paper --database data/jev-paper-20261007.sqlite3 --port 8774
```

启动只装配模型，仍需在页面确认会话、Paper 策略和启动操作才调用。2026-10-07在127.0.0.1:8774启动的16:14:49起20分钟实例已退出；当前已接续持续CLI服务，仍用data/jev-paper-20261007.sqlite3，模型重启默认暂停。当前直接访问页面，不另起冲突端口。首次试跑政策已固化，支出/预留/调用次数仍0。用户已在页面保存风格80/v1，无需重复确认风格；还需Paper资金、策略和启动确认。后续同一次授权必须使用同一数据库，不换库或重置已有费用；仅当前价格/时间窗口有效时可用上面的手动命令重启，过期不自动续期，已运行公共行情页面继续更新。

总览优先绘制真实盘口中间价（买卖价均值，非成交价），使用独立book_at/状态与5秒时效；成交价与盘口分开积累，缺口处断开。最新成交为横线不代表没有盘口报价。账户/挂单需要另配Binance只读接入，Flash与JEV建议尚未装配收费运行；Paper使用独立虚拟资金，不需要账户Key。市场gap/指标warming时即使盘口可以绘图，真实Paper决策仍被守卫阻止。

若公共行情未就绪或报价超过 5 秒，不派发模型请求。真实 JEV 直接读取虚拟持仓、已闭合的最近 12 根 K 线、可用且同快照的指标以及本轮策略，不等待 Flash；当前 Flash、JEV 建议和强模型收费调用没有启用。

## 费用与异常

首次有效配置固化进当天 SQLite 预算状态；改配置、重启、新会话或其他模块请求都不能提高首次总额。配置完全相同可重开；改价格、窗口或限额时本日拒绝替换。模型请求最多每小时 60 次；派发前同时检查单次 0.02 USD 与共享累计预算，未知收费继续占用预留，未知费用或实际超估冻结按已有预算纪律处理。程序到截止时间不自动续期，下一次试跑需新的明确授权和配置。

系统页显示共享预算和实际收费状态；Paper 每条记录显示自己的费用证据。预算不足、超时或不可评估不会冒充有效 WAIT。页面来源必须与持久钱包来源一致，Mock 钱包不能重标成真实 JEV 历史；切换来源需关闭旧会话并确认新会话。

这里的“自动”只执行本地虚拟成交。Agent OS 和 Binance Testnet 订单接入仍是 Paper 验证后的下一阶段，真实资金执行不在本次范围内。

## 本次行情检查与校时

2026-10-07实际公共REST时间请求与WS握手成功；录制trade时间比本机接收时间快约700ms。正常流因此触发未来数据守卫，尚无可用于Paper的有效快照。Windows时间源为Local CMOS Clock/未同步；重新同步被系统拒绝0x80070005，尚未修改系统时间。先在Windows“日期和时间”设置中同步，或在管理员PowerShell执行：

```powershell
w32tm /resync
```

用户已报告校时完成，状态实测同步源已变为time.windows.com且成功时间15:54:24。但25秒公开复测仍失败，原始trade时间领先本机0.713897s，独立NTP仍测得约0.772s偏差。微软文档说明小偏差可以[通过时钟速率逐步校正](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings#configure-a-computer-clock-reset)；同步成功标志并不能保证此时已收敛。

已提供小幅即时校正工具，先默认只读测量，只有显式-Apply才调整。在**管理员PowerShell**执行：

```powershell
& 'D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/tools/sync-trading-clock.ps1' -Apply
```

工具使用[Cloudflare公共NTP](https://www.cloudflare.com/time/)作本次测量，保留Windows现有时间源、注册表和服务设置。要求5个有效样本、极差≤0.1s、偏差≤2s；校正后复测残差≤0.05s才报告成功。输出JSON中applied/after_offset_seconds可核查；无管理员权限、网络超时或不稳定样本均拒绝，不能把错误输出当成功。不要使用旧0.772s手工固定加时，也不要放宽未来数据守卫。首轮准备时仅验证只读路径，随后用户已完成系统校正，见下文结果。

用户随后成功Apply，复测残差0.0088855s；独立25秒公共行情探测已captured/ready，24849条事件/120根K线，Web行情和指标ready。当前不需再次校时。首次真实会话风格仍需明确0–100确认，页面Paper参数/策略/启动也须明确确认；目前没有收费请求，不能把此前Mock35及模拟策略直接作为用户选择。详情和证据见DEVELOPMENT_STATUS.md及IMPLEMENTATION_LEDGER.md。

## 是否要连接 Binance MCP

Paper需要公共报价、K线和自身虚拟资金，不需要Binance账户授权。现有Direct REST/WebSocket是项目主行情链路；MCP可以作为后续可替换接入，并非当前Paper的必需前提。[Binance MCP官方文档](https://developers.binance.com/en/docs/agent-native/mcp-server/agentic)区分公共Market data、Account、Trade、Transfer范围。

Codex桌面中的MCP连接属于本客户端；独立Python服务仍须自己的适配器/授权，不复制桌面Token。如果先连接MCP用于查看数据，先使用公共行情或账户只读；本阶段不授权真实Trade/Transfer，也不充值Agentic子账户。Testnet是否受支持、确切工具Schema和请求映射尚须实测，不把官方主账户/子账户能力自动视为测试网能力。
