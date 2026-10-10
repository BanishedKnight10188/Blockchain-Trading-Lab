"use strict";
(()=>{
const W=window.Workbench;
async function load(){const data=await W.api("/api/status");W.mode(data.mode);const market=data.market,account=data.account,runtime=data.decision_runtime;W.facts("data-status",[
["公共行情",W.label(market.status)],["报价时间",W.time(market.quote_at)],["盘口状态",W.label(market.book_status)],["指标状态",W.label(market.feature_status)],
["账户同步",W.label(account.status)],["账户证据时间",W.time(account.as_of)],["下次同步",W.time(account.next_attempt_at)],["历史覆盖",account.history_complete?"本次接口历史覆盖完整，库存覆盖仍待确认":"未确认完整"],["读取故障",market.error||account.error?"读取条件未满足，请核对本机配置或网络":"暂无已记录故障"]]);
const health=data.runtime, archive=data.archive;
const states={starting:"启动中",running:"运行中",degraded:"部分后台降级",paused:"暂停",stopping:"停止中",stopped:"已停止"};
const logWorker=health?.workers?.find(worker=>worker.name==="diagnostics");
W.facts("worker-status",[
["整体后台",states[health?.status]||"暂不可用"],["后台组件数",health?.worker_count],
["评估后台",runtime?.running?"运行中":"未运行"],["模型在途",runtime?.inflight?"有在途请求":"无已知在途请求"],
["会话状态",W.label(runtime?.session_status)],
["评估条件",runtime?.reason==="current_evidence_unavailable"?"当前证据不足":runtime?.reason==="persistence"?"存储故障，评估停止":runtime?.reason==="deciding"?"正在评估":"等待会话与有效证据"],
["持久回访后台",data.review_worker.running?"运行中":"未运行"],
["回访故障",data.review_worker.error?"本地回访暂不可用，请检查存储":"暂无已记录故障"],
["辅助行情归档",!archive?.enabled?"未启用":archive.error?"已降级，请检查本地存储":archive.running?"运行中":"未运行"],
["归档采样与分钟记录",archive?.enabled?archive.observations_written:"尚未启用"],
["本轮清理记录",archive?.enabled?archive.observations_removed:"尚未启用"],
["归档在途",archive?.pending_count?"正在写入一批记录":"无在途写入"],
["辅助行情保留", archive?`1秒采样${archive.raw_retention_days}天 / 已收盘分钟${archive.minute_retention_days}天；已固定审计永久保留`:"暂不可用"],
["本地诊断日志",logWorker?.failure?"存储故障，请检查本机磁盘":logWorker?.running?"轮转记录中":"暂不可用"],
["Haiku 主分析","常态主分析，模型尚未连接"],
["JEV 建议",data.model_modules?.jev?.enabled?"已选择开启，尚未连接":"关闭"],
["JEV 操盘",data.model_modules?.jev_trader?.enabled?"已选择开启，尚未连接":"关闭"],
["操盘方式",data.operation?.mode==="auto"?"自动操盘（testnet）":"给出建议"],
["执行资格",data.operation?.state==="trader_disabled"?"操盘模块关闭":data.operation?.mode==="auto"?"执行器尚未接通":"由你决定是否执行"],
["收费模型",data.paid_models_enabled?"已显式启用":"关闭"]]);
const budget=data.budget;W.set("budget-note",data.budget_status==="unavailable"?"费用账本暂不可读；请恢复本地存储后重新载入。":"持久账本已读取，实际费用与未知预留分别保留。");W.facts("budget-status",[["预算日期（上海）",budget?.budget_day],["每日上限 / USD",budget?.daily_limit_usd],["已确认费用 / USD",budget?.spent_usd],["在途或未知预留 / USD",budget?.reserved_usd],["最近1小时请求数",budget?.hourly_call_count],["收费冻结",budget?budget.billing_frozen?"已冻结":"未冻结":"未知"],["配置路由数",data.routing.configured_routes],["费用采样",W.time(data.budget_as_of)]]);W.set("status-generated","状态读取于 "+W.time(data.generated_at));}
document.getElementById("refresh-page").addEventListener("click",W.load(load));W.load(load)();
})();
