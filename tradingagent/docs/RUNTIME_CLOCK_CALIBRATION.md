# Paper 自动校时与 market_unavailable 修复

更新：2026-10-08 23:43，Asia/Shanghai。

当前会话为`ogn测试1` / **ONGUSDT**，95/v1；原OGNUSDT历史会话保留。
8776工作台已重载，钱包保持暂停。本轮没有收费请求或交易所订单。

## 原因

两个公共WS均连通，但原事件领先本机约307ms，超过50ms校验边界。
免费NTP实测Windows落后约366ms；原生W32Time虽配置64秒同步并刚成功同步，
仍未维持需要的精度。原状态证据：
`output/verification/market-unavailable-before-20261008T151715271540.json`。

## 修复

- live Paper装配`CalibratedClock`，任务共用根时钟和一个worker；离线仍可注入FakeClock。
- 对公开`ntp.aliyun.com / ntp1.aliyun.com`各测3次，每30秒刷新。验证已连接服务器/
  端口、originate身份、模式、层级和时间顺序；拒绝未同步、超过2秒校正及样本冲突。
- 误差估计包含半网络延迟、半上游root delay、root dispersion、服务器精度、
  本机保守16ms时间戳误差及15ppm老化。估计须≤原**50ms**，证据最多180秒。
  无效校准同时阻断新报价和缓存报价；不据此启动模型或成交。
- 校准UTC锚定高精度单调时钟，Windows后续调整不会改变运行时时间轴。
  小幅负校正保持时间连续并计入误差；发布应用时间后不允许迟到的首次校正倒退。
  无法在误差预算内维持连续时阻断行情，必要时网络恢复后重启。
- 刷新失败只暂时保留仍有效证据，源冲突或超过误差/年龄限制则阻断，后续有效
  一致证据才能恢复。未修改Windows时间、服务、代理或用户费用配置。
- 原Binance E/T及首次接收时间保留，不夹紧、不伪造、不延长5秒行情有效期。
  `FuturesMarketSnapshot.clock_evidence`保存源、校准时间、误差、本机偏差、证据年龄，
  随完整`market_snapshot`进入模型输入与逐笔档案；旧无字段快照仍省略该字段。
- 页面区分校时未就绪、事件超界、断线及报价尚未完整；旧启动失败记录保留，
  当前恢复时明确提示“公共行情已恢复，可点击恢复运行”。

公式与误差参考[RFC 5905](https://datatracker.ietf.org/doc/html/rfc5905)；校时源见
[阿里云NTP服务说明](https://help.aliyun.com/en/ecs/user-guide/alibaba-cloud-ntp-server/)。
当前方案仅为live Paper接入，尚未验收真实资金执行所需的长期时间保证。

## 验证

37相关离线passed/11.97秒，覆盖偏差、Windows跳时、源冲突、迟到/过期校准、
50ms守卫、多任务共享时钟及逐笔档案兼容。新用例实际失败后修复；未全套。
日志：`output/verification/calibrated-clock-focused-20261008.txt`。
Ruff/10文件format、Node消息回归及工作台JS语法通过。

免费真实ONGUSDT探测7/8、39/40有效；首次连接出现ConnectionResetError并重连，
不是全程无网络故障。后续跨一次30秒自动刷新，仍通过原时间/时效守卫：
`calibrated-public-market-20261008T153349950339.json`、
`calibrated-public-market-20261008T153612849541.json`。

原服务再次只读确认：两连接true、30条报价缓存、quote_fresh=true、clock ready/
误差估计44.323ms、0帧拒绝；钱包paused/1000USDT/空仓、paid=false。
证据：`calibrated-clock-live-task-20261008T154314145279.json`。
浏览器已显示实时报价与200根已收盘K线，无console error；截图：
`output/verification/calibrated-clock-ui-20261008.jpg`。

四钱包、48条原费用、会话/策略/逐笔档案/README/长期配置hash与资金核对
preserved=true：`jev-workbench-before-20261008T153916221287.json` →
`jev-workbench-after-20261008T154019548174.json`。滚动小时请求数可自然到期，
不清零消费。原累计0.10USD/单次0.02USD/无到期授权保留；确认消费0.011754876、
未知预留0.003825780、剩余0.084419344USD未变。

较广层级检查仍报告两处既有越层引用：application/futures_trading.py引用config，
application/jev_tasks.py引用bootstrap/sqlite；本轮保留原装配位置，没有扩展无关重构，
该检查不记作通过。系统Temp权限失败改用项目内独立临时库后完成上述37项验证。

8776最终PID21756 / exec session87392，日志`calibrated-clock-server-20261008.txt`；
8775未重载。没有依赖安装、提交、推送或部署。

## 使用与边界

刷新工作台，选择**ogn测试1 / ONGUSDT**，确认实时报价及自动校时就绪后点击
**恢复运行**，才会开启原费用上限内的真实JEV Paper预测；无需再次管理员手动校时。
免费复测：用正式Conda Python执行`tools/check-calibrated-futures-market.py --symbol ONGUSDT --samples 8`。

本轮恢复行情和操作入口；真实自主成交/退出、持续多会话及24小时稳定性仍须实际验证，
不能用免费行情探测替代。
