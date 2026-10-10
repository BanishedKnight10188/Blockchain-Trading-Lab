"use strict";
(() => {
  const W = window.Workbench;
  const groupForm = document.getElementById("group-form");
  const reviewForm = document.getElementById("review-form");
  const followupForm = document.getElementById("followup-form");
  let selected = null, groupAfter = 0, groupNext = 0, versionAfter = 0, versionNext = 0;
  let versionGeneration = 0, groupGeneration = 0, jobGeneration = 0;
  const gaps = {history_incomplete:"成交历史未确认完整",inventory_coverage_missing:"缺期初库存与资金移动覆盖",quote_quantity_missing:"缺实际成交金额",third_asset_fee_unvalued:"非BTC/USDT手续费未估值",opening_inventory_unknown:"期初BTC成本未知",unmatched_sale:"卖出缺匹配成本"};
  const current = scope => selected?.group_id === scope.groupId && versionGeneration === scope.generation;

  async function checks(record, container, scope, offset = 0) {
    if (!current(scope)) return;
    if (offset) {
      const page = await W.api(`/api/review-groups/${encodeURIComponent(scope.groupId)}/versions?after_revision=${record.review.revision - 1}&limit=1&trade_offset=${offset}`);
      if (!current(scope)) return;
      record = page.versions[0];
      if (!record) throw new Error("复盘版本未确认，请重新载入。");
    }
    for (const check of record.trade_checks) {
      const entry = W.node("div", null, "review-check");
      entry.append(W.node("strong", `成交 ${check.trade_id}`));
      const direction = check.action_deviation === null ? "无已关联原建议" : check.action_deviation ? "方向偏离原建议" : "方向与原建议一致";
      entry.append(W.node("p", `${direction}；${check.expired_at_execution === null ? "期限不可评估" : check.expired_at_execution ? "执行时原建议已过期" : "执行时仍在原期限内"}`));
      entry.append(W.node("p", `原始风格 ${check.original_style ? check.original_style.strength + " / 100，v" + check.original_style_revision : "未关联"}；手续费 ${check.fee} ${check.fee_asset}`));
      entry.append(W.node("p", `${check.quantity_check === "exceeds_original" ? "本笔数量超过原建议" : "整单数量合规无法确认"}；成交时账户纪律不可评估。`));
      const usage = check.original_model_usage;
      entry.append(W.node("p", usage ? `原建议模型费估计 ${usage.estimated_cost_usd} USD，实际 ${usage.actual_cost_usd ?? "未知"} USD${usage.token_counts_known ? "" : "，token 未知"}` : "暂无已记录的原建议模型费用。"));
      container.append(entry);
    }
    if (record.has_more_checks) {
      const more = W.node("button", "更多成交检查", "secondary");
      more.type = "button";
      more.addEventListener("click", W.load(async () => {
        if (!current(scope) || more.disabled) return;
        more.disabled = true;
        try { await checks(record, container, scope, record.next_trade_offset); if (current(scope)) more.remove(); }
        finally { more.disabled = false; }
      }));
      container.append(more);
    }
  }

  async function versions() {
    if (!selected) return;
    const scope = {groupId:selected.group_id, generation:++versionGeneration};
    const after = versionAfter;
    const page = await W.api(`/api/review-groups/${encodeURIComponent(scope.groupId)}/versions?after_revision=${after}`);
    if (!current(scope)) return;
    versionNext = page.next_revision;
    document.getElementById("more-versions").hidden = !page.may_have_more;
    const entries = [];
    if (!page.versions.length) entries.push(W.node("p", "当前页没有复盘版本。可先生成初始复盘。", "section-note"));
    for (const record of page.versions) {
      const review = record.review, entry = W.node("article", null, "review-version");
      entry.append(W.node("h3", `v${review.revision} · ${W.label(review.kind)}`), W.node("p", `数据截止 ${W.time(review.data_cutoff)}；生成 ${W.time(review.generated_at)}`, "version-meta"), W.node("p", review.explanation), W.node("p", `成本 ${W.label(record.facts.cost_status)}；完整盈亏 ${record.facts.realized_pnl_quote ?? "不可确认"} USDT；美元盈亏不可确认。`), W.node("p", record.facts.reasons.map(reason => gaps[reason] || "成本或资金移动证据缺口").join("；")));
      const context = record.retrospective_context;
      entry.append(W.node("p", context ? `后见证据：${W.time(context.captured_at)}，EMA12 ${context.features.ema_fast ?? "未知"}，EMA26 ${context.features.ema_slow ?? "未知"}，VWAP ${context.features.vwap ?? "未知"} USDT。属于事后补充。` : "没有截止前已提交的后见行情快照，不能评价后续走势。"));
      entry.append(W.node("p", `成交检查显示最多20笔，共${record.trade_count}笔；复盘模型未参与。`));
      await checks(record, entry, scope);
      if (!current(scope)) return;
      entries.push(entry);
    }
    if (current(scope)) document.getElementById("versions").replaceChildren(...entries);
  }

  async function choose(group) {
    selected = group;
    versionAfter = 0;
    ++versionGeneration;
    document.getElementById("selected-group").hidden = false;
    document.getElementById("versions").replaceChildren(W.node("p", "正在读取所选分组…", "section-note"));
    document.getElementById("more-versions").hidden = true;
    W.set("selected-title", group.group_id);
    W.set("selected-detail", `${group.trade_count}笔，冻结截止 ${W.time(group.cutoff)}，最后成交 ${W.time(group.last_execution_at)}`);
    for (const button of document.getElementById("group-list").children) button.setAttribute("aria-pressed", String(button.dataset?.groupId === group.group_id));
    for (const form of [reviewForm, followupForm]) {
      form.elements.confirmed.checked = false;
      form.elements.confirmed.dispatchEvent(new Event("change", {bubbles:true}));
    }
    await versions();
  }

  async function jobs() {
    const generation = ++jobGeneration, data = await W.api("/api/review-jobs");
    if (generation !== jobGeneration) return;
    const list = document.getElementById("job-list");
    list.replaceChildren();
    if (!data.jobs.length) list.append(W.node("p", "没有已安排的回访任务。", "section-note"));
    for (const job of data.jobs) {
      const entry = W.node("div");
      entry.append(W.node("strong", `${job.group_id} · ${W.label(job.status)}`), W.node("p", `计划截止 ${W.time(job.scheduled_for)}${job.failure_reason ? "；复盘条件不满足，请重新载入后手工回访。" : ""}`));
      if (job.status === "pending") {
        const form = W.node("form", null, "entry-form"), confirm = W.node("label", null, "confirm-row"), checkbox = W.node("input");
        checkbox.name = "confirmed"; checkbox.type = "checkbox";
        confirm.append(checkbox, W.node("span", "确认取消此任务"));
        const button = W.node("button", "取消回访", "secondary"); button.type = "submit";
        form.append(confirm, button);
        W.bind(form, async () => { await W.write(form, "/api/review-jobs/" + encodeURIComponent(job.job_id) + "/cancel", {expected_revision:job.revision}); await jobs(); });
        entry.append(form);
      }
      list.append(entry);
    }
    if (data.may_have_more) list.append(W.node("p", "任务列表仅显示前50项；完整任务仍在本机保存。", "section-note"));
  }

  async function load() {
    const generation = ++groupGeneration, page = await W.api("/api/review-groups?after_sequence=" + groupAfter);
    if (generation !== groupGeneration) return;
    W.mode(page.mode); groupNext = page.next_sequence;
    document.getElementById("more-groups").hidden = !page.has_more;
    const list = document.getElementById("group-list"); list.replaceChildren();
    if (!page.groups.length) list.append(W.node("p", "尚无已保存分组。先成功导入成交，再冻结范围。", "section-note"));
    for (const group of page.groups) {
      const button = W.node("button", `${group.group_id} · ${group.trade_count}笔 · ${W.time(group.cutoff)} · 成本${W.label(group.cost_status)}`);
      button.type = "button"; button.dataset.groupId = group.group_id;
      button.setAttribute("aria-pressed", String(selected?.group_id === group.group_id));
      button.addEventListener("click", W.load(() => choose(group))); list.append(button);
    }
    await jobs();
    if (generation === groupGeneration && selected) await versions();
  }

  W.bind(groupForm, async () => {
    const group = await W.write(groupForm, "/api/review-groups", {group_id:groupForm.elements.group_id.value.trim()});
    groupAfter = 0; await choose(group); await load();
  });
  W.bind(reviewForm, async () => {
    if (!selected) throw new Error("请先选择分组。");
    const group = selected, kind = reviewForm.elements.kind.value;
    const status = await W.api("/api/status");
    await W.write(reviewForm, `/api/review-groups/${encodeURIComponent(group.group_id)}/reviews`, {kind}, () => ({cutoff:kind === "initial" ? group.cutoff : status.generated_at}));
    if (selected?.group_id === group.group_id) { versionAfter = 0; await versions(); }
  });
  W.bind(followupForm, async () => {
    if (!selected) throw new Error("请先选择分组。");
    const group = selected, hours = [];
    if (followupForm.elements.hour1.checked) hours.push(1);
    if (followupForm.elements.hour24.checked) hours.push(24);
    if (!hours.length) throw new Error("请明确选择1小时或24小时。");
    await W.write(followupForm, `/api/review-groups/${encodeURIComponent(group.group_id)}/followups`, {hours}); await jobs();
  });
  document.getElementById("refresh-page").addEventListener("click", W.load(async () => { groupAfter = 0; versionAfter = 0; ++versionGeneration; await load(); }));
  document.getElementById("more-groups").addEventListener("click", W.load(async () => { groupAfter = groupNext; await load(); }));
  document.getElementById("more-versions").addEventListener("click", W.load(async () => { versionAfter = versionNext; await versions(); }));
  W.load(load)();
})();
