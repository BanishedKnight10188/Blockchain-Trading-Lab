"use strict";
(()=>{
  const W=window.Workbench, section=document.getElementById("futures-archive"), next=document.getElementById("futures-archive-next");
  let account=null, after=0, through=null, nextAfter=null, generation=0, loading=false, total=0,snapshotTotal=null;
  const intentLabel=value=>({open_long:"开多",open_short:"开空",add_long:"加多",add_short:"加空",reduce:"减仓",close:"全平",trade:"成交",funding:"资金费",liquidation:"清算"})[value]||value;
  async function load(reset=false){
    if(!account||loading)return;
    if(reset){after=0;through=null;}
    loading=true;next.disabled=true;const current=generation;
    try{
      const params=new URLSearchParams({after:String(after),limit:"25"});
      if(through!==null)params.set("through",String(through));
      const page=await W.api("/api/futures-trading/archive?"+params);
      if(current!==generation)return;
      through=page.through;nextAfter=page.next_after;snapshotTotal=page.total;
      W.rows("futures-archive-rows",page.entries.map(e=>{
        const r=e.record, op=r.operation, evidence=r.execution_command?.decision_evidence;
        const details=W.node("details"), summary=W.node("summary",e.origin==="legacy_import"?"旧记录 · 缺证据见详情":evidence?(evidence.decision_source==="real_jev"?"真实 JEV · 完整详情":"离线验证 · 完整详情"):"资金事件 / 非模型 · 详情");
        const pre=W.node("pre",JSON.stringify(e,null,2));pre.style.maxWidth="40rem";pre.style.maxHeight="28rem";pre.style.overflow="auto";
        details.append(summary,pre);
        const plan=evidence?.plan, basis=plan?.sizing_basis==="equity_margin"?"权益保证金":plan?.sizing_basis==="current_quantity"?"原持仓数量":null;
        return [`${e.sequence} · ${W.time(op.occurred_at)}`,`${r.state.symbol} · ${intentLabel(plan?.intent||r.order?.action||r.kind)}${basis?` · ${basis} ${plan.percent}%`:""}`,`${op.quantity} / ${op.price??"—"}`,`${r.before_state?.settings?.leverage??"未知"} → ${r.state.settings.leverage} 倍 / ${op.fee_usdt} USDT`,details];
      }),5,"暂无成交、资金费或清算档案；不会生成模拟成交。");
      W.set("futures-archive-status",`档案 ${total} 笔 · 本快照 ${page.total} 笔 · 本页 ${page.entries.length} 笔 · 完整性校验通过`);
    }finally{if(current===generation){loading=false;next.disabled=nextAfter===null;}}
  }
  document.addEventListener("futures-trading-state",event=>{
    const data=event.detail;section.hidden=data.market!=="usdt_perpetual";
    total=data.summary?.total??0;
    if(account!==data.account){account=data.account||null;generation++;after=0;through=null;nextAfter=null;snapshotTotal=null;loading=false;W.load(()=>load(true))();}
    else if(snapshotTotal!==null&&total!==snapshotTotal)W.set("futures-archive-status",`当前 ${total} 笔档案 · 本快照 ${snapshotTotal} 笔 · 刷新查看新增记录`);
  });
  document.getElementById("futures-archive-refresh").addEventListener("click",W.load(()=>load(true)));
  next.addEventListener("click",W.load(async()=>{if(nextAfter!==null){after=nextAfter;await load();}}));
})();
