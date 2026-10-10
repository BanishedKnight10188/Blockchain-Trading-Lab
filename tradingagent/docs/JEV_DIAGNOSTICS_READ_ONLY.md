# JEV 安全诊断与只读加载

2026-10-08，沿用户自主推进与减少测试授权原地开发；不续期旧授权、不收费、不改资金纪律。

已知问题：两笔收费返回被拒绝，但统一错误码无法定位解析阶段；到期策略阻止正常重启，页面与已有持仓维护受影响。

设计：错误保留有限的解析阶段、问题索引和数量，不保存任意响应正文、字段内容或异常文字。已知费用正常结算，未知费用保留；无效模型身份也停止后续请求。诊断经过预算层保留到合约 cycle、SQLite 与页面。

显式 `paper_read_only` / `--paper-read-only`（工具入口 `--read-only`）仅供真实 Paper。允许读取过期但静态有效且与共享账本完全一致的策略；不能创建新 trial、续期或改变封顶。模型从启用与派发两个入口拒绝调用；启动操盘返回固定原因，暂停、查看与免费持仓维护继续。普通启动仍要求授权有效。

实施顺序：

- [x] 最小失败回归：解析阶段、费用传播、持仓失败暂停、只读策略与派发阻断。
- [x] 实现严格诊断类型、解析与持久化、页面展示。
- [x] 实现配置、CLI、工具和只读加载；核对旧预算不变。
- [x] 定向验证后安全重载本任务 8776；保留 8775、旧钱包和未知预留。

验收：先12失败/2通过，再3失败；初次绿色54通过，1项错误预期（choice在数值阶段拒绝）与1项fixture清理错误（Mock没有set_enabled），修正测试后最终 **94 passed /21.94秒**。Ruff、format、JS语法通过。仅相关定向验证，既有Starlette提示保留，无新依赖。

8776已用--read-only重载（PID39312/session69886），实际页面/API200、受保护start409/model_read_only；原8775未重启。钱包1000虚拟USDT/空仓/2倍/paused；15笔模型账本，spent0.002459016/held0.002161068保持。全部trial、旧授权文件、预算、两会话、交易policy和用户README哈希一致。免费mark/funding维护可以更新账户行情水位；不要求revision冻结。浏览器tab3已刷新，显示参数控件、只读预算、禁用启动和可确认暂停。

证据：output/verification/jev-diagnostics-readonly-{red,green,final}-20261008.txt、jev-diagnostics-cycle-red-20261008.txt、jev-readonly-reload-{before,after}-20261008.json、jev-readonly-server-20261008.txt。旧无效cycle未猜测补诊断。

限制：旧会话仍fixed_notional（2倍/500持仓上限），v2真实联调未运行。实际仍有maintenance_unavailable和8775 futures_unavailable；行情连续性未解决，不能称长稳成功。15:53:27到期授权未续期。本轮0模型派发/交易所订单。

本项不代表 v2 真实参数成交、独立调杠杆、保护单或 Testnet 已完成。
