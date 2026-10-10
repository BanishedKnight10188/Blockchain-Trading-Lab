"use strict";
const slider = document.getElementById("style-strength");
const confirmed = document.getElementById("style-confirmed");
const save = document.getElementById("save-style");
const reload = document.getElementById("reload-session");
const message = document.getElementById("form-message");
const start = document.getElementById("start-session");
const pause = document.getElementById("pause-session");
const close = document.getElementById("close-session");
let session = null;
let busy = true;
const marketSelect = document.getElementById("analysis-market");
const symbolSelect = document.getElementById("analysis-symbol");
const symbolSearch = document.getElementById("symbol-search");
const historyDays = document.getElementById("history-days");
let contracts = [];
let catalogReady = false;

function describeStyle() {
  const value = Number(slider.value);
  const label = value <= 33 ? "偏保守" : value <= 66 ? "均衡" : "偏激进";
  document.getElementById("style-value").value = String(value);
  document.getElementById("style-label").textContent = label;
  document.getElementById("style-description").textContent = value <= 33
    ? "更耐心地等待信号确认，优先谨慎判断。"
    : value <= 66 ? "在等待确认和及时响应之间保持平衡。"
    : "更积极地关注市场变化，在纪律范围内及时响应。";
  slider.setAttribute("aria-valuetext", `${value} / 100，${label}`);
}

function buttons() {
  const targetReady = session || marketSelect.value === "spot" || (catalogReady && contracts.some(c => c.symbol === symbolSelect.value));
  save.disabled = busy || !confirmed.checked || !targetReady;
  reload.disabled = busy;
  slider.disabled = busy;
  confirmed.disabled = busy;
  for (const node of [marketSelect, symbolSelect, symbolSearch, historyDays]) node.disabled = busy || session !== null;
  const unsaved = session && Number(slider.value) !== session.style.strength;
  const futures = session?.analysis_target.market === "usdt_perpetual";
  start.disabled = busy || !session || unsaved || !["configured","paused"].includes(session?.status);
  pause.disabled = busy || !session || unsaved || session?.status !== "running";
  close.disabled = busy || !session || unsaved;
  start.textContent = futures ? (session?.status === "paused" ? "恢复合约会话" : "启动合约会话") : session?.status === "paused" ? "恢复评估" : "启动评估";
}

function notice(text, error = false) {
  message.textContent = text;
  message.classList.toggle("error", error);
}

function localTime(timestamp) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(timestamp));
}

function render(data) {
  const previous = session;
  session = data.session?.status === "closed" ? null : data.session;
  const history = document.getElementById("style-history");
  history.replaceChildren();
  if (session) {
    slider.value = String(session.style.strength);
    document.getElementById("selection-mode").textContent = "修改当前会话";
    save.textContent = "保存风格";
    document.getElementById("session-state").textContent = {configured:"已配置，尚未启动",running:"评估运行中",paused:"评估已暂停"}[session.status];
    document.getElementById("saved-style").textContent = `${session.style.strength} / 100 · ${data.style_label}`;
    document.getElementById("saved-revision").textContent = `v${session.style_revision}`;
    document.getElementById("saved-at").textContent = localTime(session.updated_at);
    marketSelect.value = session.analysis_target.market;
    historyDays.value = String(session.analysis_target.history_days);
    symbolSelect.replaceChildren(new Option(session.analysis_target.symbol, session.analysis_target.symbol));
    document.getElementById("saved-target").textContent = `${session.analysis_target.symbol} · ${session.analysis_target.market === "spot" ? "现货" : "USDT 永续"} · ${session.analysis_target.history_days} 天`;
    for (const item of [...data.style_history].reverse()) {
      const row = document.createElement("li");
      const value = document.createElement("span");
      value.textContent = `v${item.revision}　${item.strength} / 100`;
      const time = document.createElement("time");
      time.dateTime = item.changed_at;
      time.textContent = localTime(item.changed_at);
      row.append(value, time);
      history.append(row);
    }
  } else {
    if (previous) {
      marketSelect.value = "usdt_perpetual";
      historyDays.value = "7";
      symbolSearch.value = "";
    }
    slider.value = "50";
    document.getElementById("selection-mode").textContent = "新会话";
    save.textContent = "创建会话";
    document.getElementById("session-state").textContent = "尚未创建";
    document.getElementById("saved-style").textContent = "等待选择";
    document.getElementById("saved-revision").textContent = "—";
    document.getElementById("saved-at").textContent = "—";
    document.getElementById("saved-target").textContent = "等待选择";
    populateContracts();
    const empty = document.createElement("li");
    empty.className = "empty-history";
    empty.textContent = "创建会话后，这里会显示你的选择。";
    history.append(empty);
  }
  confirmed.checked = false;
  const connected = data.readiness.market !== "not_connected" || data.readiness.account !== "not_connected";
  document.getElementById("connection-title").textContent = connected ? "只读数据服务已启用" : "行情与账户尚未接入";
  document.getElementById("connection-detail").textContent = connected
    ? "请在只读总览检查数据来源、同步状态与采样时间。" : "当前可设置和保存会话风格。行情与账户状态可在只读总览查看。";
  if (session?.analysis_target.market === "usdt_perpetual") {
    document.getElementById("connection-title").textContent = "合约历史由独立后台准备";
    document.getElementById("connection-detail").textContent = "创建后自动读取历史；首次分析状态可在大盘查看。请先启动合约会话，再到 JEV 操盘页启动独立模拟决策循环。";
  }
  describeStyle();
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin", ...options,
    headers: {"Content-Type": "application/json",
      "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content},
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.detail || "请求未完成，请重新载入后再试。");
    error.status = response.status;
    throw error;
  }
  return data;
}

async function loadSession() {
  busy = true;
  buttons();
  notice("正在载入会话…");
  try {
    render(await request("/api/session"));
    notice(session ? "已恢复当前会话的风格。" : "选择风格并确认，即可创建会话。");
    busy = false;
  } catch (error) {
    notice(error.message, true);
    // Without a current revision, saving cannot safely proceed.
    reload.disabled = false;
    return;
  }
  buttons();
}

slider.addEventListener("input", () => {
  confirmed.checked = false;
  describeStyle();
  buttons();
  notice("请确认当前选择后保存。");
});
confirmed.addEventListener("change", buttons);
reload.addEventListener("click", () => loadSession().then(loadContracts));
document.getElementById("style-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !confirmed.checked) return;
  busy = true;
  buttons();
  notice("正在保存…");
  const changing = session !== null;
  const payload = {style_strength: Number(slider.value), style_confirmed: true};
  if (changing) payload.expected_revision = session.revision;
  else payload.analysis_target = {market:marketSelect.value, symbol:marketSelect.value === "spot" ? "BTCUSDT" : symbolSelect.value, interval:"1h", history_days:Number(historyDays.value)};
  try {
    const data = await request(changing ? `/api/sessions/${session.session_id}/style` : "/api/sessions", {
      method: changing ? "PUT" : "POST", body: JSON.stringify(payload),
    });
    render(data);
    notice(changing ? "风格已保存，后续建议将使用新版本。" : session?.analysis_target.market === "usdt_perpetual" ? "合约会话已创建；历史由后台准备，请在大盘与分析查看首次分析状态。" : "现货会话已创建，请在大盘与分析查看现货行情与建议。");
  } catch (error) {
    if (error.status === 409) {
      try { render(await request("/api/session")); } catch { /* Keep the explicit reload action. */ }
    }
    notice(error.message, true);
  } finally {
    busy = false;
    buttons();
  }
});
async function changeState(status) {
  if (busy || !session) return;
  busy = true; buttons(); notice("正在更新会话状态…");
  try {
    render(await request(`/api/sessions/${session.session_id}/state`, {
      method:"PUT", body:JSON.stringify({status,expected_revision:session.revision}),
    }));
    notice({running:"评估已启动，请在总览查看数据与建议状态。",paused:"评估已暂停，当前建议已撤去。",closed:"会话已结束，历史事实已保留。"}[status]);
  } catch (error) {
    if (error.status === 409) {
      try { render(await request("/api/session")); } catch { /* Explicit reload remains available. */ }
    }
    notice(error.message,true);
  } finally { busy=false; buttons(); }
}
start.addEventListener("click",()=>changeState("running"));
pause.addEventListener("click",()=>changeState("paused"));
close.addEventListener("click",()=>changeState("closed"));
function populateContracts() {
  if (session) return;
  const selected = symbolSelect.value;
  symbolSelect.replaceChildren();
  if (marketSelect.value === "spot") {
    symbolSelect.append(new Option("BTCUSDT · 现货", "BTCUSDT"));
    return;
  }
  const query = symbolSearch.value.trim().toUpperCase();
  const filtered = contracts.filter(c => c.symbol.includes(query) || c.base_asset.includes(query));
  for (const c of filtered) symbolSelect.append(new Option(`${c.symbol} · ${c.base_asset}`, c.symbol));
  if (filtered.some(c => c.symbol === selected)) symbolSelect.value = selected;
  else if (filtered.some(c => c.symbol === "BTCUSDT")) symbolSelect.value = "BTCUSDT";
}
async function loadContracts() {
  try {
    const data = await request("/api/analysis/contracts");
    contracts = data.contracts; catalogReady = true;
    document.getElementById("contract-note").textContent = `已读取 ${contracts.length} 个可交易 USDT 永续合约。币种在本会话中固定；首次只分析已收盘历史。`;
    populateContracts();
  } catch (error) {
    catalogReady = false;
    document.getElementById("contract-note").textContent = error.message;
  }
  buttons();
}
for (const node of [marketSelect,symbolSelect,historyDays]) node.addEventListener("change",()=>{confirmed.checked=false; if(node===marketSelect) populateContracts(); buttons();});
symbolSearch.addEventListener("input",()=>{confirmed.checked=false; populateContracts(); buttons();});
describeStyle();
loadSession().then(loadContracts);
