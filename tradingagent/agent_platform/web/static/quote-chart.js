"use strict";
(() => {
  function midpointText(bid, ask) {
    const decimal = value => {
      const raw = String(value), match = /^(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(raw);
      if (!match || raw.length > 160) return null;
      const fraction = match[2] || "", exponent = Number(match[3] || 0);
      if (!Number.isInteger(exponent) || Math.abs(exponent) > 128) return null;
      const scale = fraction.length - exponent;
      return {amount:BigInt(match[1] + fraction + "0".repeat(Math.max(0, -scale))), scale:Math.max(0, scale)};
    };
    const left = decimal(bid), right = decimal(ask);
    if (!left || !right) return null;
    const scale = Math.max(left.scale, right.scale);
    const sum = left.amount * 10n ** BigInt(scale - left.scale) + right.amount * 10n ** BigInt(scale - right.scale);
    const digits = (sum * 5n).toString().padStart(scale + 2, "0");
    return (digits.slice(0, -scale - 1) + "." + digits.slice(-scale - 1)).replace(/0+$/, "").replace(/\.$/, "");
  }
  class QuoteSeries {
    constructor() { this.points = []; this.owner = null; this.kind = null; this.gap = true; }
    observe(frame) {
      const market = frame.market, owner = frame.mode + ":" + market.source;
      if (owner !== this.owner) { this.points = []; this.gap = true; this.owner = owner; }
      const generated = Date.parse(frame.generated_at), bookAt = Date.parse(market.book_at);
      const bid = Number(market.bid), ask = Number(market.ask);
      const bookReady = market.book_status === "ready" && market.bid != null && market.ask != null
        && Number.isFinite(bid) && Number.isFinite(ask) && bid > 0 && ask >= bid
        && Number.isFinite(bookAt) && bookAt <= generated && generated - bookAt <= 5000;
      // Derived book midpoint is display-only, never a trade price or execution input.
      const kind = bookReady ? "book_midpoint" : "trade";
      const at = bookReady ? bookAt : Date.parse(market.quote_at);
      const priceText = bookReady ? midpointText(market.bid, market.ask) : market.price;
      const price = Number(priceText);
      const allowed = bookReady ? ["ready", "warming", "gap"] : ["ready", "warming"];
      if (frame.mode === "disabled" || !allowed.includes(market.status)
          || (!bookReady && market.price == null) || !Number.isFinite(price) || price <= 0
          || !Number.isFinite(at) || !Number.isFinite(generated) || at > generated || generated - at > 5000) {
        this.gap = true; return;
      }
      if (kind !== this.kind) { this.points = []; this.gap = true; this.kind = kind; }
      const previous = this.points.at(-1);
      if (previous && at <= previous.at) return;
      this.points.push({at, kind, price:priceText, value:price,
        breakBefore:this.gap || market.status === "gap" || !previous || at - previous.at > 5000});
      this.gap = false;
      if (this.points.length > 120) { this.points.shift(); this.points[0].breakBefore = true; }
    }
  }
  window.QuoteSeries = QuoteSeries;
  window.renderQuoteChart = (series, svg, note) => {
    svg.replaceChildren();
    const points = series.points;
    if (!points.length) { note.textContent = "尚无可绘制的报价；不会生成模拟走势。"; return; }
    const element = (tag, attrs, text) => {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
      if (text !== undefined) node.textContent = text;
      return node;
    };
    // Number is used only for SVG coordinates, never for money, risk, or stored facts.
    const min = Math.min(...points.map(point => point.value)), max = Math.max(...points.map(point => point.value));
    const start = points[0].at, end = points.at(-1).at, width = 660, height = 132;
    const x = point => 76 + (end === start ? .5 : (point.at - start) / (end - start)) * width;
    const y = point => 18 + (max === min ? .5 : (max - point.value) / (max - min)) * height;
    for (const ratio of [0, .5, 1]) {
      const position = 18 + ratio * height;
      svg.append(element("line", {x1:76,y1:position,x2:736,y2:position,class:"chart-grid"}));
      svg.append(element("text", {x:68,y:position+4,"text-anchor":"end",class:"chart-label"}, (max + (min - max) * ratio).toLocaleString("en-US", {maximumFractionDigits:2})));
    }
    let path = "";
    for (const point of points) path += `${point.breakBefore ? "M" : "L"}${x(point).toFixed(2)},${y(point).toFixed(2)} `;
    svg.append(element("path", {d:path,class:"chart-line"}));
    for (const point of points) svg.append(element("circle", {cx:x(point),cy:y(point),r:2,class:"chart-point"}));
    const time = value => new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(value));
    svg.append(element("text", {x:76,y:178,class:"chart-label"}, time(start)));
    svg.append(element("text", {x:736,y:178,"text-anchor":"end",class:"chart-label"}, time(end)));
    const last = points.at(-1);
    const label = last.kind === "book_midpoint" ? "盘口中间价（非成交价）" : "最新成交价";
    note.textContent = `${points.length} 个已接收${label}；最后 ${time(last.at)}，${last.price} USDT。缺口处断开，刷新页面后重新积累。`;
  };
})();
