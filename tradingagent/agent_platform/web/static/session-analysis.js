"use strict";
(() => {
  const panel = document.getElementById("initial-analysis-panel");
  const legacy = document.getElementById("legacy-spot-overview");
  const status = document.getElementById("initial-status");
  const chart = document.getElementById("history-chart");
  const interval = document.getElementById("chart-interval");
  const count = document.getElementById("chart-count");
  let busy = false, stopped = false;
  let chartScope = null, chartVersion = 0, chartNextRead = 0, chartBusy = false;
  let chartAbort = null;
  let chartLatest = null;
  const reasons = {
    pending:"后台正在准备所选合约的已收盘历史；首次分析独立运行。",
    model_unconfigured:"历史已准备。首次 Haiku 的收费配置尚未装配，因此尚未调用模型。",
    complete:"首次历史分析已完成；结果仅针对显示的历史窗口。",
    history_unavailable:"公共历史读取失败或窗口不完整，未调用模型。后台每隔至少 60 秒重试历史请求。",
    model_failed:"首次分析失败或回答未通过校验；不会自动重试收费调用。",
    discarded:"会话、风格或截止时间已变化，原回答不再作为当前分析。",
    interrupted:"上次任务被中断；为避免重复收费，不自动重发。",
    style_changed:"风格已修改；原首次分析保留在记录中，当前不展示旧观点。",
    public_history_disabled:"合约公共历史未启用，请使用 --live-public 启动服务。",
    paused:"会话已暂停；不派发首次分析，也不展示先前观点。",
  };
  const time = value => new Date(value).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",hour12:false});
  function draw(history) {
    chart.replaceChildren();
    if (!history) return false;
    const bars = history.candles.map(c => ({
      ...c, open:Number(c.open), high:Number(c.high), low:Number(c.low), close:Number(c.close),
    }));
    if (!bars.length || bars.some(c =>
      [c.open,c.high,c.low,c.close].some(p => !Number.isFinite(p) || p <= 0)
      || c.low > Math.min(c.open,c.close) || c.high < Math.max(c.open,c.close))) return false;
    const low = Math.min(...bars.map(c => c.low)), high = Math.max(...bars.map(c => c.high));
    const span = high - low, slot = 690 / bars.length;
    const y = p => span ? 155-(p-low)*125/span : 95;
    bars.forEach((c,i) => {
      const x = 40+(i+0.5)*slot, width = Math.min(8, Math.max(0.6,slot*0.65));
      const color = c.close >= c.open ? "#1f805f" : "#c75045";
      const wick = document.createElementNS("http://www.w3.org/2000/svg", "path");
      wick.setAttribute("d", `M${x.toFixed(2)},${y(c.high).toFixed(2)} L${x.toFixed(2)},${y(c.low).toFixed(2)}`);
      wick.setAttribute("stroke",color); wick.setAttribute("stroke-width","0.8");
      const body = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      body.setAttribute("x",(x-width/2).toFixed(2));
      body.setAttribute("y",Math.min(y(c.open),y(c.close)).toFixed(2));
      body.setAttribute("width",width.toFixed(2));
      body.setAttribute("height",Math.max(1,Math.abs(y(c.open)-y(c.close))).toFixed(2));
      body.setAttribute("fill",color);
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${time(c.opened_at)} · 开 ${c.open} · 高 ${c.high} · 低 ${c.low} · 收 ${c.close} USDT`;
      body.append(title); chart.append(wick,body);
    });
    for (const [y,p] of [[22,high],[179,low]]) {
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x","40"); label.setAttribute("y",String(y));
      label.textContent = `${p.toLocaleString("zh-CN",{maximumFractionDigits:8})} USDT`;
      chart.append(label);
    }
    return true;
  }
  function resetChart() {
    chartScope=null; chartVersion++; chartAbort?.abort(); chartBusy=false;
    chartLatest=null;
    chart.replaceChildren();
    document.getElementById("chart-history-note").textContent="";
  }
  async function loadChart(force=false) {
    if (!chartScope || stopped || (!force && (chartBusy || Date.now()<chartNextRead || document.visibilityState==="hidden"))) return;
    const version=++chartVersion, scope=chartScope, selectedInterval=interval.value;
    const selectedCount=Number(count.value);
    const hasPrevious=chartLatest?.scope===scope && chartLatest.interval===selectedInterval
      && chartLatest.count===selectedCount;
    chartAbort?.abort(); chartAbort=new AbortController(); chartBusy=true;
    // Clear the old timeframe immediately; a late response cannot relabel it.
    if (force && !hasPrevious) {
      chart.replaceChildren();
      chartLatest=null;
      document.getElementById("chart-history-note").textContent="";
      document.getElementById("captured-at").textContent="正在读取图表";
    }
    const chartStatus=document.getElementById("chart-status");
    chartStatus.textContent="正在读取所选周期的公共 K 线…";
    document.getElementById("chart-target").textContent=`${scope.symbol} · USDT 永续 · ${selectedInterval}`;
    try {
      const response=await fetch(`/api/analysis/chart?interval=${encodeURIComponent(selectedInterval)}&limit=${selectedCount}`,
        {credentials:"same-origin",signal:chartAbort.signal});
      if (!response.ok) throw new Error("chart_unavailable");
      const data=await response.json();
      if (version!==chartVersion || scope!==chartScope) return;
      const history=data.history;
      if (data.session_id!==scope.sessionId || history?.market!=="usdt_perpetual"
        || history.symbol!==scope.symbol || history.interval!==selectedInterval
        || !Array.isArray(history.candles) || !history.candles.length
        || history.candles.length>selectedCount) throw new Error("chart_scope_changed");
      if (!draw(history)) throw new Error("invalid_chart_prices");
      chartLatest={scope,interval:selectedInterval,count:selectedCount,history};
      chartStatus.textContent="所选周期已载入 · 仅已收盘 K 线 · 每30秒检查更新";
      const source=history.source==="fake" ? "离线 Fake，不是真实行情" : "Binance 合约公共数据";
      document.getElementById("chart-history-note").textContent=`${source} · ${history.candles.length} 根 · ${time(history.candles[0].opened_at)} 至 ${time(history.candles.at(-1).closed_at)} · 价格 USDT`;
      document.getElementById("captured-at").textContent=`图表采集 ${time(history.captured_at)}`;
      chartNextRead=Date.now()+30000;
    } catch (error) {
      if (version!==chartVersion || scope!==chartScope) return;
      if (hasPrevious && chartLatest && error.message!=="chart_scope_changed" && error.message!=="invalid_chart_prices") {
        chartStatus.textContent="更新失败，保留上次确认的 K 线；60秒后重试。";
        document.getElementById("captured-at").textContent=`上次图表采集 ${time(chartLatest.history.captured_at)}（本次更新失败）`;
      } else {
        chart.replaceChildren(); chartLatest=null;
        chartStatus.textContent="所选周期暂不可读，60秒后重试；也可点击重新载入。";
        document.getElementById("chart-history-note").textContent="尚无可确认的所选周期 K 线。";
        document.getElementById("captured-at").textContent="图表读取失败";
      }
      chartNextRead=Date.now()+60000;
    } finally { if (version===chartVersion) chartBusy=false; }
  }
  async function loadAnalysis() {
    if (busy || stopped) return;
    busy = true;
    try {
      const response = await fetch("/api/analysis/current", {credentials:"same-origin"});
      if (!response.ok) throw new Error("analysis_unavailable");
      const view = await response.json();
      document.getElementById("page-message").textContent="";
      const futures = view.target?.market === "usdt_perpetual";
      panel.hidden = !futures; legacy.hidden = futures;
      window.setAnalysisMarket(futures ? "usdt_perpetual" : "spot");
      document.getElementById("workbench-title").textContent = futures ? `${view.target.symbol} · USDT 永续` : "BTCUSDT 现货工作台";
      if (!futures) { resetChart(); return; }
      document.getElementById("mode-note").textContent = "USDT 永续历史分析；现货账户与 BTC Paper 已隔离。";
      document.getElementById("initial-target").textContent = `首次 Haiku 历史：${view.target.symbol} · 近 ${view.target.history_days} 天 · 1 小时`;
      if (chartScope?.sessionId!==view.session_id || chartScope?.symbol!==view.target.symbol) {
        resetChart(); chartScope={sessionId:view.session_id,symbol:view.target.symbol};
        chartNextRead=0; loadChart(true);
      } else { loadChart(); }
      status.textContent = reasons[view.reason] || "首次分析状态暂不可评估。";
      if (view.reason === "history_unavailable" && view.history_retry_at) {
        status.textContent += ` 下次尝试不早于 ${time(view.history_retry_at)}。`;
      }
      const history = view.record?.history;
      document.getElementById("initial-history-note").textContent = history
        ? `${history.source === "fake" ? "离线 Fake，不是真实行情" : "Binance 合约公共数据"} · ${history.candles.length} 根 · ${time(history.requested_start)} 至 ${time(history.requested_end)}（不含结束时刻） · USDT · 风格 ${view.record.style.strength}/100 v${view.record.style_revision}`
        : view.reason === "history_unavailable"
          ? "历史数据暂不可用，正在等待公共接口重试；恢复后自动显示 K 线。"
          : "当前尚无完整历史 K 线，请查看上方任务状态。";
      const result = view.current_result;
      document.getElementById("initial-summary").textContent = result ? `${result.source === "fake" ? "离线 Fake 结果：" : `${result.model_id}：`}${result.summary}` : "";
      const details = document.getElementById("initial-details"); details.replaceChildren();
      if (result) for (const [name,items] of [["依据",result.evidence],["风险",result.risks],["观察条件",result.watch_conditions]]) {
        for (const item of items) { const row=document.createElement("li"); row.textContent=`${name}：${item}`; details.append(row); }
      }
    } catch {
      // Keep both market projections hidden until identity is known.
      panel.hidden=true; legacy.hidden=true;
      resetChart();
      window.setAnalysisMarket("unknown");
      document.getElementById("page-message").textContent="无法确认当前分析对象，请检查服务后重新载入。";
    } finally { busy=false; }
  }
  interval.addEventListener("change",()=>loadChart(true));
  count.addEventListener("change",()=>loadChart(true));
  document.getElementById("refresh-overview").addEventListener("click",()=>{loadAnalysis();loadChart(true);});
  window.addEventListener("pagehide",()=>{stopped=true;chartAbort?.abort();});
  setInterval(loadAnalysis,5000);
  loadAnalysis();
})();
