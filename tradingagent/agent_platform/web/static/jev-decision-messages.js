"use strict";
window.JevDecisionMessages = (() => {
  const stages={question_set:"问题集合",answer_type:"答案类型",criteria:"选项集合",legend:"评分说明",answer_values:"答案数值",binding:"请求身份",usage:"费用证据",metadata:"模型身份",transport:"网络传输"};
  function diagnosticFor(reason,cycles){
    const actual=reason==="provider_timeout_retry"?"provider_timeout":reason;
    return (cycles||[]).find(c=>c.reason===actual)?.diagnostic;
  }
  function failure(reason,diagnostic,recovery){
    if(reason==="provider_timeout"){
      const phases={connect:"建立网络连接",proxy_connect:"连接代理",tls:"建立加密连接",send:"发送模型请求",response_wait:"等待模型响应",body:"读取模型响应"};
      const phase=phases[diagnostic?.transport_evidence?.failed_phase];
      const detail=`JEV 模型连接（OpenRouter）：${phase?phase+"超时":"请求等待超时"}`;
      if(recovery?.retrying)return `${detail}，本轮未成交；冷却后使用新行情继续预测，原费用预留已保留。`;
      if(recovery?.consecutive_timeouts>=3)return `${detail}，连续 ${recovery.consecutive_timeouts} 次超时，已暂停后续模型调用。`;
      if(recovery?.active)return `${detail}，本轮未成交；后续预测已继续，原费用预留已保留。`;
      return `${detail}，已暂停后续模型调用；可查看下方耗时记录后恢复。`;
    }
    if(reason==="decision_expired")return `决策已过期，本次未成交；${diagnostic?.local_timing?"费用记录已保留，":""}下一轮继续。`;
    if(reason!=="invalid_model_assessment")return null;
    if(diagnostic?.issue==="probability_sum")return `JEV 概率合计为 ${diagnostic.probability_total}，偏差超过 1%；本次结果未采用，已暂停。`;
    const stage=stages[diagnostic?.stage];
    return `JEV ${stage||"决策"}未通过校验，已暂停；这笔结果未成交。`;
  }
  function result(cycle){
    const failed=failure(cycle.reason,cycle.diagnostic);
    if(failed)return failed;
    const labels={wait:"等待",advised:"已保存建议",filled:"已成交",rejected:"已拒绝",pending:"处理中",unknown:"待核实",interrupted:"已中断"};
    const base=cycle.reason||labels[cycle.status]||cycle.status;
    const adjustments=cycle.choice_probability_adjustments||[];
    return base+(adjustments.length?` · 概率合计 ${adjustments.map(a=>a.original_total).join(" / ")} 已归一化，原值已存档`:"");
  }
  function market(status){
    if(status?.clock?.ready===false)return "自动校时暂未通过精度校验，系统正在重试；恢复前不会启动收费预测。";
    if(status?.failure==="futures_stream_future")return `本机时钟偏差：行情事件领先约 ${status.event_ahead_ms??"—"} 毫秒；等待自动校时后重试。`;
    if(status?.connected?.book&&status?.connected?.mark&&status.recent_quote_count>0&&!status.failure)return "公共行情已恢复，可点击恢复运行；本次未自动启动。";
    if(status?.failure==="futures_stream_disconnected"||status?.connected?.book===false||status?.connected?.mark===false)return "公共行情连接中断，系统正在重连；连通后可重试启动。";
    return "公共报价尚未通过时效或结构校验，系统正在等待完整行情；本轮未调用模型。";
  }
  function clock(status){
    if(!status)return "";
    return status.ready?`自动校时 · 本机偏差 ${status.offset_ms} ms · 误差估计 ≤ ${status.uncertainty_ms} ms`:"自动校时未就绪 · 收费预测受阻";
  }
  return {failure,result,market,clock,diagnosticFor};
})();
