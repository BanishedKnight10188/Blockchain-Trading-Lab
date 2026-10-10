"use strict";
(()=>{
  const W=window.Workbench, config=document.getElementById("futures-configure"), action=document.getElementById("futures-action"), cf=document.getElementById("futures-config-fields"), af=document.getElementById("futures-action-fields");
  let state=null,saving=false,generation=0,identity=null;
  function failure(reason){
    if(reason==="model_read_only")return "只读模式禁止新模型决策；已有持仓继续维护。";
    if(reason==="prediction_tick_skipped")return "准备阶段延迟导致发起时点拥挤，本轮跳过，不收费或补发。";
    if(reason==="prediction_superseded")return "较新的有效预测已采用，本轮旧结果不执行。";
    if(reason==="decision_expired")return "结果超过预测有效期，不执行。";
    if(reason==="account_changed")return "预测期间持仓或账户状态已变化，本轮不执行。";
    if(reason==="invalid_model_metadata")return "返回的模型身份未通过校验；已暂停新决策，费用证据保留。";
    const http=/^provider_http_([1-5][0-9]{2})$/.exec(reason||"");
    if(http)return `模型接口 HTTP ${http[1]}；已暂停新决策，本轮费用预留保留。`;
    return ({provider_access_denied:"模型服务拒绝访问（403）；已暂停新决策，请检查地区可用性或账户权限。",provider_error:"模型接口失败；已暂停新决策，费用预留保留。",provider_transport_error:"模型网络传输失败；已暂停新决策，费用预留保留。",provider_timeout:"模型请求超时；已暂停新决策，费用预留保留。",invalid_model_assessment:"模型决策未通过结构、身份或有效期校验；已暂停新决策，请核对该笔已知费用。",invalid_model_response:"模型响应未通过校验；已暂停新决策，费用预留保留。",invalid_model_usage:"模型费用证据未通过校验；已暂停新决策，费用预留保留。",model_budget_exhausted:"模型费用余额不足，已暂停新决策。",model_billing_frozen:"模型费用待核对，已暂停新决策。",model_hourly_limit:"模型每小时调用上限已到，已暂停新决策。",model_policy_expired:"模型费用授权已到期，已暂停新决策。",market_unavailable:"当前报价未通过校验，本轮未调用模型。",history_unavailable:"历史行情暂不可用，本轮未调用模型。",maintenance_unavailable:"持仓维护缺少有效行情，等待恢复。"})[reason]||reason;
  }
  function render(data){
    state=data;
    document.dispatchEvent(new CustomEvent("futures-trading-state",{detail:{account:data.account?.scope?.account_ref,summary:data.archive_summary,market:data.analysis_target?.market}}));
    const selected=data.analysis_target?.market==="usdt_perpetual";
    document.getElementById("futures-controls").hidden=!selected;
    document.getElementById("futures-history").hidden=!selected;
    const sessionNotice=document.getElementById("futures-session-status");
    if(sessionNotice){sessionNotice.hidden=!selected||data.session?.status==="running";W.set("futures-session-message",data.session?.status==="paused"?"当前合约会话已暂停，请先在会话页点击“恢复合约会话”，再启动操盘。":"当前合约会话已配置但尚未启动，请先在会话页点击“启动合约会话”，再启动操盘。");}
    const next=JSON.stringify([data.session?.session_id,data.session?.style_revision,data.trader_revision,data.account?.scope?.account_ref]);
    if(identity!==null&&identity!==next){config.elements.confirmed.checked=false;action.elements.confirmed.checked=false;config.dispatchEvent(new Event("change"));action.dispatchEvent(new Event("change"));}
    identity=next;
    if(data.account){for(const [k,v] of Object.entries({...data.limits,...data.policy})){if(config.elements[k])config.elements[k].value=String(k==="max_leverage"?(v??data.limits.leverage):v);}}
    W.set("futures-source",!data.enabled?"合约后台未装配。使用 --paper --paper-mock 接公共行情验证框架，或配置有效的真实 JEV 试跑。":`${data.analysis_target?.symbol} · ${data.market_source==="binance_futures_public"?"Binance 公共合约行情":"离线演示报价"} · ${data.decision_source==="real_jev"?(data.model_read_only?"JEV 只读：模型调用关闭，账本和持仓维护可用":"真实 JEV / 调用计费"):"离线 Mock / 默认 WAIT / 无模型费用"}`);
    const a=data.account, q=data.quote?.quote;
    const b=data.model_budget;
    const cadence=data.cadence, metrics=data.prediction_metrics||{}, runtime=data.runtime_metrics||{};
    W.facts("futures-cadence",cadence?[["目标预测节奏",`每 ${cadence.decision_seconds} 秒发起一次`],["本次进程模型请求数",metrics.requests_started??0],["当前并行预测",`${runtime.in_flight??0} / ${cadence.max_predictions}`],["最近模型耗时",metrics.last_model_ms==null?"尚未请求":`${metrics.last_model_ms} ms`],["本次完成轮次",runtime.completed_cycles??0],["满载跳过时点",runtime.skipped_capacity??0],["预测有效期",`${cadence.prediction_ttl_seconds} 秒`],["每小时请求上限",cadence.hourly_call_limit]]:[]);
    const stream=data.market_status;
    W.set("futures-market-status",stream?`行情：实时 WebSocket · 买卖价${stream.connected.book?"已连接":"连接中"} / 标记价${stream.connected.mark?"已连接":"连接中"} · ${stream.failure==="futures_stream_future"?`事件领先本机约 ${stream.event_ahead_ms??"未知"} ms，请同步系统时间`:stream.failure?"等待有效行情":`最近 ${stream.recent_quote_count} 个真实报价样本`}`:"行情：离线回放");
    W.facts("futures-budget",b?.provider_managed?[["模型费用上限","OpenRouter API Key 网站限额"],["费用记录","在工作台各会话下查看已消耗与待核实费用"],["费用状态",b.read_only?"只读，禁止模型调用":b.active?"已启用":"未启用"]]:b?[[b.budget_scope==="cumulative"?"累计模型费用上限 USD":"模型费用上限 USD",b.balance.daily_limit_usd],["已确认费用 USD",b.balance.spent_usd],["待核对预留 USD",b.balance.reserved_usd],["单次上限 USD",b.single_call_usd],["有效期",b.expires_at?W.time(b.expires_at):"长期有效，不设到期时间"],["费用状态",b.read_only?"只读，禁止模型调用":b.balance.billing_frozen?"冻结，等待核对":b.active?"有效":"未生效或已到期"]]:[["模型费用",data.decision_source==="real_jev"?"状态暂不可用":"Mock 不产生模型费用"]]);
    const dynamic=data.policy?.decision_mode==="parameterized";
    W.facts("futures-wallet",[["运行状态",a?W.label(a.status):"未建立"],["决策方式",dynamic?"JEV选择比例与杠杆":"固定金额 / 固定杠杆"],["当前杠杆",dynamic&&a&&Number(a.quantity)===0?"空仓，开仓时由 JEV 选择":a?.leverage!=null?`${a.leverage}倍`:"—"],["杠杆候选",dynamic?data.policy.leverage_choices.map(v=>`${v}倍`).join(" / "):"—"],["杠杆上限",data.limits?`${data.limits.max_leverage??data.limits.leverage}倍`:"—"],["可用 USDT",a?.free_usdt],["逐仓保证金 USDT",a?.margin_usdt],["持仓",a?`${a.side||"空仓"} / ${a.quantity} ${data.analysis_target.symbol}`:"—"],["权益 USDT",a?.equity_usdt??"报价不可用"],["未实现盈亏 USDT",a?.unrealized_pnl_usdt],["已结算资金费 USDT",a?.funding_usdt],["累计手续费 USDT",a?.fees_usdt],["标记价",q?`${q.mark} · ${W.time(q.mark_at)}`:"未取得"],["维护状态",failure(data.maintenance_failure||data.worker_failure)||"正常"],["真实订单","关闭"]]);
    const planText=p=>!p?"":p.intent==="wait"?"保持仓位与杠杆":`${({open_long:"开多",open_short:"开空",add_long:"加多",add_short:"加空",reduce:"减仓",close:"全平"})[p.intent]} ${p.percent}%（${p.sizing_basis==="equity_margin"?"净权益保证金占比":"当前数量"}） / 数量 ${p.quantity} / ${p.leverage}倍`;
    const diagnosticText=d=>!d?"":`；校验阶段：${({transport:"网络传输",usage:"费用",metadata:"模型身份",question_set:"问题集",answer_type:"答案类型",criteria:"候选集合",legend:"评分说明",answer_values:"答案数值 / 概率",binding:"请求绑定"})[d.stage]||"未知"}${d.question_index!=null?`，第${d.question_index+1}题`:""}${d.expected_count!=null?`，预期${d.expected_count}项 / 返回${d.actual_count??"未知"}项`:""}`;
    W.rows("futures-cycles",(data.cycles||[]).map(c=>[W.time(c.created_at),`${planText(c.plan)||c.decision||"—"} / ${c.confidence??"—"}`,`${W.label(c.status)}${c.reason?" · "+failure(c.reason):""}${diagnosticText(c.diagnostic)}`,c.command_id?`${c.command_id} / ${c.execution?.status||"结果待确认"}`:"—",c.usage?`${c.usage.actual_cost_usd??c.usage.estimated_cost_usd} (${c.usage.billing_status})`:"未调用"]),5,"尚无合约决策");
    W.rows("futures-operations",(data.operations||[]).map(r=>[W.time(r.operation?.occurred_at||r.state.updated_at),r.kind,r.operation?`${r.operation.quantity} @ ${r.operation.price??"—"}`:"—",r.operation?`${r.operation.fee_usdt} / ${r.operation.funding_usdt}`:"—"]),4,"尚无资金操作");
    cf.disabled=saving||!selected||!data.enabled||Boolean(a)||!data.session||data.source_compatible===false;
    af.disabled=saving||!selected||!data.enabled||!a||data.source_compatible===false;
    const startBlocked=Boolean(data.session?.status!=="running"||data.model_read_only||(data.decision_source==="real_jev"&&b&&!b.active));
    action.elements.action.querySelector('option[value="start"]').disabled=startBlocked;
    if(startBlocked&&action.elements.action.value==="start"){action.elements.action.value="pause";action.elements.confirmed.checked=false;action.dispatchEvent(new Event("change"));}
  }
  async function load(){if(saving)return;const own=++generation;try{const data=await W.api("/api/futures-trading");if(own===generation&&!saving)render(data);}catch(e){if(own===generation){cf.disabled=true;af.disabled=true;document.getElementById("futures-controls").hidden=true;document.getElementById("futures-history").hidden=true;}throw e;}}
  async function write(form,path,body){saving=true;++generation;cf.disabled=true;af.disabled=true;try{render(await W.write(form,path,body));}finally{saving=false;if(state)render(state);}}
  W.bind(config,async()=>{if(!state?.session||!state.enabled)return;const limits={},policy={};for(const k of ["initial_usdt","max_position_notional","max_run_loss_usdt","fee_bps","slippage_bps"])limits[k]=config.elements[k].value;for(const k of ["leverage","max_leverage"])limits[k]=Number(config.elements[k].value);for(const k of ["order_notional_usdt","max_price_drift_bps","min_confidence","strategy_instructions","decision_mode"])policy[k]=config.elements[k].value;for(const k of ["entry_margin_percents","position_change_percents","leverage_choices"])policy[k]=config.elements[k].value.split(",").map(v=>Number(v.trim()));await write(config,"/api/futures-trading/configure",{limits,policy,session_id:state.session.session_id,style_revision:state.session.style_revision});});
  W.bind(action,async()=>{if(!state?.account||!state.session)return;await write(action,"/api/futures-trading/"+action.elements.action.value,{account_ref:state.account.scope.account_ref,expected_revision:state.account.revision,style_revision:state.session.style_revision,trader_revision:state.trader_revision});});
  document.getElementById("refresh-page").addEventListener("click",W.load(load));W.load(load)();
  const poll=setInterval(W.load(load),2000);window.addEventListener("pagehide",()=>clearInterval(poll),{once:true});
})();
