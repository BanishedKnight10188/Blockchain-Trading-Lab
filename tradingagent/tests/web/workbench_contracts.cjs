"use strict";
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const html=fs.readFileSync("agent_platform/web/templates/jev-workspace.html","utf8");
assert.match(html,/<select\s+id="symbol-input"/,"all contracts need a real selector, not a BTC-filtered datalist");
class Node {
  constructor(){this.value="";this.children=[];this.listeners={};this.textContent="";this.disabled=false;}
  addEventListener(kind,callback){this.listeners[kind]=callback;}
  append(child){this.children.push(child);}
  replaceChildren(...children){this.children=children;this.value=children[0]?.value||"";}
}
const scope=vm.createContext({window:{},document:{createElement:()=>new Node()}});
vm.runInContext(fs.readFileSync("agent_platform/web/static/contract-selector.js","utf8"),scope);
function setup(fetchCatalog){const select=new Node(),search=new Node(),status=new Node(),retry=new Node();
  const selector=new scope.window.ContractSelector({select,search,status,retry,fetchCatalog});
  return {select,search,status,retry,selector};}
(async()=>{
  const contracts=[{symbol:"BTCUSDT"},{symbol:"ETHUSDT"},{symbol:"SOLUSDT"}];
  const ctx=setup(async()=>({contracts}));await ctx.selector.load();
  assert.equal(ctx.select.value,"BTCUSDT");assert.equal(ctx.select.children.length,3);
  assert(ctx.select.children.some(n=>n.value==="ETHUSDT"));
  ctx.search.value="eth";ctx.search.listeners.input();
  assert(ctx.select.children.some(n=>n.value==="ETHUSDT"));
  assert.equal(ctx.select.value,"BTCUSDT","search must not silently change the confirmed target");
  ctx.select.value="ETHUSDT";ctx.select.listeners.change();ctx.search.value="";ctx.search.listeners.input();
  assert.equal(ctx.select.value,"ETHUSDT");assert.equal(ctx.select.children.length,3);
  let denied=true;const retry=setup(async()=>{if(denied)throw new Error("offline");return {contracts};});
  await retry.selector.load();assert.equal(retry.select.children.filter(n=>n.value).length,0);
  assert.match(retry.status.textContent,/重新/);denied=false;await retry.selector.load();
  assert.equal(retry.select.children.length,3);assert.equal(retry.select.value,"BTCUSDT");
  denied=true;retry.select.value="ETHUSDT";retry.select.listeners.change();await retry.selector.load();
  assert.equal(retry.select.value,"ETHUSDT");assert.equal(retry.select.children.length,3);
  assert.match(retry.status.textContent,/保留/);
  console.log("PASS: full contract selection, independent filtering, failed load recovery and retained selection");
})().catch(error=>{console.error(error);process.exitCode=1;});
