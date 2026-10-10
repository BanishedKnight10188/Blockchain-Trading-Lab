# USDT 合约 Paper 内核验证

2026-10-07，正式 tradingagent Conda Python3.12。范围是独立离线领域内核与 SQLite 存储；未装配合约实时后台、JEV 决策或 Web 钱包操作。规格见 [FUTURES_PAPER_SPEC.md](FUTURES_PAPER_SPEC.md)，执行计划见 [FUTURES_PAPER_KERNEL_IMPLEMENTATION.md](FUTURES_PAPER_KERNEL_IMPLEMENTATION.md)。

## 已实现与验证范围

- USDT 单合约、单向逐仓、LONG/SHORT、同向加仓和 reduce-only；杠杆1–20、显式虚拟资金及风险纪律、手续费与滑点。Spot 钱包、真实账户和旧会话没有转换为保证金。
- 80位独立 Decimal 上下文，现金1e−12、入场成本1e−24；确定性资金守恒、部分平仓成本、跳空清算和短缺记录。维持率来源明确为 `simulation_fixed`，没有冒称 Binance 全币种风险档位。
- 已结算资金费去重与时序；暂停不改变持仓时刻，同刻资金费必须先于订单。逐仓耗尽即清算，浮盈所得先抵该持仓的资金费欠款，原可用现金不补仓。
- 当前会话/市场/币种/style/trader/auto/paper 事务内重验；CAS、命令ID幂等、不同内容冲突、事务失败完整回滚、重启暂停及历史保留。原 SQLite 核心状态类型与 Spot 表未改写。
- 报价/订单/资金费原始输入与审计原子保存；安全 mark 水位单独持久化，普通观察不改变资金版本。拒绝来源改变、旧报价及同刻冲突；mark-only 保留同刻或更旧盘口，恢复不回退到旧水位。

## RED → GREEN 与独立复核

证据均位于项目 `output/verification`，失败日志保留，不用后续成功覆盖。

| 范围 | 实际证据 |
| --- | --- |
| 领域缺模块 → 31项通过 | `futures-kernel-red-20261007.txt` / `futures-kernel-green-20261007.txt` |
| 成交后 mark 风险、空仓资金费、宿主 Decimal traps | `futures-kernel-hardening-red-20261007.txt`；补齐后34项通过 |
| 暂停后迟到结算，不把暂停当持仓变动 | `futures-funding-pause-red-20261007.txt`；后续 store GREEN 包含该回归 |
| SQLite 缺模块 → domain/store 47项通过 | `futures-store-red-20261007.txt` / `futures-store-green-20261007.txt` |
| 输入证据、暂停后的独立 mark 清算 | `futures-store-evidence-red-20261007.txt`；连 Spot/架构回归77项、113 subtests通过 |
| 独立复核4项P2及补充边界 | `futures-kernel-final-review-red-20261007.txt`，实际8 failed/50 passed；修复后58项通过 |
| 独立水位在重启恢复中保留 | `futures-kernel-final-targeted-20261007.txt`，59项通过；恢复读取合并后的最新报价 |
| mark-only 覆盖同刻已采用盘口 | `futures-book-watermark-red-20261007.txt`，1 failed/17 passed；`futures-book-watermark-green-20261007.txt`，60 passed（3.07秒） |

独立复核最初4项P2为：同刻订单后补资金费按错误持仓计费、保证金耗尽但浮盈为正不清算、5秒窗口内旧报价仍可成交、尾零/乘积指数使合法数值被拒绝。随后追加同刻盘口覆盖P2。所有问题实际复现并修复，最终只读复核确认无剩余P1/P2；探针也确认原盘口、更新盘口仍可执行，不能只靠拒绝所有输入通过测试。

中间 `futures-kernel-review-green-initial-20261007.txt` 仍有5失败：旧数值 fixture 用同一 NOW 表示不同市场价格，与新增同刻冲突守卫矛盾。改用递增行情时刻并保留原金额断言，最终58项通过；这些 fixture 修正没有用于改写真实 RED。首次全套 `futures-kernel-full-suite-20261007.txt` 因评审发现而主动中断，不计验收。随后 `futures-kernel-reviewed-suite-20261007.txt` 为1288 passed / 148 subtests（261.89秒），属于最终同刻盘口修复之前的中间证据。

最后修复后的完整 `futures-kernel-final-suite-20261007.txt` 实际 **1289 passed / 148 subtests passed，251.15秒**，进程exit0；只有既有 Starlette 测试客户端弃用提示。Ruff check 与 format 检查范围固定为 `agent_platform tests tools`，295个Python文件，均通过；生成的 output 产物不纳入源码检查。新增盘口回归第一次有两条过长行，已格式化；未改产物或安装环境。

## SQLite 离线流程证据

`futures-kernel-proof-20261007.py` 创建唯一、全新的验证数据库；不打开用户 `data/jev-paper-20261007.sqlite3`，使用明确标记 `offline_replay` 的 ETHUSDT 报价和 Fake 控制。

1000虚拟USDT、5倍、5bps手续费：2000开多1ETH → 0.001结算资金费扣2USDT → 2100减仓0.4ETH → 重开数据库恢复暂停。最终可用797.78USDT、逐仓238.8USDT、持仓0.6ETH、已实现40USDT、费用1.42USDT、资金费−2USDT；mark2100时权益1096.58USDT。审计共6项，恢复保持原持仓/资金并暂停。

最新 proof 数据库 `futures-paper-proof-f093db779cef4e8ba06ed92a2dda1823.sqlite3`，完整报告 `futures-kernel-proof-report-20261007.json`，运行摘要 `futures-kernel-proof-final-20261007.txt`。这是虚拟资金/录制报价验证，没有真实JEV响应、收费或交易所订单。

## 当前服务与未完成项

持续只读Web仍在8774、原用户DB；状态证据 `market-types-persistent-status-20261007.json` 保留原Spot/BTCUSDT PAUSED、风格80/v1、钱包未配置。新内核没有装配到该服务，合约页面继续显示待接入。20分钟隔离UI预览已正常结束，不作为用户操盘证明。

下一顺序：合约实时 mark/book、已结算资金费及目录 filters → 独立JEV候选/风险重验/费用后台 → 合约Paper配置、启动、持仓和成交Web → 有界公共行情/模型试跑 → Binance Testnet/Agent OS。全仓/双向、ADL、真实档位、深度/部分成交、特殊资金费和24h持续验证仍未完成。

模型费用政策于2026-10-07 16:41:18上海到期，本轮不续期。无依赖安装、收费调用、私有账户请求、真实订单、提交/推送、部署或新wheel；旧Oct6 wheel不能代表新增合约功能。README SHA256保持 `E670ED4212C6241F3FDF9B68F6868046FCC92261063F11B09D73A87898796EDD`。
