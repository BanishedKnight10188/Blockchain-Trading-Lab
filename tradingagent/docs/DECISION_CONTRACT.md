# T11决策与费用契约

当前交付为离线编排内核。供应商/真实价格/日预算尚未配置，不发收费请求。
Runtime调度和Web建议投影另按T11装配；不要把内核测试当作完整首版验收。

## 输入与路由

DecisionRequest冻结原始快照、风格原值/版本、路线、请求时间、deadline和Prompt版本。
prepare默认15秒上限且不超过trigger有效期。日预算0、缺模型或有效价格返回规则路线；
没有显式规则则UNAVAILABLE，不以普通HOLD掩盖缺失策略。JEV仍未定义。
economy/standard/review只按明确配置选择，不由模型升级；费率必须带版本与有效时间。

有限Prompt≤64KiB，只含BTC/USDT所需余额、指标、时效、缺失值、纪律及风格。
金额精确字符串；不含account_ref、订单/成交ID、完整日志或凭据。
Provider Adapter必须使用此投影，不允许直接导出ModelRequest.snapshot。
当前没有正式HTTP收费Adapter；Fake/录制响应仅证明工程行为。

## 请求与费用

SQLite v7先写immutable snapshot与claim。相同ID不同输入拒绝；完成精确重试返回原事实。
崩溃留下in-flight时不自动重claim/收费。历史PUBLISHED返回不代表它仍适合当前账户；
Web查询须根据当前会话、账户与有效期重新展示状态。

每次派发必须先BudgetPort.reserve(require_new=True)。与旧T03幂等读取模式不同，
此模式在同一写锁内拒绝任何既存RESERVED/UNKNOWN/SETTLED，不能把预算旧事实当新许可。
手工、普通建议与review共享持久小时额度（上限60）和上海日预算，重启不清零。
估计使用全部Prompt字节的保守token上界、显式开销与配置最大输出。
预留等待后再次检查deadline、当前路线与价格有效期；确定未派发时confirmed费用为0，
释放费用敞口并保留未派发原因。小时额度按已预留尝试保守计数，不伪称完成模型调用。

已派发但超时、取消、坏费用身份/极端金额时UNKNOWN保留估计敞口；
token_counts_known=false明确表示计数未知，0仅是该状态的存储占位，不代表已测零消耗。
合法费用与assessment独立：迟到或坏建议不能抹掉有效收费事实。
用量绑定本次request/route/model/quote价格、估计值、token cap和实际预留开始时间。
费用≤128位、exp±128；有限实际超估按真实值保存并冻结后续收费。
失败最多一次免费规则回退，每条回退仍经硬纪律；没有隐式第二次收费调用。

## 发布与竞争

原始输入快照保持不变，另存publication snapshot；risk引用后者，不能先于其证据时间。
发布前读取最新缓存与已确认账户并重评估硬纪律，风格不放宽任何限制。
期限从原始snapshot.captured_at算起，不由模型返回时间刷新；数量建议仍须用户纪律、
过滤器与显式成本证据。缺少时UNAVAILABLE，不生成假定数量。

同一SQLite事务核对RUNNING/session_revision/style_revision/原风格及真实account_revision。
同余额刷新不制造资金版本变化；风格/实质余额变化保留原作者与原文并SUPERSEDED。
账户失败或时效失效为UNAVAILABLE。每个snapshot_id在所有请求中只能描述同一事实。
完成usage必须已在对应预算事务结算；仅finish不能注入收费身份。

首次建议先created、再final两条审计与CAS状态；预写event-only阶段不得伪装已提交。
最终审计失败回滚全部本次候选/完成记录，保留先前claim。
写入末尾再次检查行情/盘口/指标/账户/trigger/deadline/日亏损证据有效时间，
SAVEPOINT撤销写入期间过期的候选，只记录UNAVAILABLE。

## 证据

路由16项、Prompt27项、台账26项、DecisionService29项；均离线。
审查/RED/完整回归见IMPLEMENTATION_LEDGER.md及output/verification。
真实模型价格、供应商凭据/计费、账户与网络联调尚未验收。
