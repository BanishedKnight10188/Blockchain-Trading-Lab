"use strict";
(()=>{
  const W=window.Workbench, form=document.getElementById("advice-form"), fields=document.getElementById("advice-fields"), refresh=document.getElementById("refresh-advice");
  let revision=null, generation=0, saving=false;
  function render(data){
    revision=data.revision;form.elements.enabled.checked=data.jev_advice.enabled;form.elements.confirmed.checked=false;
    W.set("control-revision",`建议版本 ${data.jev_advice.revision} · ${revision?"保存于 "+W.time(data.updated_at):"尚未确认保存"}`);
    W.facts("jev-advice-status",[["建议开关",data.jev_advice.enabled?"已选择开启":"关闭"],["模型连接",W.label(data.jev_advice.connection_state)],["最新判断",data.jev_advice.decision||"暂无已验证判断"],["下单能力","本建议板块不下单"]]);
  }
  async function load(){if(saving)return;const own=++generation;fields.disabled=true;try{const data=await W.api("/api/controls");if(own===generation)render(data);}finally{if(own===generation)fields.disabled=revision===null;}}
  W.bind(form,async()=>{if(revision===null||saving)return;const body={enabled:form.elements.enabled.checked,expected_revision:revision};saving=true;++generation;fields.disabled=true;refresh.disabled=true;
    try{render(await W.write(form,"/api/controls/advice",body));}finally{saving=false;fields.disabled=false;refresh.disabled=false;}
  });refresh.addEventListener("click",W.load(load));W.load(load)();
})();
