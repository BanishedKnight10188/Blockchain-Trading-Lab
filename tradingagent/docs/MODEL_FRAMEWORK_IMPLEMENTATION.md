# 模型主体框架实施计划

本文件保留F1–F4历史实施证据。后续默认串行级联已被用户新要求覆盖：Jev独立开关、建议/自动两模式，自动先testnet；当前接续JEV_INDEPENDENT_IMPLEMENTATION.md。文中旧级联“下一阶段”不再执行。

> 使用superpowers:executing-plans在当前目录逐项实施。用户2026-10-06明确授权先开发主体，强模型做可选择模块，暂不调用；沿用自主推进授权，不另建worktree、安装依赖或提交。

**Goal:** 提供可替换的Flash/Jev适配器和有预算约束的决策接口，强模型仅可选择且保持禁用。
**Architecture:** Domain保存不可变模块配置与typed question/answer；Application保留预算、解析与失败处理；Adapters使用固定OpenRouter端点和有界HTTP。现有Web启动继续无模型、无收费，强模型选择不会装配执行器。
**Tech Stack:** 正式Conda Python3.12、Pydantic2、httpx0.28.1、pytest；无新依赖。
**Spec:** MODEL_SELECTION.md及PRODUCT_SPEC.md；本次用户要求优先于此前强模型自动复核建议。

## Global Constraints

- 只在D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent开发。真实交易由人执行。
- 强模型ID可选择，calls_enabled固定false；没有隐式fallback或真实模型联调。
- 所有新增HTTP验证使用MockTransport；pytest继续阻止外部网络。
- 凭据不进入RuntimeConfig、Prompt、日志、数据库或报告；默认不读取模型环境变量。
- 保留15秒既有决策期限、预算预留/UNKNOWN、全局60次/小时和发布前风险重验。
- 本次交付主体配置、Transport、两种Adapter及Jev预算执行器；完整Flash→Jev发布级联、策略配置UI和真实联调属于后续M3–M5，不假装已接通。

## Review Focus

1. 强模型已选择、Key存在或配置被篡改：没有请求，没有预算预留。F1/F3。
2. 重定向、恶意/重复JSON、超大流、供应商错误回显Key：固定端点、流量有界、错误脱敏、只尝试一次。F2。
3. 答案损坏但账单有效：保留确认费用；超时/取消费用UNKNOWN，不释放预留。F3/F4。
4. Jev请求/返回模型版本、问题集、概率或类型不一致：不交付可用判断，不把confidence当胜率。F4。
5. 日预算0、重复派发、晚到响应或价格失效：派发前拒绝/发布结果拒绝，持久费用不丢失。F4。

## F1 模块选择与关闭边界

Files: domain/model_modules.py、config.py、cli.py、bootstrap.py、application/system_queries.py；tests/domain/test_model_modules.py。
Interfaces: ModelModulesConfig(analysis_model固定Flash, decision_model固定Jev, strong_model可选择且不得与二者重叠)；StrongModelSelection(model_id=None,calls_enabled=False)。RuntimeConfig.model_modules只含非凭据配置；CLI web/soak --strong-model仅选ID；/api/status显示选择与禁用，启动不装配模型执行器。
- [x] RED：配置roundtrip、非法ID/秘密字段/调用启用拒绝、CLI/实际Web状态选择仍paid=false。
- [x] GREEN：实现不可变配置并接入现有入口；不创建任何外部客户端。
- [x] 验证：新增测试与既有配置/系统投影相关测试通过；记录证据，不提交。

## F2 OpenRouter有界HTTP

Files: adapters/openrouter/transport.py；tests/adapters/test_openrouter_transport.py。
Interfaces: OpenRouterCredentials；OpenRouterClient(clock,credentials=None,enabled=False,client=None).post(path,payload,deadline)->dict；aclose()只关闭自有客户端。
- [x] RED：默认零请求、只允许/chat及/alpha/decisions、固定主机、redirect拒绝、429/超时单次、JSON重复key/NaN/流超限拒绝、Key回显和日志脱敏。
- [x] GREEN：显式opt-in、256KiB响应/128KiB输入、有界超时、禁止redirect/fallback，borrowed client不关。
- [x] 验证：MockTransport故障输入与实际流关闭、取消测试通过。

## F3 Flash生成式Adapter与已知费用失败

Files: adapters/openrouter/chat.py、ports/model.py、domain/model_calls.py、application/decisions.py；tests/adapters/test_openrouter_chat.py及application相关回归。
Interfaces: OpenRouterChatModel(client,router,clock,modules,providers).generate(ModelRequest)->ModelResponse；ModelCallFailed(reason,usage=None)携带已知费用，不含原始body；ModelResponse记录有限provider metadata。
- [x] RED：固定分析模型、严格JSON schema、完整usage/推理输出计数、未知成本保留、强模型路线拒绝、无效建议仍确认结算、实际模型版本不冒充请求别名。
- [x] GREEN：复用build_prompt/parse_assessment与quote，先解析费用再解析建议；错误费用由DecisionService验证后结算。
- [x] 验证：Adapter测试及原DecisionService/SQLite费用/Prompt验证通过。

## F4 Jev独立契约、Adapter与预算执行器

Files: domain/decision_models.py、ports/decision_model.py、adapters/openrouter/jev.py、application/decision_models.py；tests/domain/test_decision_models.py、tests/adapters/test_openrouter_jev.py、tests/application/test_decision_models.py。
Interfaces: DecisionQuestion/DecisionModelRequest/DecisionModelResponse；DecisionModelPort.decide(request,quote)；BudgetedDecisionModel.decide(request)，使用现有BudgetStorePort，不直接发布交易建议。
- [x] RED：Choice/Score/Noul有限不可变问题/答案；request绑定完整问题集，概率分布/score/版本错误拒绝；成本已知错误仍结算。
- [x] GREEN：专用Decisions payload/parser；同一Key但不混用Chat Schema；有版本价格/单次上限、原子reserve(require_new)、每次小时计数、UNKNOWN保留与过期拒绝。
- [x] 验证：真实SQLite+Fake HTTP贯通、零预算/重复/取消/晚到/强模型隔离，专项和完整离线回归、Ruff通过。

## 执行记录

基线57项通过；Windows测试需同时设置测试进程PYTHONUTF8=1，使其CLI子进程编码一致。首次未设置时56通过/1读取stderr失败，统一进程编码后57通过；未改Conda、产品或既有测试。
Ruling: 沿用用户指定工作目录和IMPLEMENTATION_LEDGER，不执行Git/安装/清理脚本；本计划无提交。新增范围先交付可测试模块和Adapter，后续发布级联需单独细化事务/时效。

F1–F4离线关闭：独立复核7项发现加wire自查实际RED→137项专项GREEN；最终全套1056 passed/134 subtests passed（223.67s），Ruff check ./280文件format、新wheel -I隔离检查通过。具名缺陷与处理见output/verification/model-framework-review-20261006.md；完整验证见IMPLEMENTATION_LEDGER.md。
默认装配无模型/0预算，未调用收费接口；下一阶段M3只推进Flash→Jev发布级联和当前能力状态迁移，强模型仍不调用。继续沿用15秒总期限、当前证据重验和持久子调用预算；M4用户策略/纪律配置与标注评测、M5真实联调是后续范围。
