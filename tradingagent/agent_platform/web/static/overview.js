"use strict";
const labels = {not_connected:"未连接", unavailable:"不可用", warming:"预热中", ready:"正常",
  fresh:"已同步", stale:"已过期", gap:"数据缺口", none:"未启用", fake:"离线 Fake 数据",
  replay:"离线回放", binance_direct:"Binance 只读", buy:"买入", sell:"卖出", new:"挂单中",
  partially_filled:"部分成交", filled:"已成交", canceled:"已取消", expired:"已过期", rejected:"被拒绝"};
const failures = {credentials:"本机凭据未配置", authentication:"只读认证失败，请检查本机凭据和权限",
  rate_limit:"读取受到限流，等待下次同步", transport:"连接失败，等待恢复", invalid_data:"数据校验失败",
  persistence:"本地存储失败，账户同步已停止，请恢复存储后重启服务", not_connected:"等待接收到数据"};
const adviceReasons = {no_session:"请先创建并明确确认会话风格。",no_decision:"尚无评估结果，请启动会话并等待数据就绪。",
  feedback_recorded:"已记录你的反馈；这不代表交易已执行。", decision_changed:"更新的评估已替代当前记录。",
  recommendation_state_unavailable:"建议状态暂不可读，请检查本地存储。",
  session_changed:"会话或风格已改变，原建议已被取代。",account_changed:"账户事实已改变，原建议已被取代。",
  advice_expired:"原建议有效期已结束。",unfinished_request:"评估未在截止时间内完成，费用以本地账本为准。",
  current_evidence_unavailable:"当前证据不足或过期，暂不能评估。",rule_unconfigured:"尚未配置可使用的规则或模型。",
  model_unconfigured:"模型未配置。",budget_unavailable:"模型预算不可用。",market_not_ready:"行情未就绪。",
  market_quote_stale:"报价已过期。",features_not_ready:"指标尚未预热完成。",features_stale:"指标已过期。",
  account_unavailable:"账户未确认或同步已过期。",provider_cancelled:"评估已取消，未知费用仍保留在账本。",
  provider_timeout:"模型评估超时。",publication_expired:"发布前证据已失效。",request_expired:"评估请求已失效。",
  persistence:"本地存储故障，建议评估已停止，请恢复存储后重启。",invalid_data:"评估数据校验失败。"};
let stream = null;
let latest = null;
let receivedAt = 0;
let loading = false;
const quoteSeries = new window.QuoteSeries();
const button = document.getElementById("refresh-overview");
const text = (id, value) => { const node=document.getElementById(id); node.textContent=value ?? "—"; node.title=node.textContent; };
const time = value => value ? new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", month:"2-digit", day:"2-digit",
  hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false}).format(new Date(value)) : "—";
function state(id, value) { const node=document.getElementById(id); node.textContent=labels[value] || "不可评估"; node.dataset.state=value; }
function age(value) { return value && latest ? (new Date(latest.generated_at)-new Date(value))/1000+(performance.now()-receivedAt)/1000 : Infinity; }
function freshness() {
  if (!latest || window.analysisMarket !== "spot") return;
  let market=latest.market.status, account=latest.account.status;
  if (market === "ready" && age(latest.market.quote_at)>5) market="stale";
  if (account === "fresh" && age(latest.account.as_of)>60) account="stale";
  state("market-state",market); state("account-state",account);
  let book=latest.market.book_status, features=latest.market.feature_status, orders=latest.account.orders_status;
  if (book === "ready" && age(latest.market.book_at)>5) book="stale";
  if (["ready","warming"].includes(features) && age(latest.market.features?.as_of)>60) features="stale";
  if (orders === "fresh" && age(latest.account.orders_as_of)>60) orders="stale";
  state("book-state",book); state("feature-state",features);
  text("orders-note",latest.account.order_count === null ? "尚未读取"
    : `${latest.account.order_count} 笔 · ${labels[orders]} · ${time(latest.account.orders_as_of)}${latest.account.order_count>20?" · 显示前20笔":""}`);
  renderAdvice();
}
function renderAdvice() {
  const advice=latest.advice;
  let status=advice?.status || "unavailable", reasons=advice?.reasons || [];
  if (status === "unavailable" && reasons.includes("no_decision")
      && latest.decision_runtime?.reason === "current_evidence_unavailable") reasons=["current_evidence_unavailable"];
  if (status === "pending" && advice.expires_at && age(advice.expires_at)>=0) {
    status="unavailable"; reasons=["unfinished_request"];
  }
  if (status === "published") {
    if (age(advice.expires_at)>=0) { status="expired"; reasons=["advice_expired"]; }
    else if (age(latest.market.quote_at)>5 || age(latest.market.features?.as_of)>60 || age(latest.account.as_of)>60
      || (advice.quantity !== null && age(latest.market.book_at)>5)) {
      status="unavailable"; reasons=["current_evidence_unavailable"];
    }
  }
  text("advice-state",{published:"有效建议",pending:"生成中",expired:"已失效",superseded:"已被取代",unavailable:"不可评估",accepted:"已采纳",rejected:"已拒绝"}[status]);
  document.getElementById("advice-state").dataset.state=status;
  text("advice-reason",status === "published" ? ({buy:"增持观点",sell:"减持观点",hold:"持有观点"}[advice.action])
    : status === "pending" ? "正在评估；行情和风险提示继续更新。" : reasons.map(reason=>adviceReasons[reason] || "评估条件尚不满足。").join(" ") || "请先管理会话并检查数据状态。");
  text("advice-explanation",status === "published" ? advice.explanation : "");
  text("advice-source",{fake:"离线模型测试",rule:"规则建议",model:"模型建议"}[advice?.source]);
  text("advice-style",advice?.style_strength !== null && advice?.style_strength !== undefined
    ? `${advice.style_strength} / 100 · v${advice.style_revision}` : null);
  text("advice-evidence-time",time(advice?.evidence_as_of)); text("advice-expires",time(advice?.expires_at));
  text("advice-quantity",status === "published" ? advice.quantity : null);
  const usage=advice?.usage;
  text("advice-usage",usage ? `估计 ${usage.estimated_cost_usd}；实际 ${usage.actual_cost_usd ?? "未知，预留仍保留"}${usage.token_counts_known ? "" : "；token 未知"}`
    : advice?.usage_status === "unavailable" ? "费用记录暂不可读" : status === "pending" ? "费用尚未结算" : "暂无已记录模型费用");
  const alert=latest.decision_runtime?.alerts?.at(-1);
  text("hard-alert",alert && age(alert.occurred_at)<=60
    ? `风险提示 ${time(alert.occurred_at)}：${alert.reasons.map(reason=>adviceReasons[reason] || "当前数据不可用。").join(" ")}` : "");
}
function rows(id, values, empty, width) {
  const body=document.getElementById(id); body.replaceChildren();
  if (!values.length) { const row=document.createElement("tr"), cell=document.createElement("td"); cell.colSpan=width; cell.textContent=empty; row.append(cell); body.append(row); return; }
  for (const valuesRow of values) { const row=document.createElement("tr"); for (const value of valuesRow) { const cell=document.createElement("td"); cell.textContent=value; row.append(cell); } body.append(row); }
}
function render(data) {
  latest=data; receivedAt=performance.now();
  if (window.analysisMarket !== "spot") return;
  quoteSeries.observe(data);
  window.renderQuoteChart(quoteSeries, document.getElementById("quote-chart"), document.getElementById("chart-note"));
  text("mode-note",data.mode === "live_read_only" ? "Binance 只读模式，交易由你执行。" : data.mode === "disabled"
    ? "行情与账户读取未启用。可先设置会话风格。" : "离线测试数据，不代表 Binance 实时行情或真实账户。");
  text("captured-at",`后台采样 ${time(data.captured_at)}`);
  const market=data.market, account=data.account, features=market.features;
  text("last-price",market.price); text("best-bid",market.bid); text("best-ask",market.ask);
  text("quote-time",time(market.quote_at)); text("book-time",time(market.book_at));
  text("market-source",labels[market.source]); state("feature-state",market.feature_status);
  text("market-note",market.error ? failures[market.error] : market.status === "not_connected"
    ? "公共行情未启用。启用后将在这里显示接收到的数据。" : market.price == null && market.book_status === "ready"
    ? "最新成交暂无；已显示实时盘口买卖报价。图表取买卖价均值，指标由已收盘 K 线计算。"
    : "报价按接收到的事实显示；指标由已收盘 K 线计算。");
  for (const [id,key] of [["ema-fast","ema_fast"],["ema-slow","ema_slow"],["atr","atr"],["vwap","vwap"],
    ["interval-return","interval_return"],["volatility","volatility"]]) text(id,features?.[key]);
  text("holding",account.quantity); text("account-time",time(account.as_of)); text("account-attempt",time(account.attempted_at));
  text("account-next",time(account.next_attempt_at));
  text("account-note",account.error ? failures[account.error] : account.status === "not_connected"
    ? "账户未启用。界面不会把缺失余额显示为零。" : "已确认余额独立于历史覆盖；持仓成本仍未知。");
  text("history-state",account.history_complete ? "本次读取覆盖成交接口可用历史" : "历史可能不完整");
  rows("balances",account.balances.map(item=>[item.asset,item.free,item.locked]),"尚无已确认余额",3);
  text("orders-note",account.order_count === null ? "尚未读取" : `${account.order_count} 笔 · ${time(account.orders_as_of)}${account.order_count>20?" · 显示前20笔":""}`);
  rows("orders",account.orders.map(item=>[labels[item.side],labels[item.status] || item.status,item.quantity,item.filled_quantity,item.price]),
    account.order_count === 0 ? "已确认没有当前挂单" : "尚无已确认挂单数据",5);
  freshness();
}
function connection(message, failed=false) { text("update-state",message); document.getElementById("update-state").classList.toggle("error",failed); }
function connect() {
  stream?.close(); stream=new EventSource("/api/events");
  stream.addEventListener("overview",event=>{ try { render(JSON.parse(event.data)); connection("本地实时更新已连接"); } catch { connection("数据校验失败，请重新载入",true); } });
  stream.onerror=()=>connection("实时更新断开，正在重连；请留意数据时间",true);
}
async function load() {
  if (loading) return; loading=true; button.disabled=true;
  try { const response=await fetch("/api/overview",{credentials:"same-origin"}); if (!response.ok) throw new Error();
    render(await response.json()); connection("本地数据已载入"); connect(); }
  catch { connection("无法读取本地状态，请刷新页面或检查服务是否运行",true); }
  finally { loading=false; button.disabled=false; }
}
button.addEventListener("click",load);
window.setAnalysisMarket = (market) => {
  window.analysisMarket = market;
  if (market === "spot" && latest) render(latest);
};
window.addEventListener("pagehide",()=>stream?.close());
setInterval(freshness,1000);
load();
