# T14完整本地工作台实施计划

用户已授权持续自主推进，沿用executing-plans/TDD/verification与frontend-design，不另等重复审批；不安装依赖、提交、联网或部署。

## 设计与行为

沿用STYLE_CONTROL.md的明亮工作台：背景#EDF1F5、面板#FFFFFF、正文#17263C、操作#1C5DC7、次级#53647A、分隔#CBD5E1；中文Microsoft YaHei UI、数字Bahnschrift。总览保留报价/证据/建议，新增“记录”“复盘”“系统”导航；304px下导航换行，操作按真实工作顺序排列，表格有界可横向滚动。

记录页以成交列表为主，明确真实导入/未归属与USER_REPORTED；反馈与人工报告放独立表单，归属/核实直接对应选中行。复盘页以冻结组列表、版本和证据为主，原建议检查、成本缺口、后见快照分列，1h/24h只有明确选择后排期。系统页显示证据时间、断线、预热、预算与本地worker，不把未启用或UNKNOWN填0。

不添加营销hero/收益装饰、嵌套卡片、CDN/收费素材/Node依赖。所有用户文本textContent/Jinja自动转义；动态日期Asia/Shanghai，金额保持字符串，状态不能仅靠颜色。动作明确是本地记录，不能显示为真实下单。

## 第一段：保护API与脱敏查询

- [x] 先测review-groups列表/冻结、版本分页、手工复盘、显式1h/24h任务/取消，默认cookie与Host/Origin/CSRF，确认strict true、scope服务端所有、固定错误。
- [x] ReviewQueryService投影只提供页面需要的数量/费用/时间/归属/原建议检查/后见行情，不暴露account_ref/orderID/requestID/full snapshots；最大50组/10版本/20成交检查，分页明确。
- [x] 系统查询读同一安全overview及持久预算；读失败明确unavailable，不虚构零费用；worker错误固定枚举。
- [x] 目标缺失RED→最小实现→定点验证→独立复核。

## 第二段：页面与完整流程

- [x] 已确认风格→建议/反馈→导入成交归属→复盘版本/手工回访HTTP集成及无证据状态测试；初始风格确认沿用既有会话回归，不把Fake导入视作真实联调。
- [x] 导航/本地assets/模板与textContent转义；实际浏览器恶意HTML无注入节点，敏感值不导出测试通过。
- [x] records/reviews/status与原生JS表单；写入对应显式确认，待重试操作ID/原payload保留；提交绑定原分组，旧读响应与旧checks按钮失效，独立复核通过。
- [x] 总览轻量SVG只记录最近120个已接收报价；重复/旧/未来报价拒绝，缺口断开，模式变化清空，无历史回放或无限队列。

## 第三段：验收与交接

- [x] 完整888项/122subtests、Ruff/223文件format、wheel隔离验证通过。
- [x] 独立disabled/Fake测试库；桌面与304px实际检查，表单确认/保存/刷新/归属/版本/回访/取消/时间/UNKNOWN通过，无console error；output/verification/t14-*.jpg。
- [x] 状态/台账/运行说明更新，继续T15；真实WS/账户/MCP/模型和24h soak仍未验收。
