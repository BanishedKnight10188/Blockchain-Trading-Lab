"use strict";
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const nodes=new Map();let refresh,available=true;
function node(id){if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,textContent:"",elements:{confirmed:{checked:false}},
  dispatchEvent(){},addEventListener(event,fn){if(id==="refresh-page")refresh=fn;}});return nodes.get(id);}
let view={enabled:false,analysis_target:{market:"usdt_perpetual",symbol:"ETHUSDT"},paper_market:"spot",market_compatible:false,
  session:{session_id:"eth",style_revision:1},account:null,cycles:[]};
const W={api:async()=>{if(!available)throw new Error("offline");return view;},set(id,value){node(id).textContent=value;},facts(){},rows(){},bind(){},message(){},load:fn=>async()=>{try{await fn();}catch{}}};
vm.runInNewContext(fs.readFileSync("agent_platform/web/static/paper-trader.js","utf8"),{
  window:{Workbench:W,setInterval(){},clearInterval(){},addEventListener(){}},document:{getElementById:node},Event:class{},Date});
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(node("spot-paper-controls").hidden,true,"futures must hide Spot BTC configuration");
  assert.ok(node("paper-market").textContent.includes("ETHUSDT")&&node("paper-market").textContent.includes("永续合约"));
  view={...view,analysis_target:{market:"spot",symbol:"BTCUSDT"},market_compatible:true};
  await refresh();
  assert.equal(node("spot-paper-controls").hidden,false);
  assert.ok(node("paper-market").textContent.includes("现货"));
  available=false;
  await refresh();
  assert.equal(node("spot-paper-controls").hidden,true,"identity lookup failure must hide old Spot controls");
  assert.equal(node("paper-config-fields").disabled,true);
  assert.ok(node("paper-market").textContent.includes("无法确认"));
  available=true;
  await refresh();
  assert.equal(node("spot-paper-controls").hidden,false);
  console.log("Trader market labels and Spot controls isolation: passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
