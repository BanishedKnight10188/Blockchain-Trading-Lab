"use strict";
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.hidden=false;this.disabled=false;this.open=false;this._text="";}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join("");}
  set innerHTML(value){throw new Error("archive must render untrusted values as text");}
  append(...values){this.children.push(...values);}
  replaceChildren(...values){this._text="";this.children=values;}
  addEventListener(name,callback){this.listeners[name]=callback;}
  async emit(name){return this.listeners[name]?.({target:this});}
}
const context={window:{},document:{createElement:tag=>new Element(tag)},URLSearchParams,Intl,Date};
vm.runInNewContext(fs.readFileSync("agent_platform/web/static/jev-archive.js","utf8"),context);
const archive=context.window.JevArchive;
const wallet={symbol:"RLCUSDT",quantity:"0",side:null,free_usdt:"1000",margin_usdt:"0",settings:{leverage:10}};
const entry={sequence:1274,origin:"recorded",previous_hash:"0".repeat(64),content_hash:"a".repeat(64),record:{
  kind:"funding",command_id:"funding:1",before_state:wallet,state:wallet,
  funding:{settled_at:"2026-10-10T00:00:00Z",rate:"0.0001",mark:"1.01"},
  operation:{occurred_at:"2026-10-10T00:00:03Z",quantity:"0",price:null,fee_usdt:"0",funding_usdt:"0",realized_pnl_usdt:"0"}}};
function find(root,tag){return root.tagName===tag?root:root.children.map(child=>find(child,tag)).find(Boolean);}
function findText(root,text){return root.tagName==="button"&&root.textContent===text?root:root.children.map(child=>findText(child,text)).find(Boolean);}
function view(api){
  const elements={rows:new Element("tbody"),info:new Element("p"),status:new Element("p"),raw:new Element("pre"),
    next:new Element("button"),refresh:new Element("button"),json:new Element("button")};
  return {elements,controller:archive.create({...elements,api})};
}
(async()=>{
  const before=JSON.stringify(entry),summary={total:6,counts:{trade:0,funding:6,liquidation:0}};
  assert.match(archive.summaryText(summary),/0 笔模拟成交/);
  assert.match(archive.summaryText(summary),/6 次资金费结算/);
  const page={entries:[entry],total:6,through:3230,next_after:1274};
  const paths=[],v=view(async path=>{paths.push(path);return page;});
  await v.controller.update("rlc1",summary);
  assert.match(v.elements.rows.textContent,/资金费结算.*空仓.*未扣款/);
  assert.equal(v.elements.raw.hidden,true,"raw JSON is opt-in");
  assert.equal(find(v.elements.rows,"pre").hidden,true);
  const button=findText(v.elements.rows,"JSON");
  await button.emit("click");
  assert.equal(JSON.parse(find(v.elements.rows,"pre").textContent).content_hash,entry.content_hash);
  await v.elements.json.emit("click");
  assert.equal(v.elements.raw.hidden,false);
  assert.equal(JSON.parse(v.elements.raw.textContent).total,6);
  await v.elements.next.emit("click");
  assert.match(paths.at(-1),/after=1274/);
  assert.match(paths.at(-1),/through=3230/);
  assert.equal(JSON.stringify(entry),before,"view must not rewrite original evidence");
  const legacy={...entry,origin:"legacy_import",record:{...entry.record,kind:"trade",before_state:null,
    execution_command:null,operation:{...entry.record.operation,side:"buy",quantity:"2",price:"1.02"}}};
  const legacyView=view(async()=>({entries:[legacy],total:1,through:1274,next_after:null}));
  await legacyView.controller.update("old",{total:1,counts:{trade:1,funding:0,liquidation:0}});
  assert.match(legacyView.elements.rows.textContent,/旧记录/);
  assert.match(legacyView.elements.rows.textContent,/未记录/);
  let resolveOld;
  const stale=view(path=>path.includes("/old/")?new Promise(resolve=>{resolveOld=resolve;}):Promise.resolve(page));
  const loading=stale.controller.update("old",summary);
  await stale.controller.update("new",summary);
  resolveOld({entries:[legacy],total:1,through:1274,next_after:null});await loading;
  assert.match(stale.elements.rows.textContent,/资金费结算/);
  assert.doesNotMatch(stale.elements.rows.textContent,/旧记录/);
  console.log("PASS: classified archive, readable details, opt-in JSON, stable cursor and task isolation");
})().catch(error=>{console.error(error);process.exitCode=1;});
