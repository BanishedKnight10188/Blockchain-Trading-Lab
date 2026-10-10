(() => {
  "use strict";
  let state = null;
  const el = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  async function api(path, body, method = "POST") {
    const response = await fetch("/api/event-agent/" + path, {
      method: body ? method : "GET",
      headers: body ? {"Content-Type": "application/json", "X-CSRF-Token": csrf} : {},
      body: body ? JSON.stringify(body) : undefined
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.detail || "操作失败");
    return value;
  }
  function operation() {
    if (!state?.available) throw new Error("请先装配事件 Agent");
    return {expected_revision: state.lane.revision, confirmed: true};
  }
  async function load() {
    state = await api("status");
    for (const id of ["agent-start", "agent-pause", "agent-analyze"]) el(id).disabled = !state.available;
    if (!state.available) {
      el("agent-capability").textContent = "尚未启用。启用事件 Agent 配置后，重启隔离服务即可使用。";
      return;
    }
    el("agent-capability").textContent = (state.lane.enabled ? "已启用" : "已暂停") +
      " · " + state.model_source + " · 行情 " + state.market_source + " · 保护 " + state.protection_status;
    el("agent-wallet").textContent = state.account ?
      "独立钱包 " + state.account.scope.account_ref + " · 可用 " + state.account.free_usdt +
      " USDT · 持仓 " + (state.account.side || "空仓") + " " + state.account.quantity :
      "资金与风险尚未配置；仍可进行离线研究与假设监控。";
    el("data-mode").textContent = "事件 Agent · Paper · " + (state.paid_calls_enabled ? "显式费用授权" : "无收费调用");
    el("agent-watches").replaceChildren();
    for (const w of state.watches) {
      const row = document.createElement("p");
      const text = document.createElement("span");
      text.textContent = w.state + " · " + w.definition.timeframe + " · " +
        w.definition.hypothesis + " · " + (w.reason || "") + " ";
      row.append(text);
      if (["ARMED", "TRIGGERED"].includes(w.state)) {
        const button = document.createElement("button");
        button.type = "button"; button.className = "secondary"; button.textContent = "取消假设";
        button.onclick = () => run(() => api("watches/" + encodeURIComponent(w.definition.watch_id) + "/cancel",
          {expected_revision: w.revision, lane_revision: state.lane.revision, confirmed: true}));
        row.append(button);
      }
      el("agent-watches").append(row);
    }
    el("agent-runs").replaceChildren();
    for (const r of state.runs) {
      const p = document.createElement("p");
      const reasons = r.tools.map(t => t.result?.reason).filter(Boolean);
      p.textContent = r.started_at + " · " + r.mode + " · " + r.status + " · " +
        (r.decision?.reason || r.reason || "") + " " + reasons.join(" / ");
      el("agent-runs").append(p);
    }
    el("agent-executions").replaceChildren();
    for (const item of state.executions) {
      const r = item.receipt;
      const tr = document.createElement("tr");
      for (const value of [item.command_id, r?.status || item.operation.kind, r?.filled_quantity ?? "",
        r?.average_price ?? "", r?.fee_usdt ?? item.operation.fee_usdt ?? ""]) {
        const td = document.createElement("td"); td.textContent = value; tr.append(td);
      }
      el("agent-executions").append(tr);
    }
    el("agent-evidence").textContent = JSON.stringify({
      events: state.events, costs: state.costs, protections: state.protections
    }, null, 2);
  }
  async function run(action) {
    try { el("page-message").textContent = "正在处理…"; await action(); await load(); el("page-message").textContent = "已保存"; }
    catch (error) { el("page-message").textContent = error.message; }
  }
  el("agent-start").onclick = () => run(() => api("start", operation()));
  el("agent-pause").onclick = () => run(() => api("pause", operation()));
  el("agent-analyze").onclick = () => run(() => api("analyze", {...operation(), request_id: crypto.randomUUID()}));
  el("agent-risk").onsubmit = e => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    run(() => api("configure", {...operation(),
      policy: {order_notional_usdt:f.order_notional_usdt, max_price_drift_bps:f.max_price_drift_bps,
        min_confidence:"0", strategy_instructions:f.strategy_instructions},
      limits:{initial_usdt:f.initial_usdt,leverage:Number(f.leverage),max_leverage:Number(f.leverage),
        max_position_notional:f.max_position_notional,max_run_loss_usdt:f.max_run_loss_usdt,
        fee_bps:f.fee_bps,slippage_bps:f.slippage_bps},
      qty_step:f.qty_step,min_qty:f.min_qty,max_qty:f.max_qty,min_notional:f.min_notional}));
  };
  el("agent-watch").onsubmit = e => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    const now = new Date();
    run(() => api("watches", {...operation(),definition: {
      watch_id:"manual:"+crypto.randomUUID(),definition_revision:1,
      lane_id:state.lane.lane_id,session_id:state.lane.session_id,symbol:state.lane.symbol,
      timeframe:f.timeframe,hypothesis:f.hypothesis,created_at:now.toISOString(),
      expires_at:new Date(now.getTime()+Number(f.minutes)*60000).toISOString(),
      trigger:{logic:"ALL",conditions:[{metric:f.metric,op:f.op,value:f.value}]},
      invalidation:f.invalid_below ? {logic:"ALL",conditions:[{metric:"candle.close",op:"LT",value:f.invalid_below}]} : null
    }}));
  };
  el("refresh-page").onclick = () => run(load);
  load().catch(error => { el("page-message").textContent = error.message; });
})();
