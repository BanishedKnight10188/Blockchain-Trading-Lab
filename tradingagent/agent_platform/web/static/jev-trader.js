"use strict";
(()=>{
  const W=window.Workbench, form=document.getElementById("trader-form"), fields=document.getElementById("trader-fields"), refresh=document.getElementById("refresh-page");
  let revision=null,generation=0,saving=false,dirty=false;
  const states={advisory_only:"给出建议，由你执行",trader_disabled:"操盘模块关闭，自动执行暂停",execution_unconfigured:"自动执行未就绪：账户、策略、资金限额与执行器待配置"};
  function render(data,force=false){
    if(dirty&&!force&&revision!==data.revision){form.elements.confirmed.checked=false;form.dispatchEvent(new Event("change"));W.message("服务器设置已变化，编辑已保留；请核对当前状态并重新确认后保存。");}
    revision=data.revision;
    if(!dirty||force){form.elements.enabled.checked=data.jev_trader.enabled;form.elements.mode.value=data.operation.mode;if(form.elements.execution_environment)form.elements.execution_environment.value=data.operation.execution_environment;form.elements.confirmed.checked=false;dirty=false;}
    W.set("trader-revision",`操盘版本 ${data.jev_trader.revision} · ${revision?"保存于 "+W.time(data.updated_at):"尚未确认保存"}`);
    W.set("data-mode","独立操盘设置；Haiku 和 JEV 建议的开关不受本模块影响。");
    W.facts("trader-status",[["模块开关",data.jev_trader.enabled?"已选择开启":"关闭"],["操盘方式",data.operation.mode==="auto"?"自动操盘":"给出建议"],["执行环境",data.operation.execution_environment],["模型连接",data.operation.execution_environment==="paper"?"见下方对应市场的模型与运行状态":W.label(data.jev_trader.connection_state)],["实际执行状态",data.operation.execution_environment==="paper"?"见下方 Paper 运行状态":!data.jev_trader.enabled?"操盘模块关闭":states[data.operation.state]||"条件不可评估"],["真实下单能力",data.operation.writes_enabled?"已启用":"未启用"]]);
  }
  async function load(){if(saving)return;const own=++generation;fields.disabled=true;try{const data=await W.api("/api/controls");if(own===generation)render(data);}finally{if(own===generation)fields.disabled=revision===null;}}
  W.bind(form,async()=>{if(revision===null||saving)return;const body={mode:form.elements.mode.value,execution_environment:form.elements.execution_environment?.value||"testnet",enabled:form.elements.enabled.checked,expected_revision:revision};saving=true;++generation;fields.disabled=true;refresh.disabled=true;
    try{render(await W.write(form,"/api/controls/trader",body),true);}finally{saving=false;fields.disabled=false;refresh.disabled=false;}
  });refresh.addEventListener("click",W.load(load));W.load(load)();
  form.addEventListener("input",()=>{dirty=true;});form.addEventListener("change",()=>{dirty=true;});
})();
