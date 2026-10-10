# T11后台与只读建议装配

历史验证点说明（2026-10-06）：下文“JEV未定义”属于原需求假设。用户已明确指TypeSafe Jev决策模型；原T11离线证据不覆盖其专用Adapter或级联。新增范围以[MODEL_SELECTION.md](D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent/docs/MODEL_SELECTION.md)为准。

沿用已验证DecisionService/预算/发布台账，不重写。分三步测试先行。

1. AdviceQueryService读取当前活跃会话、最新DecisionClaimReceipt及已确认账户，
   只投影建议内容、原始风格/证据时间、风险与脱敏费用。查询不触发模型或交易所读取。
   in-flight/UNAVAILABLE/EXPIRED/SUPERSEDED与普通HOLD分开；风格、账户、暂停及数据过期时
   不能把历史PUBLISHED展示为当前建议。latest查询按持久claim顺序，不用内存历史。
2. SnapshotFactory从LatestOverview/SessionStore缓存及已确认账户构建有限不可变快照；
   缺失/未来/scope错误关闭评估。PublicationEvidencePort另取新快照，保留原始证据引用
   和新的当前事实引用。没有真实资金纪律时不生成数量。JEV未定义。
3. 单独DecisionRuntime维护T08 scheduler/trigger与决策worker，模型在途不阻塞采样、
   账户同步或硬风险通知；单会话单在途。会话只有明确RUNNING时调度，暂停/关闭废弃
   当前建议；本地会话状态操作使用已有cookie/Origin/CSRF与CAS审计，不新增资金写能力。
   bootstrap默认无模型/无付费预算/无策略，Web清楚说明不可用原因。Fake完整链路验证后
   才称T11离线装配完成；真实网络/供应商联调仍单列。

测试覆盖查询隐私/时效、风格/账户竞争、后台独立、停止取消、预算失败、硬提示不等LLM、
Fake→SQLite→Web。使用用户已有Conda环境，不安装包或发收费请求。
