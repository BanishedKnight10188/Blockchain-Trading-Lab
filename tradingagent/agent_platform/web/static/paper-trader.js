"use strict";
(()=>{
  const W=window.Workbench, config=document.getElementById("paper-configure"), action=document.getElementById("paper-action"), configFields=document.getElementById("paper-config-fields"), actionFields=document.getElementById("paper-action-fields"), refresh=document.getElementById("refresh-page");
  let revision=null,generation=0,saving=false,configured=false,enabled=false,scope=null,compatible=true;
  const status={pending:"模型请求中",filled:"已模拟成交",wait:"等待",rejected:"纪律拒绝",unavailable:"判断不可用",discarded:"旧结果作废"};
  function render(data){
    enabled=data.enabled;configured=Boolean(data.account);revision=data.account?.revision??null;
    const next={session_id:data.session?.session_id,style_revision:data.session?.style_revision,account_ref:data.account?.account_ref,activation_revision:data.account?.activation_revision};
    if(scope&&JSON.stringify(scope)!==JSON.stringify(next)){config.elements.confirmed.checked=false;action.elements.confirmed.checked=false;W.message("会话、风格或运行身份已变化，输入已保留，请核对当前身份并重新确认。");config.dispatchEvent(new Event("change"));action.dispatchEvent(new Event("change"));}
    const target=data.analysis_target, spot=target?.market==="spot", futures=target?.market==="usdt_perpetual";
    scope=next;compatible=spot&&data.paper_market==="spot"&&data.source_compatible!==false&&data.market_compatible!==false;
    document.getElementById("spot-paper-controls").hidden=!spot;
    document.getElementById("spot-paper-history").hidden=!spot;
    W.set("paper-market",target?`${target.symbol} · ${futures?"USDT 永续合约":spot?"现货":"市场未确认"}`:"尚未选择交易市场，请先创建会话。");
    W.set("paper-source",!enabled?"Paper 尚未装配。启动服务时明确选择 Mock 演示或真实 JEV 配置。":`${data.decision_source==="real_jev"?"真实 JEV · 调用计费":"Mock 脚本判断 · 模型费为零"} / ${data.market_source==="binance_public"?"Binance 公共行情":"离线演示报价 · 非真实市场"}`);
    const a=data.account;
    if(spot&&data.source_compatible===false)W.set("paper-source",`历史现货钱包来源：${data.decision_source} / ${data.market_source}。与当前进程来源不同，启动已禁用；请结束旧会话并确认新会话。`);
    if(futures)W.set("paper-source","USDT 永续合约的配置、保证金、持仓和成交见下方合约操盘区。");
    if(spot&&enabled&&compatible)W.set("paper-source",`${document.getElementById("paper-source").textContent} · BTCUSDT 现货模拟，不包含合约杠杆。`);
    W.facts("paper-wallet",a?[["运行状态",W.label(a.status)],["虚拟 USDT",a.usdt],["虚拟 BTC",a.btc],["虚拟权益 USDT",data.equity_usdt??"报价不可用"],["账户版本",a.revision],["在途请求",a.pending_request_id?"一个模型请求在途":"无"],["真实订单","关闭"],["重启行为","保留资金并暂停"]]:[["虚拟账户","尚未建立"],["真实订单","关闭"]]);
    W.rows("paper-cycles",(data.cycles||[]).map(c=>[W.time(c.created_at),`${c.decision??"不可评估"} / ${c.confidence??"—"}`,`${status[c.status]||c.status}${c.reason?" · "+c.reason:""} · ${((new Date(c.updated_at)-new Date(c.created_at))/1000).toFixed(2)}s`,c.fill?`${c.fill.side} ${c.fill.quantity} BTC @ ${c.fill.price}; 手续费 ${c.fill.fee} USDT`:"—",c.usage?`${c.usage.actual_cost_usd??c.usage.estimated_cost_usd} (${c.usage.billing_status})`:"未取得"]),5,"尚无决策记录");
    configFields.disabled=!enabled||configured||!scope.session_id||!compatible;actionFields.disabled=!enabled||!configured||!compatible;
  }
  async function load(){if(saving)return;const own=++generation;try{const data=await W.api("/api/paper");if(own===generation&&!saving)render(data);}catch(error){if(own===generation&&!saving){compatible=false;configFields.disabled=true;actionFields.disabled=true;config.elements.confirmed.checked=false;action.elements.confirmed.checked=false;document.getElementById("spot-paper-controls").hidden=true;document.getElementById("spot-paper-history").hidden=true;W.set("paper-market","无法确认当前交易市场，请重新载入。");}throw error;}}
  async function write(form,path,body){if(saving)return;saving=true;++generation;configFields.disabled=true;actionFields.disabled=true;
    try{render(await W.write(form,path,body));}finally{saving=false;configFields.disabled=!enabled||configured||!scope?.session_id||!compatible;actionFields.disabled=!enabled||!configured||!compatible;}}
  W.bind(config,async()=>{if(!enabled||!compatible||!scope?.session_id)return;const settings={};for(const key of ["initial_usdt","order_quantity","max_position_quantity","max_run_loss_usdt","fee_bps","slippage_bps","max_price_drift_bps","min_confidence","strategy_instructions"])settings[key]=config.elements[key].value;await write(config,"/api/paper/configure",{settings,session_id:scope.session_id,style_revision:scope.style_revision});});
  W.bind(action,async()=>{if(revision===null||!compatible)return;await write(action,"/api/paper/"+action.elements.action.value,{expected_revision:revision,account_ref:scope.account_ref,style_revision:scope.style_revision,activation_revision:scope.activation_revision});});
  refresh.addEventListener("click",W.load(load));W.load(load)();
  const poll=window.setInterval(W.load(load),2000);window.addEventListener("pagehide",()=>window.clearInterval(poll),{once:true});
})();
