/* Actual browser asset regression checks; Node built-ins only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const nodes = [];
const context = {window: {}, document: {createElementNS(_ns, tag) {
  const node = {tag, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }};
  nodes.push(node); return node;
}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../agent_platform/web/static/quote-chart.js'), 'utf8'), context);
const NOW = Date.parse('2026-10-07T08:22:55Z');
const iso = ms => new Date(ms).toISOString();
function frame(change = {}, offset = 0) {
  return {mode: 'live_read_only', generated_at: iso(NOW + offset), market: {
    source: 'binance_direct', status: 'ready', price: null,
    quote_at: iso(NOW + offset - 100), book_at: iso(NOW + offset - 100),
    book_status: 'ready', bid: '100', ask: '101', ...change
  }};
}
function series() { return new context.window.QuoteSeries(); }
let failed = 0, passed = 0;
function test(name, run) {
  try { run(); passed++; console.log('PASS ' + name); }
  catch (e) { failed++; console.log('FAIL ' + name + ': ' + e.message); }
}
test('real book draws despite absent latest trade', () => {
  const s = series(); s.observe(frame());
  assert.equal(s.points.length, 1); assert.equal(s.points[0].value, 100.5);
  assert.equal(s.points[0].kind, 'book_midpoint');
});
test('fresh book in market gap draws an explicitly separated observation', () => {
  const s = series(); s.observe(frame({status: 'gap', quote_at: null}));
  assert.equal(s.points.length, 1); assert.equal(s.points[0].breakBefore, true);
});
test('book uses its own time, never a newer trade timestamp', () => {
  const s = series(); s.observe(frame({book_at: iso(NOW - 6000)}));
  assert.equal(s.points.length, 0);
});
test('future book is rejected', () => {
  const s = series(); s.observe(frame({book_at: iso(NOW + 1)}));
  assert.equal(s.points.length, 0);
});
test('missing or crossed or invalid book never becomes zero or fabricated price', () => {
  for (const data of [{bid:null}, {ask:null}, {bid:'102'}, {bid:'0'}, {ask:'nan'}, {bid:undefined}]) {
    const s = series(); s.observe(frame(data)); assert.equal(s.points.length, 0);
  }
});
test('midpoint is preferred when independently observed book is valid', () => {
  const s = series(); s.observe(frame({price:'99'}));
  assert.equal(s.points[0].value, 100.5);
});
test('decimal midpoint label has no binary rounding artifact or lost half tick', () => {
  const s = series(); s.observe(frame({bid:'84022.63000000', ask:'84022.64000000'}));
  assert.equal(s.points[0].price, '84022.635');
  const tiny = series(); tiny.observe(frame({bid:'0.00000001', ask:'0.00000002'}));
  assert.equal(tiny.points[0].price, '0.000000015');
  const exponent = series(); exponent.observe(frame({bid:'1E-8', ask:'2E-8'}));
  assert.equal(exponent.points[0].price, '0.000000015');
});
test('trade fallback retains its explicit identity', () => {
  const s = series(); s.observe(frame({price:'99', book_status:'unavailable', bid:null, ask:null, book_at:null}));
  assert.equal(s.points.length, 1); assert.equal(s.points[0].kind, 'trade');
  assert.equal(s.points[0].value, 99);
});
test('disabled and unknown market cannot draw', () => {
  const s = series(); const v = frame(); v.mode = 'disabled'; s.observe(v);
  s.observe(frame({status:'unavailable'})); assert.equal(s.points.length, 0);
});
test('trade fallback remains blocked by market gaps', () => {
  const s = series(); s.observe(frame({status:'gap', price:'99', book_status:'unavailable'}));
  assert.equal(s.points.length, 0);
});
test('price kind switch starts a new series rather than joining unlike facts', () => {
  const s = series(); s.observe(frame());
  s.observe(frame({price:'99', book_status:'unavailable', book_at:null}, 1000));
  assert.equal(s.points.length, 1); assert.equal(s.points[0].kind, 'trade');
  assert.equal(s.points[0].breakBefore, true);
});
test('bounded history, source reset and out-of-order rejection hold', () => {
  const s = series(); for (let i=0;i<125;i++) s.observe(frame({}, i*1000));
  assert.equal(s.points.length, 120); assert.equal(s.points[0].breakBefore, true);
  s.observe(frame({}, 1000)); assert.equal(s.points.length, 120);
  s.observe(frame({source:'fake'}, 126000)); assert.equal(s.points.length, 1);
});
test('render identifies midpoint and draws actual observed points', () => {
  const s = series(); s.observe(frame()); const svg = {replaceChildren(){}, append(){}};
  const note = {}; context.window.renderQuoteChart(s, svg, note);
  assert.match(note.textContent, /盘口中间价/); assert.match(note.textContent, /非成交价/);
  assert.ok(nodes.some(node => node.tag === 'circle'));
});
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
