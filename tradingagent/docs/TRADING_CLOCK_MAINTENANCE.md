# 持续校时与秒级行情（2026-10-08）

最新：原生64秒同步仍出现超过50ms的误差，live Paper现在使用应用内自动NTP校准和单调UTC。无需重复管理员手动校时；新时钟与行情守卫、验证结果见[RUNTIME_CLOCK_CALIBRATION.md](RUNTIME_CLOCK_CALIBRATION.md)。下文保留Windows配置工具的历史说明，不将安装成功视为持续精度保证。

2026-10-08 20:00上海接续：持久同步正常仍可能出现毫秒误差，实测15.6ms领先触发旧零容差，两次免费预检0费。公共合约报价现允许50ms领先，保留交易所原E/T及本机首次接收时间，超界/5秒过期继续拒绝，不伪造时间，离线不放宽。50ms是项目策略，不是外部精度保证；无需反复Set-Date。策略边界RED后相关63项通过，真实最终8次JEV请求成功，长时间精度仍需观察。[完整证据](JEV_PAPER_ARCHIVE.md)。本轮未改系统校时服务或配置。

实测W32Time Running/Automatic，但Leap Indicator3未同步、Stratum0、Source Local CMOS Clock，最近成功同步时间未指定。原配置NTP/time.windows.com,0x9，MinPoll10、MaxPoll15、SpecialPoll32768秒（约9.1小时）。Cloudflare五样本偏差中位+0.4145889秒/spread0.0022463秒；Windows时间源三样本也约+0.43秒，Google超时。机器WORKGROUP，未入域。

两个Binance公共WS已经既有代理连通，BTCUSDT/st=1原始帧可读；事件首次接收领先本机330–374ms，被原守卫拒绝。旧sync-trading-clock.ps1只Set-Date纠正一次，没有修复持续同步。

`tools/maintain-trading-clock.ps1`默认只打印计划；管理员`-Install`实施，`-Restore`按原备份还原本工具修改的配置。主时间源Cloudflare客户端0x8，备用time.windows.com客户端+fallback0xA；Min/MaxPoll均6，即64秒原生同步。服务自动启动，NtpClient启用，不创建反复Set-Date任务、不改防火墙或系统代理。

第一次安装先验证NTP稳定/偏差不超过2秒，保存`output/verification/w32time-original.local.json`（重复安装不覆盖），配置服务、做一次初始校时并请求native resync。核对网络时间源和偏差，保存`w32time-maintenance.local.json`；命令失败或仍CMOS时明确失败，不把配置写入当作成功同步。入域或存在管理策略时拒绝覆盖。

管理员PowerShell执行一次：

```powershell
& 'D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/tools/maintain-trading-clock.ps1' -Install
```

Windows服务使用UDP源端口123，stripchart用临时源端口，后者成功不能证明前者网络路径成功。安装如报告w32time_command_failed，需进一步检查服务路径，工具不自动放开防火墙。[Microsoft说明](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings)。64秒配置参考[Windows高精度配置](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/configuring-systems-for-high-accuracy)。

PowerShell计划/管理员先行守卫/非法备份3项离线通过（1.31秒），未修改系统。配置证据`jev-time-service-before-20261008.json`、NTP`jev-fast-clock-20261008.txt`、WS网络/帧诊断`jev-stream-transport-diagnostic-20261008.json`/`jev-stream-frame-diagnostic-20261008.json`。

**2026-10-08 18:39:58上海，用户已完成管理员安装。** 报告configured/source_verified=true、failure=null，实际源time.cloudflare.com、Automatic/Running、Min/MaxPoll=6；安装后偏差0.0085849秒（8.5849ms），原备份保留。18:40:59起免费公共WS复测8/8有效，0模型调用/0交易所订单。当前同步和行情校验恢复，长期时间保持尚待观察。

1秒调度/3并发已在8776加载；复查原钱包1000USDT空仓paused、model_enabled=false，费用/配置哈希不变。真实JEV每秒派发性能与连续Paper尚未运行，详见jev-fast-after-20261008.json和jev-fast-public-stream-20261008.json。
