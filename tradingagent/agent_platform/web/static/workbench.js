"use strict";
window.Workbench=(()=>{
  const pending=new WeakMap(), busy=new WeakSet();
  const labels={disabled:"数据读取未启用",fake:"离线 Fake 数据",replay:"离线回放",live_read_only:"Binance 只读",
    buy:"买入",sell:"卖出",hold:"等待 / 持有",unclassified:"未归属",human:"本人决定",agent:"Agent 原建议",
    pending:"待处理",done:"已完成",failed:"失败",canceled:"已取消",verified:"已核实",conflict:"字段冲突",
    initial:"初始复盘",manual:"手工回访",followup:"事后回访",unknown:"未知",partial:"部分已知",known:"已知",
    not_connected:"未启用 / 未连接",unavailable:"不可评估",warming:"预热中",ready:"正常",fresh:"已同步",stale:"已过期",gap:"数据缺口",
    accepted:"已采纳",rejected:"已拒绝",modified:"修改后决定",independent:"独立想法",published:"有效建议",expired:"已失效",superseded:"已被取代",
    running:"运行中",paused:"已暂停",closed:"已结束",created:"待启动"};
  const label=value=>labels[value]||value||"—";
  const node=(tag,value,cls)=>{const n=document.createElement(tag);if(value!==undefined&&value!==null)n.textContent=String(value);if(cls)n.className=cls;return n;};
  const set=(id,value)=>{const n=document.getElementById(id);if(n)n.textContent=value??"—";};
  const time=value=>value?new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(value)):"—";
  const message=(value,error=false)=>{set("page-message",value);document.getElementById("page-message")?.classList.toggle("error",error);};
  async function api(path,body){const options={credentials:"same-origin"};if(body!==undefined){Object.assign(options,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify(body)});}
    let response;try{response=await fetch(path,options);}catch{throw new Error("本地连接中断，操作结果未确认。请保留内容并重试。");}
    let data;try{data=await response.json();}catch{throw new Error("本地服务返回格式异常，请重新载入。");}
    if(!response.ok)throw new Error(data.detail||"操作未确认，请重新载入并核对当前版本。");return data;}
  function update(form){const button=form.querySelector('button[type="submit"]');if(button)button.disabled=busy.has(form)||!form.elements.confirmed?.checked;}
  function bind(form,handler){form.addEventListener("change",()=>update(form));form.addEventListener("submit",async event=>{event.preventDefault();if(busy.has(form)||!form.elements.confirmed?.checked)return;busy.add(form);update(form);try{await handler();}catch(error){message(error.message,true);}finally{busy.delete(form);update(form);}});update(form);}
  async function write(form,path,body,automatic=()=>({})){const signature=JSON.stringify([path,body]);let attempt=pending.get(form);if(!attempt||attempt.signature!==signature){const payload={...body,...automatic(),confirmed:true};attempt={signature,payload:JSON.stringify(payload)};pending.set(form,attempt);}const result=await api(path,JSON.parse(attempt.payload));pending.delete(form);form.elements.confirmed.checked=false;update(form);message("本地记录已保存。");return result;}
  const identity=prefix=>prefix+"-"+crypto.randomUUID();
  const mode=value=>set("data-mode",label(value)+(value==="fake"||value==="replay"?"，不代表真实行情或账户。":"；独立自动执行资格见“JEV 操盘”。"));
  function facts(id,values){const list=document.getElementById(id);list.replaceChildren();for(const [name,value] of values){const div=node("div");div.append(node("dt",name),node("dd",value??"—"));list.append(div);}}
  function rows(id,values,width,empty){const tbody=document.getElementById(id);tbody.replaceChildren();if(!values.length){const td=node("td",empty);td.colSpan=width;const row=node("tr");row.append(td);tbody.append(row);}for(const valuesRow of values){const row=node("tr");for(const value of valuesRow){const cell=node("td");if(value instanceof Node)cell.append(value);else cell.textContent=value??"—";row.append(cell);}tbody.append(row);}}
  const load=operation=>async()=>{try{await operation();}catch(error){message(error.message,true);}};
  return {label,node,set,time,message,api,bind,write,identity,mode,facts,rows,load};
})();
