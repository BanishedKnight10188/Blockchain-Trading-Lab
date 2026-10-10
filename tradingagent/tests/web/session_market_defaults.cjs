"use strict";
const fs=require("node:fs"), vm=require("node:vm"), assert=require("node:assert/strict");
const nodes=new Map();
class Option {constructor(text,value){this.textContent=text;this.value=value;}}
function node(id){
  if(!nodes.has(id))nodes.set(id,{id,value:id==="analysis-market"?"usdt_perpetual":id==="style-strength"?"50":"",checked:false,
    addEventListener(){},setAttribute(){},classList:{toggle(){}},append(){},
    replaceChildren(...children){if(id==="analysis-symbol")this.value=children[0]?.value??"";}});
  return nodes.get(id);
}
const empty={session:null,style_history:[],readiness:{market:"not_connected",account:"not_connected"}};
const scope=vm.createContext({document:{getElementById:node,querySelector:()=>({content:"test"}),createElement:()=>node("row")},
  Option,Intl,Date,fetch:async(path)=>({ok:true,json:async()=>path==="/api/session"?empty:{contracts:[]}})});
vm.runInContext(fs.readFileSync("agent_platform/web/static/session.js","utf8"),scope);
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(node("analysis-market").value,"usdt_perpetual");
  node("analysis-market").value="spot";
  scope.render(empty);
  assert.equal(node("analysis-market").value,"spot","retain user's new-session market choice on reload");
  scope.render({session:{session_id:"old",status:"configured",style:{strength:80},style_revision:1,
    updated_at:"2026-10-07T09:00:00Z",analysis_target:{market:"spot",symbol:"BTCUSDT",history_days:1}},
    style_history:[],readiness:empty.readiness,style_label:"偏激进"});
  assert.equal(node("analysis-market").value,"spot","retain saved old Spot identity");
  scope.render(empty);
  assert.equal(node("analysis-market").value,"usdt_perpetual","closing old Spot session defaults next session to futures");
  assert.equal(node("history-days").value,"7");
  const changedCoin={session:{session_id:"ogn",status:"configured",style:{strength:85},style_revision:1,
    updated_at:"2026-10-08T12:17:00Z",analysis_target:{market:"usdt_perpetual",symbol:"OGNUSDT",history_days:7}},
    style_history:[],readiness:empty.readiness,style_label:"偏激进"};
  scope.render(changedCoin);scope.buttons();
  assert.equal(node("start-session").disabled,false,"new futures session must expose a start action before JEV trading");
  assert.equal(node("start-session").textContent,"启动合约会话");
  assert.equal(node("saved-target").textContent,"OGNUSDT · USDT 永续 · 7 天");
  scope.render({...changedCoin,session:{...changedCoin.session,status:"running"}});scope.buttons();
  assert.equal(node("start-session").disabled,true);
  assert.equal(node("pause-session").disabled,false,"running futures session can be paused");
  scope.render({...changedCoin,session:{...changedCoin.session,status:"paused"}});scope.buttons();
  assert.equal(node("start-session").disabled,false);
  assert.equal(node("start-session").textContent,"恢复合约会话");
  node("style-strength").value="86";scope.buttons();
  assert.equal(node("start-session").disabled,true,"unsaved style still blocks starting");
  console.log("New sessions default to futures; saved Spot and draft selection are preserved: passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
