# 通用合约操盘核心与启动说明

当前快照15:31:41上海：8776服务在线、钱包自动暂停rev9。7轮有效WAIT/费用闭环；2次已收费响应未通过决策校验，另有一次明确网络传输失败触发停止保护。总确认模型费用0.002459016USD、未知预留0.002161068USD，资金1000USDT/空仓。最新证据与局限见 [JEV_FUTURES_REAL_TRIAL.md](JEV_FUTURES_REAL_TRIAL.md)；不要把下段“15:27恢复”当当前运行状态。9类失败停止专项补充通过，无强制模型交易或真实订单。

2026-10-08 15时接续：真实JEV已接通用Paper周期，入口 <http://127.0.0.1:8776/jev-trader>，5轮真实WAIT及费用落账、暂停221.30秒无收费、恢复已验证。独立钱包仍使用原试验DB，新增 `--model-budget-database data/futures-core-preview-20261008.sqlite3` 将费用与不可变trial写回原账本。具名 `grant_id` 为追加授权，旧policy/消费/unknown预留不重置；此次总0.10USD/单次0.02USD、15:53:27上海到期。启动工具 `tools/start-jev-paper-live.py` 只读取已有本机Key，重启保留资金并暂停，不续期。当前状态见 [JEV_FUTURES_REAL_TRIAL.md](JEV_FUTURES_REAL_TRIAL.md)。

公共行情免费失败不收费，启动返回409安全原因码。模型接口失败/未确认费用先落账再停模型、暂停账户，已有仓位继续维护；页面新增预算明细。实际有盘口/mark时间校验拒绝和约0.129秒只读NTP偏差，长稳未验证；未放宽行情守卫、伪造走势或模型成交。原8775仍Mock，保留用户总览与92/v1。以下为早期核心交付时的启动证据；旧PID、无真实模型等描述仅属于其历史时间点。

2026-10-08最新：真实JEV现已通过显式VPN代理取得HTTP200/WAIT和已结算usage，详见 [JEV_FUTURES_REAL_TRIAL.md](JEV_FUTURES_REAL_TRIAL.md)。正式真实模型启动命令增加 `--openrouter-proxy http://127.0.0.1:7897`，与公共合约的 `--futures-proxy` 独立；默认不继承系统代理。当前8775仍Mock预览，原钱包暂停，本次单次诊断不代表持续自动操盘启用。403明确拒绝时暂停新决策，已有仓位继续维护。以下为核心交付时的历史验证及启动范围。

2026-10-08。按用户“减少测试、先完成核心功能”交付持续后台、独立 JEV 闭环和合约 Web。使用现有 tradingagent Conda，无依赖安装。本轮不做全套回归、独立复核或收费调用。

## 实现范围

- `application/futures_trading.py` 使用当前合约、历史、账户、0–100原值/版本、策略与硬限额生成有界请求；Flash、可选JEV建议不在操盘关键路径。`OPEN_LONG / OPEN_SHORT / REDUCE / WAIT` 经确定性数量与通用执行通道；REDUCE 当前完整平仓，不直接反向。
- `application/trading_maintenance.py` 与 `runtime/futures_trading.py` 分开调度。启动立即首轮，后续决策至少60秒；维护目标2秒，实际受请求耗时影响。OS锁拒绝第二进程，未获锁实例清理不会暂停所有者的钱包。
- 生命周期/维护/执行/账户Ports与市场输入分别装配；Paper Adapter复用旧逐仓内核，主体不导入Adapter。当前只装配Paper，Testnet后端尚未实现。
- SQLite保存不可重置的资金/策略、结算游标/预告时刻、决策/费用/命令身份。成交写回失败标记unknown，只查询恢复；页面同时展示实际执行回执。重启保留资金、持仓和历史并暂停。
- 受浏览器会话、同源与确认保护的合约配置/启动/暂停页展示保证金、权益、持仓、资金费、手续费和模型费用。无钱包时也能观察当前合约报价；旧Spot独立保留。

执行前重验行情漂移、资金/持仓/亏损限额及session/style/trader/account版本，命令不越过尚未处理的资金费时刻。结算只使用已发布Regular事件；到期未发布保留游标并阻断资金变化，不按预估费率或固定8小时猜算。暂停/关闭操盘仍维护已有仓位。

Paper固定模拟维持率0.5%、清算费50bps，不是Binance真实风险档位。维护依赖可用行情窗口；结算待发布时包括清算的资金变化也暂停。单mark维护、盘口中断和其他长期故障处理仍需加固，不称全天候或交易所级完整模拟。

历史覆盖所选1/7/30天已收盘1h K线；JEV上下文提供完整区间摘要/哈希和最近最多48根，控制请求体积，不谎称逐根发送720根。首次Flash接口已存在，真实Flash和常态分析仍待装配。

## 隔离预览

访问 <http://localhost:8775/> 创建合约会话、选择币种与风格并启动；在 <http://localhost:8775/jev-trader> 保存“开启 + 自动 + Paper”，确认资金、策略、纪律后启动。

预览为 **Binance公共合约行情 + 离线Mock默认WAIT**，数据库 `data/futures-core-preview-20261008.sqlite3`。没有调用真实JEV，也没有创建用户会话/钱包或提交交易所订单。原8774服务、原用户数据库及80/v1暂停Spot会话保留。

当前预览PID **6644**、工具session **77898**；如仍运行，不要重复启动同一数据库：

```powershell
Set-Location 'D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent'
& 'C:/Users/exile/anaconda3/envs/tradingagent/python.exe' -X utf8 -m agent_platform.cli web --live-public --futures-proxy http://127.0.0.1:7897 --no-market-archive --paper --paper-mock --database data/futures-core-preview-20261008.sqlite3 --port 8775
```

完全离线时去掉公共行情和代理参数；报价/目录明确为Fake，目录只包含示例合约。不同来源不能接续同一钱包。真实JEV用有效的 `--paper-model-config` 替代Mock，复用本机Key、持久预算预留和费用结算。

**原收费政策已到期，当前不自动续期或收费。** 真实试跑需重新确认有效期与剩余累计预算，保留原累计1USD/单次0.02USD及费用事实；Key只在本机配置，不发聊天。

## 精简验证与待办

- 首次6项5pass/1fail为测试把等值1E+3/1000作文本比较，改为Decimal；未冒称实现前RED。中间77/123subtests；最终 **80 passed / 123subtests，19.41秒，exit0**，仅既有Starlette提示。新增核心集成12项，其余为相关执行/资金/进程/架构/装配/Spot兼容。
- 最终 `output/verification/futures-core-final-20261008.txt`。覆盖手算资金、资金费迟到/去重、暂停清算、慢模型独立维护、最终报价漂移、权限分离、双进程、过期停止、未知写回恢复，以及保护Web/首次唤醒/重启。没有重跑全套1450或派独立review。
- 实际预览页面/模块200，公共目录首次503传输失败，重试经原7897代理成功取得 **525** 个可交易USDT永续，含ETH。受限端口探测曾误判代理停止，外部监听与官方GET纠正；没有修改系统代理。保留首次日志；最终 `output/verification/futures-core-preview-final-20261008.json`，模型/钱包/交易所订单0。
- 后续：有界真实JEV Paper；未知未提交通道人工恢复、单mark维护/故障显示和24h长稳；随后Testnet/Agent OS。真实Flash/可选JEV建议后台、USDT合约只读账户也未完成。强模型只选择，真实资金未启用。
