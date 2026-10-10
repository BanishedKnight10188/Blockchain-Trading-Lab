"use strict";
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const nodes=new Map(),facts=new Map(),loads=[];
function node(id){
  if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,
    elements:{confirmed:{checked:false},action:{value:"pause",querySelector(){return {disabled:false};}}},
    dispatchEvent(){},addEventListener(){}});
  return nodes.get(id);
}
const view={enabled:false,analysis_target:{market:"usdt_perpetual",symbol:"BTCUSDT"},
  account:null,cycles:[],operations:[],model_budget:{provider_managed:true,active:true,read_only:false}};
const W={api:async()=>view,set(){},facts(id,rows){facts.set(id,rows);},rows(){},bind(){},
  load:fn=>()=>{const result=fn();result.catch(()=>{});loads.push(result);return result;}};
vm.runInNewContext(fs.readFileSync("agent_platform/web/static/futures-trader.js","utf8"),{
  window:{Workbench:W,addEventListener(){}},document:{getElementById:node,dispatchEvent(){}},
  CustomEvent:class{},Event:class{},setInterval(){return 0;},clearInterval(){},Date});
(async()=>{
  await loads[0];
  assert.match(JSON.stringify(facts.get("futures-budget")),/OpenRouter/);
  assert.doesNotMatch(JSON.stringify(facts.get("futures-budget")),/剩余费用/);
  console.log("PASS: legacy futures page accepts provider-managed budget without balance");
})().catch(error=>{console.error(error);process.exitCode=1;});
