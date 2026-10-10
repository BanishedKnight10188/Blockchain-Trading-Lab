"use strict";
(()=>{
  const $=id=>document.getElementById(id), form=$("task-form");
  const labels={running:"运行中",paused:"已暂停",configured:"待启动",closed:"已结束",liquidated:"已清算",wait:"等待",advised:"已保存建议",filled:"已成交",rejected:"已拒绝",pending:"处理中",unknown:"待核实",interrupted:"已中断",OPEN_LONG:"开多 / 加多",OPEN_SHORT:"开空 / 加空",REDUCE:"减仓 / 平仓",WAIT:"等待",long:"多",short:"空"};
  const reasons={market_unavailable:"公共行情暂不可用，请检查连接后点击启动。",session_not_running:"会话尚未启动。",model_read_only:"当前模型配置只允许读取，不能启动收费预测。",model_budget_exhausted:"所有会话共用的模型费用已达到上限。",model_hourly_limit:"共享模型请求数达到小时上限。",decision_expired:"模型返回已超过有效期，本次没有成交。",low_confidence:"置信度未达到所设下限。",maintenance_unavailable:"行情或资金费维护暂不可用。",account_changed:"持仓或账户版本已更新，本次结果未执行。",prediction_superseded:"已有更新的预测结果。",close_requires_flat_position:"持仓未平，不能结束会话。",provider_transport_error:"模型连接中断，已暂停后续调用。"};
  let selected=localStorage.getItem("jev-task:"+location.port), tasks=[], current=null, step=0, wizard=false, busy=false, pending=null, chartHistory=null, chartTask=null, chartLoading=false, refreshBusy=false;
  const archive=window.JevArchive.create({api,rows:$("archive-rows"),info:$("archive-info"),status:$("archive-status"),raw:$("archive-content"),next:$("archive-next"),refresh:$("show-archive"),json:$("archive-json")});
  Object.assign(reasons,{background_model_unconfigured:"六层输入需要先配置背景模型和已验证价格，本轮未调用 JEV。",background_pending:"背景模型正在整理四层历史，完成后继续预测。",background_history_pending:"四层历史尚未完整，等待公共行情更新。",short_history_pending:"正在准备连续的 20 根 3m 和 60 根 1s 已收盘 K 线。",background_model_failed:"背景整理失败；请检查模型和费用，暂停后恢复可重试。"});
  const text=(id,value)=>{$(id).textContent=value??"—";}, label=x=>labels[x]||x||"—";
  const num=(value,digits=3)=>value===null||value===undefined?"—":Number(value).toLocaleString("zh-CN",{maximumFractionDigits:digits});
  const time=value=>value?new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(value)):"—";
  const note=(message,error=false)=>{text("notice",message);$("notice").classList.toggle("error",error);};
  async function api(path,body,method="POST"){
    const options={credentials:"same-origin"};
    if(body!==undefined)Object.assign(options,{method,headers:{"Content-Type":"application/json","X-CSRF-Token":document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify(body)});
    const response=await fetch(path,options);
    let data;try{data=await response.json();}catch{throw new Error("本地服务返回异常，请刷新页面。");}
    if(!response.ok)throw new Error(data.detail||"操作未完成，请重新载入。");return data;
  }
  function budget(value){
    if(!value){text("shared-remaining","离线模式 · 不收费");text("shared-cap","");return;}
    if(value.provider_managed){text("shared-remaining","额度由 OpenRouter 管理");text("shared-cap","每个会话单独记录模型费用");return;}
    text("shared-remaining",`已消耗 ${num(value.balance?.spent_usd,8)} USD`);
    text("shared-cap",`本地限额模式 · 累计 ${num(value.balance?.daily_limit_usd,6)} · 单次 ${num(value.single_call_usd,4)} USD`);
  }
  function sidebar(){
    $("task-list").replaceChildren();text("task-count",tasks.length);
    for(const row of tasks){
      const button=document.createElement("button");button.type="button";button.className="task-item"+(row.task_id===selected&&!wizard?" selected":"");button.setAttribute("aria-current",row.task_id===selected&&!wizard?"page":"false");
      const strong=document.createElement("strong"),small=document.createElement("small"),spend=document.createElement("small");strong.textContent=row.name;small.textContent=`${row.symbol} · ${row.kind==="jev_advice"?"建议":"模拟操盘"} · ${label(row.status)}`;
      spend.className="task-spend";
      const usage=row.model_usage;
      spend.textContent=usage?`已消耗 ${num(usage.spent_usd,8)} USD${Number(usage.unconfirmed_usd)>0?` · 待核实 ${num(usage.unconfirmed_usd,8)} USD`:""}`:usage===null?"离线模式 · 不收费":"费用待读取";
      button.append(strong,small,spend);button.addEventListener("click",()=>select(row.task_id));$("task-list").append(button);
    }
  }
  async function select(id){
    if(busy)return;selected=id;localStorage.setItem("jev-task:"+location.port,id);wizard=false;current=null;chartHistory=null;chartTask=null;
    $("wizard").hidden=true;$("workspace").hidden=false;sidebar();note("");text("task-title","载入会话…");text("task-subtitle","");text("chart-message","正在读取已收盘历史…");
    for(const id of ["equity","free","position","pnl"])text(id,"—");for(const id of ["start-task","pause-task","close-task","input-mode","save-input-mode","decision-interval","save-cadence"])$(id).disabled=true;
    archive.reset(id);draw();
    try{await loadView();await loadChart();}catch(error){note(error.message,true);}
  }
  async function loadList(){const data=await api("/api/jev-tasks");tasks=data.tasks;budget(data.model_budget);sidebar();return data;}
  async function loadView(){
    if(!selected||wizard)return;const id=selected;const data=await api("/api/jev-tasks/"+encodeURIComponent(id));
    if(selected!==id||wizard)return;current=data;render(data);
  }
  function timingDetails(cell,cycle){
    const local=cycle.local_timing||cycle.diagnostic?.local_timing, transport=cycle.transport_evidence||cycle.diagnostic?.transport_evidence;
    if(!local&&!transport)return;
    if(local?.provider_us!==undefined){const model=document.createElement("small");model.className="latency-model";model.textContent=`模型接口 ${num(local.provider_us/1000,2)} ms`;cell.append(model);}
    const details=document.createElement("details"),summary=document.createElement("summary"),info=document.createElement("div");details.className="latency-details";summary.textContent="分阶段耗时";const parts=[];
    if(local){parts.push(`费用预留 ${num(local.reserve_us/1000,2)} ms`,`费用结算 ${num(local.settle_us/1000,2)} ms`,`模型接口 ${num(local.provider_us/1000,2)} ms`,`本地校验 ${num((local.validation_us+local.fee_validation_us+local.binding_us)/1000,2)} ms`);}
    if(transport){parts.push(`接口内网络：连接 ${transport.connect_ms} / 代理 ${transport.proxy_connect_ms} / TLS ${transport.tls_ms} / 发送 ${transport.send_ms} / 等响应 ${transport.response_wait_ms} / 读响应 ${transport.body_ms} ms`);}
    info.textContent=parts.join("；");details.append(summary,info);cell.append(details);
  }
  function render(data){
    const account=data.account, session=data.session, kind=data.task.kind, state=data.readonly?"closed":account?.status||"configured";
    text("task-title",data.task.name);text("task-mode",kind==="jev_advice"?"JEV 实时监控 · 建议":"JEV 自动模拟操盘");
    text("task-subtitle",`${session?.analysis_target.symbol||"待初始化"} · USDT 永续 · 风格 ${data.style_strength} / 100 · 版本 ${session?.style_revision||1}`);
    text("run-status",label(state));$("run-status").className="status-pill "+state;
    const context=data.multiscale_context;
    $("context-status").hidden=false;
    if(context){const names={background_model_unconfigured:"需要配置背景模型与价格",background_pending:"背景模型正在整理",background_history_pending:"正在准备四层历史",background_model_failed:"背景整理失败，请检查后恢复",short_history_pending:"短周期 K 线尚未完整或已过期"};const counts=(context.windows||[]).slice(-2).map(w=>`${w.interval} ${w.count}/${w.requested_count}${w.fresh?"":"（待更新）"}`).join(" · ");text("context-status",`六层输入 · ${context.ready?"已就绪":names[context.reason]||context.reason} · ${counts} · 背景刷新 ${context.background_refresh_seconds/60} 分钟`);}
    else text("context-status",data.context_mode==="multiscale"?(data.readonly?"六层输入 · 此会话已结束，实际模型输入保存在决策与交易档案中。":"六层输入 · 输入模块正在初始化，请刷新查看准备进度。"):"原有小时历史 · 当前使用小时摘要和报价观察；可暂停后切换六层输入。");
    if(context?.failure_detail?.code)$("context-status").append(document.createTextNode(` · 原因：${reasons[context.failure_detail.code]||context.failure_detail.code}`));
    if(context?.background_model_id)$("context-status").append(document.createTextNode(` · 背景模型 ${context.background_model_id}`));
    const inputLocked=busy||data.readonly||state==="running"||state==="liquidated",input=$("input-mode");
    if(input.dataset.task!==data.task.task_id||!input.dataset.dirty){input.value=data.context_mode||"legacy";input.dataset.task=data.task.task_id;delete input.dataset.dirty;}
    input.disabled=inputLocked;$("save-input-mode").disabled=inputLocked;
    text("input-mode-note",data.readonly?"已结束会话保留当时的输入设置。":state==="running"?"先暂停此会话再切换输入；其他会话继续运行。":"保存后保持暂停，保留钱包、持仓和交易档案；点击恢复运行后才允许模型调用。六层首次需准备背景与连续 60 根秒线。");
    $("start-task").disabled=busy||data.readonly||state==="running"||state==="liquidated"||data.model_read_only;
    text("start-task",state==="paused"?"恢复运行":"启动");$("pause-task").disabled=busy||data.readonly||state!=="running";
    $("close-task").disabled=busy||data.readonly||Number(account?.quantity||0)!==0;
    const failure=data.task.failure||data.worker_failure||data.maintenance_failure||(state==="paused"&&(data.cycles||[])[0]?.reason==="invalid_model_assessment"?"invalid_model_assessment":null);
    $("task-warning").hidden=!failure;text("task-warning",(failure==="market_unavailable"?JevDecisionMessages.market(data.market_status):JevDecisionMessages.failure(failure==="provider_timeout_retry"?"provider_timeout":failure,JevDecisionMessages.diagnosticFor(failure,data.cycles),data.model_recovery))||reasons[failure]||`运行提示：${failure}。详情见下方决策记录。`);
    text("equity-label",kind==="jev_advice"?"参考虚拟权益 · USDT":"模拟净权益 · USDT");
    text("equity",num(account?.equity_usdt??(account?Number(account.free_usdt)+Number(account.margin_usdt):null)));
    text("free",num(account?.free_usdt));text("position",account?Number(account.quantity)?`${label(account.side)} ${num(account.quantity,8)} / ${account.leverage}×`:"空仓 · 杠杆由 JEV 选择":"待建立钱包");
    text("pnl",account?`${num(account.realized_pnl_usdt)} / ${num(account.unrealized_pnl_usdt)}`:"—");
    text("market-title",(session?.analysis_target.symbol||"")+" 合约行情");
    const q=data.quote?.quote||data.quote;const source=data.market_source==="offline_replay"?"离线数据":"Binance 公共行情";
    const clockInfo=JevDecisionMessages.clock(data.market_status?.clock);
    text("quote-source",source);
    text("mark-price",q?num(q.mark,8):"—");text("bid-price",q?num(q.bid,8):"—");text("ask-price",q?num(q.ask,8):"—");text("quote-time",q?time(q.received_at):"—");
    const quoteState=q?(data.quote_fresh===false?"stale":"available"):state==="closed"?"closed":"unavailable";
    $("market-quotes").dataset.state=quoteState;
    text("quote-status",quoteState==="stale"?"报价已过期":quoteState==="closed"?"会话已结束":quoteState==="unavailable"?"等待报价":"");
    $("quote-status").hidden=quoteState==="available";
    text("quote-line",q?clockInfo||"":state==="closed"?"已结束会话，交易证据保存在档案中。":`${JevDecisionMessages.market(data.market_status)}${clockInfo?" · "+clockInfo:""}`);
    text("prediction-info",`${data.cadence?.decision_seconds||1} 秒发起一次 · 最多 ${data.cadence?.max_predictions||3} 并行 · ${kind==="jev_advice"?"只记录建议":"过期结果不成交"}`);
    const cadenceLocked=busy||data.readonly||state==="running"||state==="liquidated";
    if(current?.task.task_id!==$("decision-interval").dataset.task||!$("decision-interval").dataset.dirty){$("decision-interval").value=String(data.cadence?.decision_seconds||1);$("decision-interval").dataset.task=data.task.task_id;delete $("decision-interval").dataset.dirty;}
    $("decision-interval").disabled=cadenceLocked;$("save-cadence").disabled=cadenceLocked;
    text("cadence-note",data.readonly?"已结束会话的分析间隔保留在档案中。":state==="running"?"先暂停此会话再调整间隔；其他会话继续运行。":"仅影响此会话；保存不会启动模型，行情与持仓维护仍每秒运行。");
    const body=$("decision-rows");body.replaceChildren();
    for(const c of data.cycles||[]){const tr=document.createElement("tr"),p=c.plan;let plan="—";
      if(p)plan=p.action==="wait"?"保持当前仓位":`${p.percent}% ${p.sizing_basis==="equity_margin"?"权益保证金":"当前数量"} · 数量 ${num(p.quantity,8)} · ${p.leverage}×`;
      const result=JevDecisionMessages.failure(c.reason,c.diagnostic,data.model_recovery)||((c.choice_probability_adjustments||[]).length?JevDecisionMessages.result(c):c.reason==="prediction_tick_skipped"?"距上次请求不足分析间隔，本轮跳过（未调用模型）":c.reason?reasons[c.reason]||c.reason:label(c.status));
      for(const [index,value] of [time(c.created_at),label(c.decision),plan,c.confidence===null?"—":num(c.confidence,2),c.model_latency_ms==null?"—":`${c.model_latency_ms} ms`,result].entries()){const td=document.createElement("td");td.textContent=value;if(index===4)timingDetails(td,c);tr.append(td);}body.append(tr);
    }
    if(!body.children.length){const tr=document.createElement("tr"),td=document.createElement("td");td.colSpan=6;td.textContent=state==="running"?"JEV 正在准备历史与首轮预测。":"尚无决策；点击启动开始监测。";tr.append(td);body.append(tr);}
    archive.update(data.task.task_id,data.archive_summary);
    for(const [id,format] of [["export-jsonl","jsonl"],["export-csv","csv"]])$(id).href=`/api/jev-tasks/${encodeURIComponent(data.task.task_id)}/archive/export?format=${format}`;
  }
  function showStep(value){
    form.elements.history_days.closest("label").hidden=form.elements.context_mode.value==="multiscale";
    step=value;for(const panel of document.querySelectorAll("[data-panel]"))panel.hidden=Number(panel.dataset.panel)!==step;
    for(const item of document.querySelectorAll("[data-step]"))item.classList.toggle("current",Number(item.dataset.step)===step);
    $("previous-step").hidden=step===0;$("next-step").hidden=step===2;$("create-task").hidden=step!==2;
    text("next-step",step===1?"确认风格，下一步":"下一步");
    text("create-task",form.elements.start.checked?"创建并启动":"创建，稍后启动");
    text("funds-title",form.elements.kind.value==="jev_advice"?"设置建议的参考虚拟钱包":"设置独立模拟钱包");
    text("creation-summary",`${form.elements.symbol.value} · ${form.elements.kind.value==="jev_advice"?"实时建议":"自动模拟操盘"} · ${form.elements.context_mode.value==="multiscale"?"六层输入":"原有小时历史"} · 风格 ${form.elements.style_strength.value} / 100`);
  }
  function newTask(){if(busy)return;wizard=true;current=null;pending=null;form.reset();contractSelector.reset();if(!contractSelector.contracts.length)contractSelector.load();text("style-value",50);$("workspace").hidden=true;$("wizard").hidden=false;showStep(0);sidebar();note("");}
  function validatePanel(){if(step===1&&!form.elements.symbol.value){note("请先获取合约目录并选择币种。",true);return false;}const panel=document.querySelector(`[data-panel="${step}"]`);for(const input of panel.querySelectorAll("input,textarea,select"))if(!input.checkValidity()){input.reportValidity();return false;}return true;}
  const choices=name=>{const values=form.elements[name].value.split(/[,，\s]+/).filter(Boolean).map(Number);if(!values.length||values.length>4||values.some(x=>!Number.isInteger(x)||x<1)||new Set(values).size!==values.length)throw new Error("比例与杠杆需填写最多 4 个不重复的正整数。");return values;};
  function payload(){
    const f=form.elements, leverageChoices=choices("leverage_choices"), ceiling=Number(f.max_leverage.value);
    if(leverageChoices.some(x=>x>ceiling))throw new Error("可选杠杆不能超过所设上限。");
    return {kind:f.kind.value,name:f.name.value,context_mode:f.context_mode.value,analysis_target:{market:"usdt_perpetual",symbol:f.symbol.value.trim().toUpperCase(),history_days:Number(f.history_days.value)},style_strength:Number(f.style_strength.value),style_confirmed:true,
      limits:{initial_usdt:f.initial_usdt.value,leverage:Math.min(...leverageChoices),max_leverage:ceiling,max_position_notional:f.max_position_notional.value,max_run_loss_usdt:f.max_run_loss_usdt.value,fee_bps:f.fee_bps.value,slippage_bps:f.slippage_bps.value},
      policy:{decision_mode:"parameterized",order_notional_usdt:f.max_position_notional.value,max_price_drift_bps:f.max_price_drift_bps.value,min_confidence:f.min_confidence.value,strategy_instructions:f.strategy_instructions.value,entry_margin_percents:choices("entry_margin_percents"),position_change_percents:choices("position_change_percents"),leverage_choices:leverageChoices},decision_interval_seconds:Number(f.decision_interval_seconds.value),start:f.start.checked,confirmed:true};
  }
  async function act(operation){
    if(busy||!current)return;const id=current.task.task_id, body={confirmed:true,expected_revision:current.account?.revision||0,session_revision:current.session.revision};busy=true;render(current);
    try{await api(`/api/jev-tasks/${encodeURIComponent(id)}/${operation}`,body);note(operation==="start"?"会话已启动。":operation==="pause"?"会话已暂停，已有持仓仍持续维护。":"会话已结束，档案保留。");await loadView();await loadList();}catch(error){await loadView();note(error.message.includes("market_unavailable")?JevDecisionMessages.market(current?.market_status):error.message,true);}finally{busy=false;if(current)render(current);}
  }
  async function loadChart(){
    if(!selected||wizard||chartLoading)return;if(current?.readonly){text("chart-message","已结束会话的行情证据保存在逐笔档案中。");return;}const id=selected, interval=$("chart-interval").value;chartLoading=true;
    try{const data=await api(`/api/jev-tasks/${encodeURIComponent(id)}/history?interval=${encodeURIComponent(interval)}&limit=200`);if(selected!==id||wizard||$("chart-interval").value!==interval)return;chartHistory=data.history;chartTask=id;text("chart-message",`${data.history.source==="fake"?"离线示例":"Binance"} · ${data.history.candles.length} 根已收盘 ${interval} K 线 · 更新 ${time(data.history.captured_at)}`);draw();}
    catch(error){if(selected===id&&!wizard){if(chartTask===id&&chartHistory?.interval===interval){text("chart-message",`${error.message} 保留 ${time(chartHistory.captured_at)} 已确认的 K 线。`);}else{chartHistory=null;draw();text("chart-message",error.message);}}}
    finally{chartLoading=false;if(!wizard&&(id!==selected||interval!==$("chart-interval").value))queueMicrotask(loadChart);}
  }
  function draw(){
    const canvas=$("price-chart"),ctx=canvas.getContext("2d"),width=Math.max(300,canvas.clientWidth),height=250,dpr=window.devicePixelRatio||1;canvas.width=width*dpr;canvas.height=height*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,width,height);
    if(!chartHistory||chartTask!==selected)return;
    const bars=chartHistory.candles.slice(-100),min=Math.min(...bars.map(b=>Number(b.low))),max=Math.max(...bars.map(b=>Number(b.high))),span=max-min||Math.max(max*.001,1),left=6,right=75,top=12,bottom=26,plot=width-left-right,bodyWidth=Math.max(1,Math.min(8,plot/bars.length*.65)),y=value=>top+(max-Number(value))/span*(height-top-bottom);
    ctx.font='11px "Segoe UI",sans-serif';for(let i=0;i<5;i++){const v=min+span*i/4,py=y(v);ctx.strokeStyle="#e1e5eb";ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(width-right,py);ctx.stroke();ctx.fillStyle="#65768b";ctx.fillText(num(v,6),width-right+8,py+4);}
    bars.forEach((b,i)=>{const x=left+(i+.5)*plot/bars.length,green=Number(b.close)>=Number(b.open);ctx.strokeStyle=ctx.fillStyle=green?"#25895a":"#b42318";ctx.beginPath();ctx.moveTo(x,y(b.high));ctx.lineTo(x,y(b.low));ctx.stroke();ctx.fillRect(x-bodyWidth/2,Math.min(y(b.open),y(b.close)),bodyWidth,Math.max(1,Math.abs(y(b.open)-y(b.close))));});
    ctx.fillStyle="#65768b";ctx.fillText(new Date(bars[0].opened_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}),left,height-4);
  }
  $("new-task").addEventListener("click",newTask);$("cancel-wizard").addEventListener("click",()=>{if(selected)select(selected);});
  $("previous-step").addEventListener("click",()=>showStep(step-1));$("next-step").addEventListener("click",()=>{if(validatePanel())showStep(step+1);});
  $("task-style").addEventListener("input",()=>text("style-value",form.elements.style_strength.value));
  form.elements.start.addEventListener("change",()=>text("create-task",form.elements.start.checked?"创建并启动":"创建，稍后启动"));
  form.elements.context_mode.addEventListener("change",()=>{form.elements.history_days.closest("label").hidden=form.elements.context_mode.value==="multiscale";});
  form.addEventListener("submit",async event=>{event.preventDefault();if(busy||step!==2||!validatePanel())return;
    try{const body=payload(),signature=JSON.stringify(body);if(!pending||pending.signature!==signature)pending={signature,body:{...body,request_id:crypto.randomUUID()}};busy=true;$("create-task").disabled=true;note("正在建立会话和读取公共行情…");const data=await api("/api/jev-tasks",pending.body);pending=null;busy=false;await loadList();await select(data.task.task_id);if(data.task.failure)note("会话已保存，首次启动未完成；可在此点击启动重试。",true);}
    catch(error){note(error.message,true);}finally{busy=false;$("create-task").disabled=false;}
  });
  $("start-task").addEventListener("click",()=>act("start"));$("pause-task").addEventListener("click",()=>act("pause"));$("close-task").addEventListener("click",()=>act("close"));
  $("decision-interval").addEventListener("change",()=>{$("decision-interval").dataset.dirty="true";});
  $("input-mode").addEventListener("change",()=>{$("input-mode").dataset.dirty="true";});
  $("save-input-mode").addEventListener("click",async()=>{
    if(busy||!current)return;const id=current.task.task_id,mode=$("input-mode").value;
    const body={confirmed:true,expected_revision:current.account?.revision||0,session_revision:current.session.revision,context_revision:current.task.context_revision||0,context_mode:mode};busy=true;render(current);
    try{const data=await api(`/api/jev-tasks/${encodeURIComponent(id)}/context`,body,"PUT");if(selected!==id)return;delete $("input-mode").dataset.dirty;current=data;render(data);note(`已保存${mode==="multiscale"?"六层输入":"原有小时历史"}；钱包和交易档案保留，点击恢复运行继续。`);}
    catch(error){note(error.message,true);await loadView();}finally{busy=false;if(current)render(current);}
  });
  $("save-cadence").addEventListener("click",async()=>{
    if(busy||!current)return;const id=current.task.task_id,seconds=Number($("decision-interval").value);
    const body={confirmed:true,expected_revision:current.account?.revision||0,session_revision:current.session.revision,cadence_revision:current.task.cadence_revision||0,decision_interval_seconds:seconds};busy=true;render(current);
    try{const data=await api(`/api/jev-tasks/${encodeURIComponent(id)}/cadence`,body,"PUT");if(selected!==id)return;delete $("decision-interval").dataset.dirty;current=data;render(data);note(`分析间隔已保存为 ${seconds} 秒；会话保持暂停，可点击恢复运行。`);}
    catch(error){note(error.message,true);await loadView();}finally{busy=false;if(current)render(current);}
  });
  $("refresh-task").addEventListener("click",()=>refresh());$("chart-interval").addEventListener("change",()=>{chartHistory=null;draw();loadChart();});window.addEventListener("resize",draw);
  async function refresh(){if(refreshBusy||busy)return;refreshBusy=true;try{await loadList();await loadView();}catch(error){note(error.message,true);}finally{refreshBusy=false;}}
  async function init(){try{await loadList();if(!tasks.some(t=>t.task_id===selected))selected=tasks.find(t=>t.status!=="closed")?.task_id||tasks[0]?.task_id;if(selected)await select(selected);else newTask();}catch(error){note(error.message,true);newTask();}
  }
  const contractSelector=new window.ContractSelector({select:$("symbol-input"),search:$("contract-search"),status:$("catalog-status"),retry:$("reload-contracts"),fetchCatalog:()=>api("/api/analysis/contracts")});
  contractSelector.load();
  init();setInterval(refresh,2000);setInterval(()=>{if(!wizard)loadChart();},30000);
})();
