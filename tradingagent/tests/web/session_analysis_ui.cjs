"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const nodes = new Map();
let refresh, view = {target:{market:"spot"}}, available = true, chartAvailable=true, chartHistory=null, delayChart=false;
const delayed=[],requests=[];
function element(tag) {
  return {tag, hidden:true, textContent:"", attributes:{}, children:[],handlers:{},
    replaceChildren(){this.children=[];}, append(...items){this.children.push(...items);},
    setAttribute(key,value){this.attributes[key]=value;},
    addEventListener(event,fn){this.handlers[event]=fn;if(tag==="refresh-overview") refresh=fn;}};
}
function node(id) {
  if (!nodes.has(id)) nodes.set(id, element(id));
  return nodes.get(id);
}
const window = {analysisMarket:undefined, setAnalysisMarket(value){this.analysisMarket=value;}, addEventListener(){}};
node("chart-interval").value="1h"; node("chart-count").value="300";
const context = {window, document:{getElementById:node, createElement:element, createElementNS:(_,tag)=>element(tag)},
  fetch:async(url)=>{
    requests.push(url);
    if (url.includes("/chart?")) {
      const interval=new URL(url,"http://local").searchParams.get("interval");
      const response={ok:available && chartAvailable && !!chartHistory,json:async()=>({session_id:view.session_id,history:{...chartHistory,interval}})};
      if(delayChart) return new Promise(resolve=>delayed.push({resolve,response}));
      return response;
    }
    return {ok:available,json:async()=>view};
  }, setInterval(){}, Date, Number, Math, AbortController};
vm.runInNewContext(fs.readFileSync("agent_platform/web/static/session-analysis.js","utf8"), context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function reload(){refresh();await tick();}
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(node("legacy-spot-overview").hidden,false);
  available=false;
  await reload();
  assert.equal(node("legacy-spot-overview").hidden,true,"identity error must hide old Spot projection");
  assert.equal(node("initial-analysis-panel").hidden,true);
  assert.equal(window.analysisMarket,"unknown");
  assert.ok(node("page-message").textContent);
  view={session_id:"selected",target:{market:"usdt_perpetual",symbol:"ETHUSDT",history_days:7},reason:"model_unconfigured"};
  available=true;
  await reload();
  assert.equal(node("initial-analysis-panel").hidden,false);
  assert.equal(node("legacy-spot-overview").hidden,true);
  assert.equal(window.analysisMarket,"usdt_perpetual");
  assert.equal(node("page-message").textContent,"");
  assert.ok(node("initial-target").textContent.includes("ETHUSDT"));
  view.reason="history_unavailable";
  view.history_retry_at="2026-10-08T00:00:00Z";
  await reload();
  assert.ok(node("initial-status").textContent.includes("60 秒"));
  assert.ok(node("initial-history-note").textContent.includes("恢复后自动显示 K 线"));
  assert.equal(node("history-chart").children.length,0);
  view.reason="model_unconfigured";
  view.record={style:{strength:92},style_revision:1,history:{
    source:"binance_futures_public", captured_at:"2026-10-07T02:05:00Z",
    requested_start:"2026-10-07T00:00:00Z", requested_end:"2026-10-07T02:00:00Z",
    candles:[
      {opened_at:"2026-10-07T00:00:00Z",closed_at:"2026-10-07T00:59:59.999Z",open:"100",high:"110",low:"90",close:"105"},
      {opened_at:"2026-10-07T01:00:00Z",closed_at:"2026-10-07T01:59:59.999Z",open:"105",high:"112",low:"95",close:"100"},
    ],
  }};
  chartHistory={...view.record.history,market:"usdt_perpetual",symbol:"ETHUSDT",interval:"1h"};
  view.reason="history_unavailable";
  await reload();
  const chart=node("history-chart");
  assert.equal(chart.children.filter(c=>c.tag==="rect").length,2,"chart works independently of failed initial analysis history");
  assert.equal(chart.children.filter(c=>c.tag==="path").length,2,"each OHLC bar has its high/low wick");
  const bodies=chart.children.filter(c=>c.tag==="rect");
  assert.notEqual(bodies[0].attributes.fill,bodies[1].attributes.fill,"up/down distinguishable");
  assert.ok(bodies[0].children[0].textContent.includes("高 110"));
  assert.ok(chart.children.some(c=>c.tag==="text" && c.textContent.includes("112")),"axis uses high/low, not closes");
  assert.ok(node("initial-history-note").textContent.includes("Binance 合约公共数据"));
  assert.ok(node("captured-at").textContent.includes("图表采集"));
  node("chart-interval").value="5m";
  node("chart-interval").handlers.change(); await tick();
  assert.ok(node("chart-target").textContent.includes("5m"));
  assert.ok(node("initial-target").textContent.includes("1 小时"),"model's saved window remains hourly");
  delayChart=true;
  node("chart-interval").value="1m"; node("chart-interval").handlers.change();
  node("chart-interval").value="15m"; node("chart-interval").handlers.change();
  assert.equal(delayed.length,2);
  delayed[1].resolve(delayed[1].response); await tick();
  delayed[0].resolve(delayed[0].response); await tick();
  assert.ok(node("chart-target").textContent.includes("15m"),"late response cannot overwrite selection");
  assert.equal(chart.children.filter(c=>c.tag==="rect").length,2);
  delayChart=false;
  chartAvailable=false;
  await reload();
  assert.equal(chart.children.filter(c=>c.tag==="rect").length,2,"failed refresh keeps confirmed bars for the same interval");
  assert.ok(node("chart-status").textContent.includes("上次确认"));
  assert.ok(node("captured-at").textContent.includes("更新失败"));
  chartAvailable=true;
  view.record.history.candles[0].high="invalid";
  await reload();
  assert.equal(chart.children.length,0,"invalid bars must clear old prices");
  assert.ok(node("chart-status").textContent.includes("暂不可读"));
  assert.ok(requests.every(url=>url.startsWith("/api/analysis/")),"no model or trade actions from chart controls");
  console.log("Chart intervals, OHLC, model-history isolation, identity and late-response guards: passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
