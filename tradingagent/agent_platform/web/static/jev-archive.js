"use strict";
(()=>{
  const amount=value=>value===null||value===undefined?"未记录":String(value);
  const zero=value=>value!==null&&value!==undefined&&/^[+-]?0+(?:\.0+)?(?:e[+-]?\d+)?$/i.test(String(value));
  const time=value=>value?new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(value)):"未记录";
  const node=(tag,text,css)=>{const element=document.createElement(tag);if(text!==undefined)element.textContent=text;if(css)element.className=css;return element;};
  const intents={open_long:"开多",open_short:"开空",add_long:"加多",add_short:"加空",reduce:"减仓",close:"平仓"};
  function summaryText(summary){
    if(!summary)return "正在读取成交与资金流水…";
    const counts=summary.counts;
    return counts?`${counts.trade} 笔模拟成交 · ${counts.funding} 次资金费结算 · ${counts.liquidation} 次清算`:`${summary.total} 条成交与资金记录 · 分类待更新`;
  }
  function describe(entry){
    const record=entry.record,op=record.operation||{},before=record.before_state,state=record.state;
    const evidence=record.execution_command?.decision_evidence,plan=evidence?.plan;
    const isFunding=record.kind==="funding",empty=isFunding&&zero(op.funding_usdt)&&zero(before?.quantity??state.quantity);
    const kind=({trade:"模拟成交",funding:"资金费结算",liquidation:"强制清算"})[record.kind]||record.kind;
    const source=entry.origin==="legacy_import"?"旧记录":evidence?.decision_source==="real_jev"?"JEV":evidence?"离线验证":"系统资金事件";
    const action=empty?"空仓，未扣款":intents[plan?.intent]||(isFunding?"周期结算":record.kind==="liquidation"?"系统保护平仓":op.side==="buy"?"买入":op.side==="sell"?"卖出":"未记录操作");
    const quantity=isFunding?`${amount(before?.quantity??state.quantity)}（持仓）`:`${amount(op.quantity)}（成交）`;
    const leverage=state.settings?.leverage;
    const money=isFunding?`资金费 ${amount(op.funding_usdt)}`:`盈亏 ${amount(op.realized_pnl_usdt)} · 手续费 ${amount(op.fee_usdt)}`;
    return {record,op,before,state,evidence,plan,kind,source,action,quantity,money,
      leverage:leverage===undefined?"杠杆未记录":`${leverage}×`,
      price:isFunding?record.funding?.mark:op.price,
      occurred:isFunding?record.funding?.settled_at||op.occurred_at:op.occurred_at};
  }
  function details(entry,data){
    const {record,op,before,state,evidence,plan}=data,dl=node("dl",undefined,"archive-facts");
    const change=(old,value)=>`${amount(old)} → ${amount(value)}`;
    const values=[["币种",state.symbol],["记录类型",data.kind],["来源",data.source],["处理时间",time(op.occurred_at)],
      ["持仓数量",change(before?.quantity,state.quantity)],["杠杆",change(before?.settings?.leverage,state.settings?.leverage)],
      ["可用 USDT",change(before?.free_usdt,state.free_usdt)],["保证金 USDT",change(before?.margin_usdt,state.margin_usdt)],
      ["手续费 USDT",amount(op.fee_usdt)],["已实现盈亏 USDT",amount(op.realized_pnl_usdt)],
      ["资金费 USDT",amount(op.funding_usdt)],["操作编号",record.command_id],["内容 SHA-256",entry.content_hash]];
    if(record.funding)values.push(["资金费结算时点",time(record.funding.settled_at)],["资金费率",amount(record.funding.rate)]);
    if(evidence){
      const answer=evidence.response?.answers?.[0];
      values.push(["模型",evidence.response?.provider_metadata?.model_id||"未记录"],["请求编号",evidence.request?.request_id||"未记录"],
        ["置信度",amount(answer?.confidence)],["调用耗时",evidence.elapsed_ms==null?"未记录":`${evidence.elapsed_ms} ms`]);
      if(plan)values.push(["仓位计算基准",`${plan.sizing_basis==="equity_margin"?"账户权益保证金比例":plan.sizing_basis==="current_quantity"?"原持仓数量比例":plan.sizing_basis} · ${plan.percent}%`]);
    }else values.push(["模型下单证据",entry.origin==="legacy_import"?"旧记录未保存完整模型证据":"此记录为系统资金事件，没有 JEV 下单请求"]);
    for(const [title,value] of values)dl.append(node("dt",title),node("dd",value??"未记录"));
    return dl;
  }
  function entryRows(entry){
    const data=describe(entry),row=node("tr"),detailRow=node("tr",undefined,"archive-detail-row"),cell=node("td");
    detailRow.hidden=true;cell.colSpan=6;detailRow.append(cell);
    const panel=details(entry,data),raw=node("pre",undefined,"archive-raw");raw.hidden=true;cell.append(panel,raw);
    const fields=[`${time(data.occurred)} · #${entry.sequence}`,`${data.kind} · ${data.action}`,`${data.quantity} / ${data.leverage}`,
      data.price==null?"—":`${amount(data.price)}${data.record.kind==="funding"?"（标记）":""}`,data.money];
    for(const value of fields)row.append(node("td",value));
    const actions=node("td",undefined,"archive-row-actions"),detailButton=node("button","详情"),jsonButton=node("button","JSON");
    detailButton.type=jsonButton.type="button";
    let mode=null;
    function show(value){
      mode=mode===value?null:value;detailRow.hidden=mode===null;panel.hidden=mode!=="details";raw.hidden=mode!=="json";
      if(mode==="json")raw.textContent=JSON.stringify(entry,null,2);
      detailButton.textContent=mode==="details"?"收起详情":"详情";jsonButton.textContent=mode==="json"?"收起 JSON":"JSON";
    }
    detailButton.addEventListener("click",()=>show("details"));jsonButton.addEventListener("click",()=>show("json"));
    actions.append(detailButton,jsonButton);row.append(actions);return [row,detailRow];
  }
  function create({api,rows,info,status,raw,next,refresh,json}){
    let task=null,generation=0,page=null,loading=false;
    const empty=message=>{const row=node("tr"),cell=node("td",message);cell.colSpan=6;row.append(cell);rows.replaceChildren(row);};
    function reset(id){task=id;generation++;page=null;loading=false;raw.hidden=true;raw.textContent="";next.disabled=true;json.disabled=true;empty("正在读取成交与资金流水…");status.textContent="";json.textContent="JSON";}
    async function load(first=true){
      if(!task||loading||(!first&&page?.next_after==null))return;
      const own=generation,id=task;loading=true;next.disabled=true;refresh.disabled=true;
      const params=new URLSearchParams({limit:"25",after:String(first?0:page.next_after)});
      if(!first)params.set("through",String(page.through));
      try{
        const result=await api(`/api/jev-tasks/${encodeURIComponent(id)}/archive?${params}`);
        if(own!==generation||id!==task)return;
        page=result;rows.replaceChildren();raw.hidden=true;raw.textContent="";json.textContent="JSON";
        for(const entry of result.entries)rows.append(...entryRows(entry));
        if(!result.entries.length)empty("尚无模拟成交、资金费结算或清算记录。WAIT 决策不会生成成交档案。");
        status.textContent=`本页 ${result.entries.length} 条 · 本快照 ${result.total} 条 · 档案完整性校验通过`;
        json.disabled=false;
      }catch(error){if(own===generation){status.textContent=`档案读取失败：${error.message}`;if(!page)empty("档案暂不可读，请点击刷新档案重试。");}}
      finally{if(own===generation){loading=false;refresh.disabled=false;next.disabled=page?.next_after==null;}}
    }
    async function update(id,value){
      if(id!==task)reset(id);info.textContent=summaryText(value);
      if(!page&&!loading&&value!==null&&value!==undefined)return load();
      if(page&&value&&value.total!==page.total)status.textContent=`当前 ${value.total} 条 · 本快照 ${page.total} 条 · 点击刷新档案查看新增记录`;
    }
    refresh.addEventListener("click",()=>load());next.addEventListener("click",()=>load(false));
    json.addEventListener("click",()=>{if(!page)return;raw.hidden=!raw.hidden;if(!raw.hidden)raw.textContent=JSON.stringify(page,null,2);json.textContent=raw.hidden?"JSON":"收起 JSON";});
    return {reset,update};
  }
  window.JevArchive={summaryText,describe,create};
})();
