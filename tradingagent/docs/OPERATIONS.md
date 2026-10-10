# 本地运行、保留、备份与恢复

正式解释器为 `C:/Users/exile/anaconda3/envs/tradingagent/python.exe`，用户维护依赖。本项目不安装后台服务。运行根目录为 `D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent`。

## 启动与停止

```powershell
conda activate tradingagent
Set-Location D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent
python -m agent_platform.cli web
```

默认只监听127.0.0.1，无外部请求、行情sidecar或收费模型。浏览器关闭不会停止后台。真实数据通过 `web --live-public --live-account` 独立启用；账户凭据只存在本机运行环境，不粘贴进会话/报告。程序只有签名GET白名单，用户负责在Binance执行交易。

私流可加 `--live-user-stream`，必须同时有 `--live-account`。它仅提示REST对账，15秒最小间隔和RetryAfter仍由权威同步控制。权限拒绝停止私流，REST继续；不能因私流事件直接修改余额。

前台停止使用Ctrl+C；Windows控制台也可Ctrl+Break。等到 `Application shutdown complete` 和进程退出后再恢复/移动数据。2026-10-05正式环境前台PID42936实际Ctrl+Break验证完成，启动/HTTP运行状态/应用关闭/PID消失已核验；控制台中断的shell exit为1，不能据此宣称普通成功exit0。exec PTY写入Ctrl+C字符本次未实际传递信号，不当作停止证据。

进程重启先以空的实时缓存启动。SQLite会话/风格版本、成交/游标、UNKNOWN预算和回访仍在；账户须完成新一次REST尝试才能恢复发布证据。同对象重启也不会将上一轮cached sync当成新鲜证据，等待只使用原门槛的剩余时间。

## 存储与容量

| 文件 | 内容 | 保留/故障行为 |
| --- | --- | --- |
| `data/agent.sqlite3` | schema7核心会话、账户/成交、建议、费用、反馈、固定复盘和任务 | 永久保留；审计失败停建议/账户写入 |
| `data/agent.market.sqlite3` | 无账户/用户文本的辅助公共行情 | 1秒采样7天、已收盘分钟90天；显式pin永久保留 |
| `data/agent.operations.jsonl` | 固定状态代码、UTC和有限worker健康 | 1MiB当前文件+3份轮转；故障停止日志worker并显示降级 |

显式公共行情模式默认归档；`--no-market-archive`可关闭。默认disabled不会创建行情库。辅助库最多每秒写入最新一帧和最近120个分钟，每分钟清理一批最多1000条；启动会逐批追赶历史清理。归档写入故障只停止该worker，不阻断公共缓存或放宽纪律。系统页显示整体健康、归档/日志降级及本进程记录计数；计数不是全部历史数量。

完整7天采样上限约604800条，90天分钟约129600条。实际磁盘还包括正文/索引、WAL、永久pin、核心审计和备份，不能把条数当作固定字节容量。清理删除逻辑记录后SQLite文件未必立即缩小；不在运行中自动VACUUM。应预留主库与sidecar各自备份的空间，长期容量须真实运行测量。

运行一个同路径Web实例，不共享诊断日志给多个进程；SQLite能协调事务，日志轮转不是跨进程协议。运行中的SQLite及WAL不直接复制/覆盖。辅助schema只接受自身canonical表；拒绝核心库和混合库。

两个保留时长可在启动时分别配置1–365天整数，默认仍为7/90天；配置不创建默认离线行情库，也不缩短核心审计或pin记录的永久保留。例如：

```powershell
python -m agent_platform.cli web --live-public --raw-retention-days 3 --minute-retention-days 120
```

`soak`接受相同参数，系统页/运行报告显示实际所选天数；重启时需沿用自己的启动参数，不从旧数据库猜测配置。更短时长会在下一轮有界清理中删除符合条件的未pin辅助记录，应先按需要备份。上述604800/129600条估算只对应默认7/90天。

## 在线备份

先创建自己的备份目录。输出必须是新文件，已有文件不会覆盖。

```powershell
New-Item -ItemType Directory -Force data/backups
python -m agent_platform.cli backup --database data/agent.sqlite3 --output data/backups/core-20261005.sqlite3 --kind core
python -m agent_platform.cli backup --database data/agent.market.sqlite3 --output data/backups/market-20261005.sqlite3 --kind market
```

未启用归档时省略第二条。命令不读取凭据或启动联网任务；以只读连接固定一个SQLite快照，再用在线backup复制活动WAL中的已提交内容。源/目标种类与schema、完整性和外键分别验证，报告SHA256/字节数/有限记录数量。两份数据库是两个独立快照，不能宣称完全相同截止时刻。复制有60秒界限；失败输出保留为需检查的未验收文件，不作有效备份使用。

核心备份含用户本地账户/交易事实，应放在自己管理的目录，不上传聊天或公共仓库。辅助备份拒绝混入核心表。哈希检测后续变化，不替代事实链校验。

## 恢复

1. 停止本项目进程并确认退出，保留当前核心库、sidecar及关联WAL/SHM，避免丢掉需要调查的失败现场。
2. 选择CLI成功验证过的备份；先以新的独立路径启动，例如 `web --database data/backups/core-20261005.sqlite3`，默认不联网。不可将一份核心备份改名当行情库。
3. 检查会话原strength/version、成交来源、冻结复盘、待执行任务与UNKNOWN费用。UNKNOWN预留不能清零或当作未收费。
4. 在新路径确认恢复后，再由用户决定正式文件切换。实时凭据仍在本机配置，重新启用只读读取；旧账户与旧建议不自动成为当前有效证据。

执行恢复时不能同时启动正式路径和验证路径读取同一账户而声称只有一个主实例。过期新回访拒绝；原有持久任务按已冻结schedule恢复，已完成同键任务不重复生成版本。

## 持续运行报告

短测默认离线，实际经过时间和退出会写入一个新报告：

```powershell
python -m agent_platform.cli soak --seconds 60 --database output/offline-run.sqlite3 --output output/offline-run.json
```

真实只读24小时需要已验证网络、本机只读凭据、设备不休眠和持续开机；入口为：

```powershell
python -m agent_platform.cli soak --seconds 86400 --live-public --live-account --database data/agent.sqlite3 --output output/readonly-24h-new.json
```

命令不同时提供Web服务；须先停止同核心路径的Web实例。可选私流另外明确加开关，不代表已经验证其权限。报告只累积有界统计，约10秒采样一次；记录实际monotonic时间、UTC、内存首末/采样峰值、采样最大缺口、ready/degraded次数、公共重连计数和持久费用。关闭/取消产生partial信息，报告不会覆盖。取消发生在启动之前也如实标not_started/不可读费用。

Windows内存使用[微软GetProcessMemoryInfo](https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-getprocessmemoryinfo)的WorkingSetSize；不是Python对象大小或完整泄漏证明。未能读取时保留unavailable。睡眠/阻塞造成的采样缺口单独显示，不能用实际计时的86400秒冒充86400秒连续数据。

报告 `formal_acceptance`始终false。offline_short_check、duration_not_met、live_evidence_missing、runtime_faults_present分别说明缺口；duration_complete_requires_review还须人工/独立复核连续覆盖、内存趋势、日志轮转、未知费用、重启恢复和隐私。2026-10-05实际3.031秒disabled报告在output/verification/t15-offline-short-20261005.json，约64.65–64.67MB采样工作集、0收费请求、后台stopped；未进行真实24小时。

没有配置后台服务、任务计划或更改休眠设置；长期宿主机由用户决定。当前网络WS联调未通过，缺账户凭据不反复空跑同一真实诊断。
