/* 商品真伪受理系统 —— 无框架单页应用 */
(() => {
  "use strict";

  const app = document.getElementById("app");
  const actorSelect = document.getElementById("actorSelect");
  const toastEl = document.getElementById("toast");
  const modal = document.getElementById("modal");
  const modalImg = document.getElementById("modalImg");
  document.getElementById("modalClose").onclick = () => (modal.hidden = true);
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });

  let META = { stores: [], products: [], lots: [], conclusions: [] };

  // ---------- 工具 ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const actor = () => actorSelect.value;
  const isHQ = () => actor() === "HQ";

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function fmtDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function toast(msg, kind = "") {
    toastEl.textContent = msg;
    toastEl.className = "toast" + (kind ? ` ${kind}` : "");
    toastEl.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (toastEl.hidden = true), 5200);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* ignore */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || `请求失败（${res.status}）`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function openImage(src) { modalImg.src = src; modal.hidden = false; }

  function statusBadge(status) {
    const map = {
      OPEN: ["open", "受理中"], ESCALATED: ["escalated", "总部复查中"], CLOSED: ["closed", "已闭环"],
      ACTIVE: ["active", "在售"], FROZEN: ["frozen", "已冻结"],
    };
    const [cls, label] = map[status] || ["open", status];
    return `<span class="badge ${cls}">${label}</span>`;
  }
  function conclusionBadge(c, version = null) {
    const map = {
      GENUINE: ["genuine", "真品"], FAKE: ["fake", "假冒商品"], INCONCLUSIVE: ["inconclusive", "无法认定"],
    };
    if (!c) return `<span class="badge open">尚无结论</span>`;
    const [cls, label] = map[c];
    const v = version ? `V${version} · ` : "";
    return `<span class="badge ${cls}">${v}${label}</span>`;
  }
  function adapterBadge(s) {
    const map = {
      OK: ["ok", "首查正常"], DUPLICATE: ["duplicate", "重复码"],
      DAMAGED: ["damaged", "标签刮损"], UNKNOWN: ["unknown", "查无此码"],
    };
    const [cls, label] = map[s] || ["open", s];
    return `<span class="badge ${cls}">${label}</span>`;
  }
  const FINDING_LABELS = {
    DUPLICATE_CODE: "防伪码重复", SALES_RECORD_MISMATCH: "销售记录不匹配",
    LABEL_DAMAGED: "标签刮损", PACKAGE_ONLY: "仅带外包装",
    SUPPLIER_DENIED_BATCH: "供应商否认批次", SEAL_BROKEN: "封签破损",
    CODE_UNKNOWN: "防伪库无记录", ALL_MATCHED: "全部匹配",
    CONCLUSION_REVISED: "复查改判",
  };

  function storeName(id) { return (META.stores.find((s) => s.id === id) || {}).name || id || "—"; }

  // ---------- 启动 ----------
  async function boot() {
    META = await api("/api/meta");
    const saved = localStorage.getItem("actor") || "S01";
    actorSelect.innerHTML = META.stores
      .map((s) => `<option value="${esc(s.id)}">${esc(s.id)} · ${esc(s.name)}</option>`).join("");
    actorSelect.value = META.stores.some((s) => s.id === saved) ? saved : "S01";
    actorSelect.onchange = () => {
      localStorage.setItem("actor", actorSelect.value);
      render();
    };
    window.addEventListener("hashchange", render);
    document.getElementById("nav").addEventListener("click", () => setTimeout(render, 0));
    render();
  }

  function render() {
    const hash = location.hash || "#/cases";
    document.querySelectorAll("#nav a").forEach((a) => {
      const key = a.dataset.route;
      a.classList.toggle("active", hash.startsWith(`#/${key}`));
    });
    const parts = hash.slice(2).split("/");
    const [view, arg] = parts;
    (ROUTES[view] || ROUTES.cases)(arg);
  }

  const ROUTES = {};

  // ---------- 调查列表 ----------
  ROUTES.cases = async function () {
    const frozenOnly = new URLSearchParams(location.hash.split("?")[1] || "").get("frozen") === "1";
    app.innerHTML = `<h1>调查列表</h1>
      <p class="page-sub">门店受理的全部真伪质疑；同一防伪码的多次举报归并为同一调查，不另立新案。</p>
      <div id="stats" class="stats"></div>
      <div class="toolbar">
        <button class="filter-pill ${!frozenOnly ? "active" : ""}" data-f="0">全部调查</button>
        <button class="filter-pill ${frozenOnly ? "active" : ""}" data-f="1">仅看关联冻结批次</button>
        <span class="spacer"></span>
        <a class="btn secondary" href="#/intake">+ 新受理</a>
        <a class="btn secondary" href="#/query">防伪码查询</a>
      </div>
      <div class="card" style="padding:0"><div class="table-wrap"><table id="caseTable">
        <thead><tr><th>调查编号</th><th>受理门店</th><th>型号/防伪码</th><th>状态</th>
        <th>当前结论</th><th>冻结批次</th><th>首次出现</th><th></th></tr></thead>
        <tbody></tbody></table></div></div>`;

    app.querySelectorAll("[data-f]").forEach((b) =>
      b.addEventListener("click", () => { location.hash = `#/cases?frozen=${b.dataset.f}`; }));

    const { items } = await api(`/api/investigations${frozenOnly ? "?frozen=1" : ""}`);
    const stats = {
      total: items.length,
      escalated: items.filter((i) => i.status === "ESCALATED").length,
      frozen: items.filter((i) => i.frozen_lot_count > 0).length,
      fake: items.filter((i) => i.current_conclusion === "FAKE").length,
    };
    document.getElementById("stats").innerHTML = `
      <div class="stat"><div class="k">有效调查</div><div class="v">${stats.total}</div></div>
      <div class="stat warn"><div class="k">复查中</div><div class="v">${stats.escalated}</div></div>
      <div class="stat warn"><div class="k">涉冻结批次调查</div><div class="v">${stats.frozen}</div></div>
      <div class="stat"><div class="k">已认定假冒</div><div class="v">${stats.fake}</div></div>`;

    const tbody = app.querySelector("#caseTable tbody");
    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="8" style="color:var(--muted);text-align:center;padding:24px">没有符合筛选条件的调查</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map((i) => {
      return `<tr>
        <td><a href="#/cases/${i.id}" class="mono"><strong>${esc(i.case_no)}</strong></a><br>
          <small style="color:var(--muted)">${fmtDateTime(i.created_at)}</small></td>
        <td>${esc(i.store_id)} · ${esc(i.store_name)}</td>
        <td>${esc(i.product_model)}<br><span class="mono" style="font-size:12.5px;color:var(--ink-soft)">${esc(i.security_code || "（无码）")}</span></td>
        <td>${statusBadge(i.status)}</td>
        <td>${i.current_conclusion
          ? conclusionBadge(i.current_conclusion, i.current_version)
          : `<span class="badge open">待复查</span>`}</td>
        <td>${i.frozen_lot_count > 0
          ? `<span class="badge frozen">${i.frozen_lot_count} 条冻结</span>`
          : `<span class="badge active">无</span>`}</td>
        <td>${fmtTime(i.first_seen_at)}<br><small style="color:var(--muted)">${esc(storeName(i.first_seen_store_id))}</small></td>
        <td><a class="btn secondary" href="#/cases/${i.id}">证据对照</a></td>
      </tr>`;
    }).join("");
  };

  // ---------- 受理表单 ----------
  ROUTES.intake = function () {
    const p = META.products.map((p) => `<option value="${esc(p.model)}">${esc(p.model)} · ${esc(p.name)}（${esc(p.supplier_name)}）</option>`).join("");
    const lots = META.lots.map((l) => `${esc(l.lot_no)}|${esc(l.product_model)}`).join(",");
    app.innerHTML = `
      <h1>受理登记</h1>
      <p class="page-sub">登记顾客质疑的商品型号、销售单、包装批次、防伪码与封签照片（照片为本地生成的虚构凭证）。
        系统立即核对本店销售记录与供应批次；发现可疑线索会自动上报总部并冻结本店相关库存。</p>
      <form id="intakeForm" class="card">
        <div class="grid-2">
          <label class="field"><span>受理门店</span>
            <select name="actor_store">${META.stores.filter((s) => !s.is_hq).map((s) =>
              `<option value="${esc(s.id)}"${s.id === actor() ? " selected" : ""}>${esc(s.id)} · ${esc(s.name)}</option>`).join("")}</select></label>
          <label class="field"><span>商品型号 *</span><select name="product_model">${p}</select></label>
          <label class="field"><span>顾客声称的销售单号</span><input name="receipt_no" placeholder="如 R-S01-1002"></label>
          <label class="field"><span>包装批次号</span><input name="lot_no" list="lotList" placeholder="如 L202609A">
            <datalist id="lotList">${META.lots.map((l) => `<option value="${esc(l.lot_no)}">${esc(l.product_model)}</option>`).join("")}</datalist></label>
          <label class="field"><span>防伪码</span><input name="security_code" placeholder="如 AC-DUP-7777"></label>
          <label class="field"><span>顾客姓名</span><input name="customer_name"></label>
          <label class="field"><span>顾客联系方式（手机号/邮箱）</span><input name="customer_contact" placeholder="用于结论告知（模拟短信/邮件）"></label>
          <label class="field"><span>告知渠道</span><select name="notify_channel">
            <option value="SMS">短信</option><option value="EMAIL">邮件</option><option value="IN_APP">站内信</option></select></label>
        </div>
        <div class="grid-2">
          <div>
            <label class="checkrow"><input type="checkbox" name="package_only"> 顾客仅携带外包装（无商品本体）</label>
            <label class="checkrow"><input type="checkbox" name="label_damaged"> 防伪标签刮损 / 字符缺失</label>
            <label class="checkrow"><input type="checkbox" name="seal_broken"> 防伪封签破损、有揭贴痕迹</label>
          </div>
          <div class="card tinted" style="margin:0">
            <h3>演示提示</h3>
            <p style="font-size:13px;color:var(--ink-soft);margin:0">
            · 输入 <span class="mono">AC-DUP-7777</span> 可体验重复码自动归并（首次出现 09-28 南京中山路店）<br>
            · 输入 <span class="mono">AC-FAKE-0001</span> + 批次 <span class="mono">L202609X</span> 触发查无此码/供应商否认<br>
            · 刮损码 <span class="mono">AC-DMG-1234</span>、正常码 <span class="mono">AC-OK-5555</span></p>
          </div>
        </div>
        <div style="margin-top:14px"><button type="submit">提交受理并自动比对</button></div>
      </form>
      <div id="intakeResult"></div>`;
    app.querySelector("#intakeForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const payload = Object.fromEntries(fd.entries());
      payload.package_only = fd.has("package_only");
      payload.label_damaged = fd.has("label_damaged");
      payload.seal_broken = fd.has("seal_broken");
      try {
        const r = await api("/api/intake", { method: "POST", body: JSON.stringify(payload) });
        if (r.merged) {
          toast(r.message, "warn");
        } else if (r.escalated) {
          toast(r.message, "warn");
        } else {
          toast(r.message);
        }
        location.hash = `#/cases/${r.investigation_id}`;
      } catch (err) { toast(err.message, "error"); }
    });
  };

  // ---------- 防伪码查询 ----------
  ROUTES.query = function () {
    app.innerHTML = `
      <h1>防伪码查询</h1>
      <p class="page-sub">通过模拟防伪平台适配器查询；同一防伪码再次出现时，查询只关联原调查并留痕，绝不生成互不相干的新案。</p>
      <form id="qForm" class="card">
        <div class="grid-2">
          <label class="field"><span>查询门店</span>
            <select name="actor_store">${META.stores.filter((s) => !s.is_hq).map((s) =>
              `<option value="${esc(s.id)}"${s.id === actor() ? " selected" : ""}>${esc(s.id)} · ${esc(s.name)}</option>`).join("")}</select></label>
          <label class="field"><span>防伪码 *</span><input name="code" placeholder="如 AC-DUP-7777"></label>
        </div>
        <label class="checkrow"><input type="checkbox" name="label_damaged"> 涂层刮损、只能部分识读</label>
        <button type="submit">查询</button>
        <p style="font-size:13px;color:var(--muted);margin:10px 0 0">
          试试重复码 <span class="mono">AC-DUP-7777</span>（任意门店查询都归并到原案）</p>
      </form>
      <div id="qResult"></div>`;
    app.querySelector("#qForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const box = document.getElementById("qResult");
      try {
        const r = await api("/api/code-search", {
          method: "POST",
          body: JSON.stringify({
            actor_store: fd.get("actor_store"),
            code: fd.get("code"),
            label_damaged: fd.has("label_damaged"),
          }),
        });
        const a = r.adapter;
        box.innerHTML = `<div class="card">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <h2 style="margin:0">查询结果</h2>${adapterBadge(a.status)}</div>
          <dl class="kv" style="margin-top:12px">
            <dt>适配器说明</dt><dd>${esc(a.message)}</dd>
            <dt>型号</dt><dd>${esc(a.product_model || "—")}</dd>
            ${a.first_seen_at ? `<dt>首次出现</dt><dd><strong>${fmtDateTime(a.first_seen_at)}</strong> · ${esc(storeName(a.first_seen_store))}</dd>` : ""}
            <dt>案件归并</dt><dd>${r.merged
              ? `已关联原调查 <a class="mono" href="#/cases/${r.investigation_id}"><strong>${esc(r.case_no)}</strong></a>，未生成新案`
              : `建立查询留痕调查 <a class="mono" href="#/cases/${r.investigation_id}">${esc(r.case_no)}</a>`}</dd>
          </dl></div>`;
        toast(r.merged ? "重复码已关联原调查" : "查询已留痕", r.merged ? "warn" : "");
      } catch (err) { box.innerHTML = ""; toast(err.message, "error"); }
    });
  };

  // ---------- 调查详情 / 证据对照 ----------
  ROUTES.cases_detail = async function (id) {
    let d;
    try { d = await api(`/api/investigations/${id}`); }
    catch (err) { app.innerHTML = `<div class="card">${esc(err.message)}</div>`; return; }

    if (d.redirected_from) {
      toast(`受理单 ${d.redirected_from.case_no} 已归并到原调查 ${d.case_no}`, "warn");
    }

    const evidenceCol = (key, title) => {
      const list = d.evidence_by_source[key] || [];
      return `<section class="evidence-col ${key === "HQ" ? "HQ" : ""}">
        <header><span>${title}</span><span class="n">${list.length} 项</span></header>
        ${list.length ? list.map((e) => `
          <div>
            <div class="t">${esc(e.title)}</div>
            <div class="meta">${fmtTime(e.created_at)}${e.related_lot_no ? ` · 批次 ${esc(e.related_lot_no)}` : ""}${e.related_receipt ? ` · 单号 ${esc(e.related_receipt)}` : ""}</div>
            <div class="detail">${esc(e.detail || "")}</div>
            ${e.file_path ? `<img class="thumb" src="/evidence/${esc(e.file_path)}" alt="${esc(e.title)}" loading="lazy">` : ""}
          </div>`).join("") : `<div class="empty-slot">暂无该来源证据</div>`}
      </section>`;
    };

    const queries = d.queries || [];
    const multiCode = queries.length > 1;
    const children = d.children || [];

    app.innerHTML = `
      <p><a href="#/cases" style="color:var(--ink-soft);text-decoration:none">← 返回调查列表</a></p>
      <div class="card">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <h1 style="margin:0" class="mono">${esc(d.case_no)}</h1>
          ${statusBadge(d.status)} ${conclusionBadge(d.current_conclusion)}
          ${d.origin === "QUERY" ? '<span class="badge merged">码查询建档</span>' : ""}
        </div>
        <dl class="kv" style="margin-top:14px">
          <dt>受理门店</dt><dd>${esc(d.store_id)} · ${esc(d.store_name)}</dd>
          <dt>商品型号</dt><dd>${esc(d.product_model)}</dd>
          <dt>防伪码</dt><dd class="mono">${esc(d.security_code || "（未提供）")}</dd>
          <dt>销售单</dt><dd class="mono">${esc(d.receipt_no || "顾客未提供 / 查无此单")}</dd>
          <dt>顾客</dt><dd>${esc(d.customer_name || "未登记")}${d.customer_contact ? ` · ${esc(d.customer_contact)}` : ""}</dd>
          <dt>建档时间</dt><dd>${fmtDateTime(d.created_at)}</dd>
        </dl>
      </div>

      ${multiCode ? `
      <div class="dup-banner">
        <span class="dup-icon">⚠</span>
        <div>
          <div class="big">防伪码重复 · 一码多用</div>
          <p>该防伪码共发生 <strong>${queries.length}</strong> 次查询/受理，已全部归并到本调查。
             首次出现于 <strong>${fmtDateTime(d.first_seen_at)}</strong>，
             门店 <strong>${esc(d.first_seen_store_name || storeName(d.first_seen_store_id))}</strong>。</p>
          <p>后续出现：${queries.slice(1).map((q) =>
            `${fmtDateTime(q.queried_at)} ${esc(q.store_name)}（${adapterBadge(q.adapter_status).replace(/^<span[^>]*>|<\/span>$/g, "")}）`).join("；")}</p>
        </div>
      </div>` : ""}

      ${children.length ? `<div class="card">
        <h2>归并到本案的重复受理（${children.length}）</h2>
        ${children.map((c) => `<div class="child-item">
          <span><span class="mono">${esc(c.case_no)}</span> · ${esc(c.store_name)} · ${fmtDateTime(c.created_at)}</span>
          <span class="mono" style="color:var(--muted)">防伪码 ${esc(c.security_code)}</span>
        </div>`).join("")}
      </div>` : ""}

      ${d.findings && d.findings.length ? `<div class="card tinted">
        <h2>系统比对发现</h2>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          ${d.findings.map((f) => `<span class="badge ${f.kind === "ALL_MATCHED" ? "ok" : "frozen"}" title="${esc(f.detail)}">${esc(FINDING_LABELS[f.kind] || f.kind)}</span>`).join("")}
        </div>
        <ul class="timeline" style="margin-top:12px">
          ${d.findings.map((f) => `<li class="${f.kind === "ALL_MATCHED" ? "" : "warn"}">
            <div class="tl-time">${fmtDateTime(f.created_at)}</div>
            <div class="tl-text"><strong>${esc(FINDING_LABELS[f.kind] || f.kind)}</strong> — ${esc(f.detail)}</div>
          </li>`).join("")}
        </ul>
      </div>` : ""}

      <h2>证据对照（按证据来源排列）</h2>
      <p class="page-sub">包装来自顾客、销售单来自本店台账、供应批次来自供应商与连锁供货记录；三方并列可直接看出矛盾点。</p>
      <div class="evidence-columns">
        ${evidenceCol("PACKAGING", "① 顾客包装 / 防伪码 / 封签")}
        ${evidenceCol("SALES", "② 本店销售记录")}
        ${evidenceCol("SUPPLY", "③ 供应批次 / 供应商")}
      </div>
      <div class="evidence-columns" style="margin-top:14px">
        ${evidenceCol("HQ", "④ 总部质控复查")}
        ${evidenceCol("SYSTEM", "⑤ 系统比对与查询留痕")}
      </div>

      <div class="grid-2" style="margin-top:18px">
        <div class="card" style="margin-bottom:0">
          <h2>关联批次与库存标记</h2>
          ${d.lots.length ? `<div class="table-wrap"><table>
            <thead><tr><th>批次</th><th>门店</th><th>库存</th><th>状态</th></tr></thead>
            <tbody>${d.lots.map((l) => `<tr>
              <td class="mono">${esc(l.lot_no)}<br><small style="color:var(--muted)">${esc(l.product_model)} · ${esc(l.supplier_name || "")}</small></td>
              <td>${esc(l.inv_store_name || "—")}</td>
              <td>${l.qty_total != null ? `${l.qty_sold}/${l.qty_total} 已售` : "无库存记录"}</td>
              <td>${l.inv_status ? statusBadge(l.inv_status) : "—"}
                ${l.inv_status === "FROZEN" ? `<br><small style="color:var(--warn-strong)">${esc(l.frozen_reason || "")}</small>` : ""}</td>
            </tr>`).join("")}</tbody></table></div>` : `<p style="color:var(--muted)">未关联批次</p>`}
          ${d.freezes.length ? `<h3 style="margin-top:14px">冻结/解冻流水</h3>
            <ul class="timeline">${d.freezes.map((f) => `<li class="${f.action === "FREEZE" ? "warn" : ""}">
              <div class="tl-time">${fmtDateTime(f.created_at)} · ${esc(f.operator_store)}</div>
              <div class="tl-text"><strong>${f.action === "FREEZE" ? "冻结" : "解除冻结"}</strong>
                ${esc(lotLabel(d, f.inventory_id))} ${f.conclusion_version ? `· 联动结论 V${f.conclusion_version}` : ""}<br>${esc(f.reason)}</div>
            </li>`).join("")}</ul>` : ""}
        </div>
        <div>
          ${renderConclusion(d)}
          ${renderReviewPanel(d)}
          ${renderNotifications(d)}
        </div>
      </div>

      <div class="card">
        <h2>防伪码查询留痕（${queries.length}）</h2>
        <ul class="timeline">
          ${queries.map((q, idx) => `<li class="${q.adapter_status === "OK" ? "" : "warn"}">
            <div class="tl-time">${fmtDateTime(q.queried_at)} · ${esc(q.store_name)}</div>
            <div class="tl-text">${adapterBadge(q.adapter_status)}
              ${idx === 0 ? "· 首次查询" : "· 关联原调查，未另立新案"}
              ${q.reported_first_at ? `<br>平台返回首次出现：${fmtDateTime(q.reported_first_at)} · ${esc(storeName(q.reported_first_store))}` : ""}</div>
          </li>`).join("") || `<li>无查询记录</li>`}
        </ul>
      </div>`;

    app.querySelectorAll("img.thumb").forEach((img) =>
      img.addEventListener("click", () => openImage(img.src)));

    const reviewForm = document.getElementById("reviewForm");
    if (reviewForm) {
      reviewForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(reviewForm);
        try {
          const r = await api("/api/hq-review", {
            method: "POST",
            body: JSON.stringify({
              actor_store: "HQ", investigation_id: d.id,
              conclusion: fd.get("conclusion"), reason: fd.get("reason"),
              decided_by: fd.get("decided_by"),
            }),
          });
          toast(`总部结论 V${r.version} 已出具${r.revised ? "（改判）" : ""}；库存动作同事务完成。`,
                r.conclusion === "GENUINE" ? "" : "warn");
          render();
        } catch (err) { toast(err.message, "error"); }
      });
    }
    const storeAttempt = document.getElementById("storeAttempt");
    if (storeAttempt) {
      storeAttempt.addEventListener("click", async () => {
        try {
          await api("/api/hq-review", {
            method: "POST",
            body: JSON.stringify({ actor_store: actor(), investigation_id: d.id,
              conclusion: "FAKE", reason: "门店尝试自行改判（应被拒绝）" }),
          });
          toast("异常：门店请求未被拒绝", "error");
        } catch (err) {
          toast(`已被系统拒绝（HTTP ${err.status}）：${err.message}`, "warn");
        }
      });
    }
    const notifyBtn = document.getElementById("notifyBtn");
    if (notifyBtn) {
      notifyBtn.addEventListener("click", async () => {
        try {
          const r = await api(`/api/investigations/${d.id}/notify`, {
            method: "POST",
            body: JSON.stringify({ actor_store: actor() }),
          });
          toast(`已通过模拟网关发送（回执 ${r.provider_ref}）`);
          render();
        } catch (err) { toast(err.message, "error"); }
      });
    }
  };

  function lotLabel(d, inventoryId) {
    const l = (d.lots || []).find((x) => x.inventory_id === inventoryId);
    return l ? `${l.lot_no}@${l.inv_store_id}` : `#${inventoryId}`;
  }

  function renderConclusion(d) {
    if (!d.versions || !d.versions.length) {
      return `<div class="card"><h2>总部质控结论</h2>
        <p style="color:var(--muted)">总部尚未出具结论${d.status === "ESCALATED" ? "，复查中；相关库存保持冻结。" : "。"}</p></div>`;
    }
    const cur = d.versions[d.versions.length - 1];
    return `<div class="conclusion-card ${cur.conclusion}">
      <div class="head">总部质控结论 · V${cur.version}${cur.revised ? "（改判）" : ""}
        ${conclusionBadge(cur.conclusion)}</div>
      <div class="conclusion-body">
        <div style="font-size:13px;color:var(--muted)">${fmtDateTime(cur.created_at)} · 判定人 ${esc(cur.decided_by)}</div>
        <p class="conclusion-reason">${esc(cur.reason)}</p>
      </div>
      ${d.versions.length > 1 ? `<div class="version-history"><details>
        <summary>历史版本（${d.versions.length - 1} 个旧版本，全部保留）</summary>
        ${d.versions.slice(0, -1).reverse().map((v) => `<div class="version-row">
          <div class="vr-head"><strong>V${v.version}</strong>${conclusionBadge(v.conclusion)}
            ${v.revised ? '<span class="badge merged">改判版本</span>' : ""}
            <span style="color:var(--muted);font-size:12.5px">${fmtDateTime(v.created_at)} · ${esc(v.decided_by)}</span></div>
          <p class="vr-reason">${esc(v.reason)}</p>
        </div>`).join("")}
      </details></div>` : ""}
    </div>`;
  }

  function renderReviewPanel(d) {
    if (isHQ()) {
      const radio = (v, label, checked) =>
        `<label class="checkrow"><input type="radio" name="conclusion" value="${v}"${checked ? " checked" : ""}> ${label}</label>`;
      return `<div class="card">
        <h2>总部质控复核</h2>
        <p style="font-size:13px;color:var(--muted);margin-top:0">出具结论即生成新版本（改判会保留历史版本）；
          冻结/解冻与结论在同一事务提交，复查期间疑似批次无法售出。</p>
        <form id="reviewForm">
          <label class="field"><span>判定人</span><input name="decided_by" value="当值质控"></label>
          ${radio("GENUINE", "真品（解除冻结，恢复销售）", false)}
          ${radio("FAKE", "假冒商品（连锁同批库存全部冻结）", true)}
          ${radio("INCONCLUSIVE", "无法认定（维持冻结待补证）", false)}
          <label class="field" style="margin-top:8px"><span>复查说明（完整留存，顾客可见）*</span>
            <textarea name="reason" placeholder="基于包装、销售单、供应批次三方证据给出复查意见……"></textarea></label>
          <button type="submit" class="warn">${d.current_version ? `出具 V${d.current_version + 1} 结论` : "出具 V1 结论"}</button>
        </form>
      </div>`;
    }
    return `<div class="card">
      <h2>总部质控复核</h2>
      <p style="font-size:13.5px;color:var(--ink-soft)">当前身份为门店（${esc(actor())}）。
        门店只能受理、上报与按总部结论告知顾客，<strong>不能自行出具或修改总部结论</strong>。</p>
      <button class="ghost-danger" id="storeAttempt">以门店身份尝试修改结论（验证权限拦截）</button>
    </div>`;
  }

  function renderNotifications(d) {
    const list = d.notifications || [];
    return `<div class="card">
      <h2>客户告知</h2>
      ${d.customer_contact
        ? `<p style="font-size:13.5px">接收方：<span class="mono">${esc(d.customer_contact)}</span>（${esc(d.notify_channel || "SMS")}）
           ${d.current_conclusion ? `<button id="notifyBtn" class="secondary" style="margin-left:10px">按当前结论 V${d.current_version} 告知顾客</button>`
           : `<span style="color:var(--muted)">；总部出结论后才可告知</span>`}</p>`
        : `<p style="color:var(--muted)">未登记联系方式</p>`}
      ${list.length ? `<div style="margin-top:8px">${list.map((n) => `
        <div class="notice-line">
          <strong>${fmtDateTime(n.sent_at || n.created_at)}</strong> · ${esc(n.channel)} → <span class="mono">${esc(n.destination)}</span>
          ${n.conclusion_version ? `· 随 V${n.conclusion_version}` : ""}
          <br><span style="color:var(--ink-soft)">${esc(n.body)}</span>
          <br><small style="color:var(--muted)">模拟网关回执：${esc(n.provider_ref)} · ${esc(n.status)}</small>
        </div>`).join("")}</div>` : `<p style="color:var(--muted);font-size:13px">尚无告知记录</p>`}
    </div>`;
  }

  // ---------- 库存 ----------
  ROUTES.inventory = async function () {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const status = params.get("status") || "";
    const store = params.get("store") || "";
    app.innerHTML = `
      <h1>库存与批次冻结</h1>
      <p class="page-sub">复查期间标记的库存批次；冻结与质控结论同事务更新。可尝试对冻结批次下单，验证竞态下无法售出。</p>
      <div class="toolbar">
        <button class="filter-pill ${!status ? "active" : ""}" data-s="">全部状态</button>
        <button class="filter-pill ${status === "FROZEN" ? "active" : ""}" data-s="FROZEN">已冻结</button>
        <button class="filter-pill ${status === "ACTIVE" ? "active" : ""}" data-s="ACTIVE">在售</button>
        <select id="storeFilter" style="width:auto">
          <option value="">全部门店</option>
          ${META.stores.filter((s) => !s.is_hq).map((s) => `<option value="${esc(s.id)}"${s.id === store ? " selected" : ""}>${esc(s.name)}</option>`).join("")}
        </select>
      </div>
      <div class="card" style="padding:0"><div class="table-wrap"><table>
        <thead><tr><th>门店</th><th>商品</th><th>批次</th><th>供应商</th><th>已售/总量</th>
        <th>状态</th><th>冻结原因/时间</th><th>竞态验证</th></tr></thead>
        <tbody id="invBody"></tbody></table></div></div>`;
    app.querySelectorAll("[data-s]").forEach((b) =>
      b.addEventListener("click", () => {
        const s = b.dataset.s;
        location.hash = `#/inventory${s || store ? `?${new URLSearchParams([...(s ? [["status", s]] : []), ...(store ? [["store", store]] : [])]).toString()}` : ""}`;
      }));
    app.querySelector("#storeFilter").addEventListener("change", (e) => {
      const s = e.target.value;
      location.hash = `#/inventory${status || s ? `?${new URLSearchParams([...(status ? [["status", status]] : []), ...(s ? [["store", s]] : [])]).toString()}` : ""}`;
    });
    const qs = new URLSearchParams();
    if (status) qs.set("status", status);
    if (store) qs.set("store", store);
    const { items } = await api(`/api/inventory?${qs.toString()}`);
    document.getElementById("invBody").innerHTML = items.map((n) => `
      <tr>
        <td>${esc(n.store_id)}<br><small style="color:var(--muted)">${esc(n.store_name)}</small></td>
        <td>${esc(n.product_model)}<br><small style="color:var(--muted)">${esc(n.product_name)}</small></td>
        <td class="mono">${esc(n.lot_no)}</td>
        <td>${esc(n.supplier_name)}</td>
        <td>${n.qty_sold}/${n.qty_total}</td>
        <td>${statusBadge(n.status)}</td>
        <td style="max-width:260px">${n.frozen_reason ? `${esc(n.frozen_reason)}<br><small style="color:var(--muted)">${fmtDateTime(n.frozen_at)}</small>` : "—"}</td>
        <td><button class="secondary sell-btn" data-store="${esc(n.store_id)}"
              data-lotid="${n.lot_id}" data-model="${esc(n.product_model)}" data-frozen="${n.status === "FROZEN"}">
            模拟销售一单</button></td>
      </tr>`).join("") || `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:22px">无库存记录</td></tr>`;
    app.querySelectorAll(".sell-btn").forEach((btn) =>
      btn.addEventListener("click", async () => {
        const receipt = `R-DEMO-${Date.now().toString().slice(-6)}`;
        try {
          await api("/api/demo/sell", {
            method: "POST",
            body: JSON.stringify({
              store_id: btn.dataset.store, receipt_no: receipt,
              lot_id: Number(btn.dataset.lotid), product_model: btn.dataset.model,
            }),
          });
          btn.closest("tr").style.background = "#f4faf4";
          toast(`销售单 ${receipt} 出库成功。`);
          render();
        } catch (err) {
          toast(err.data && err.data.lot_frozen
            ? `销售被拒绝：${err.message}`
            : err.message, err.data && err.data.lot_frozen ? "warn" : "error");
        }
      }));
  };

  // ---------- 审计 ----------
  ROUTES.audit = async function () {
    app.innerHTML = `<h1>审计日志</h1>
      <p class="page-sub">受理、归并、冻结/解冻、总部结论改判、越权拒绝等关键动作全部留痕。</p>
      <div class="card" style="padding:0"><div class="table-wrap"><table>
        <thead><tr><th>时间</th><th>执行者</th><th>动作</th><th>对象</th><th>明细</th></tr></thead>
        <tbody id="auditBody"><tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">加载中…</td></tr></tbody>
      </table></div></div>`;
    const { items } = await api("/api/audit");
    document.getElementById("auditBody").innerHTML = items.map((a) => `
      <tr><td class="mono" style="white-space:nowrap">${fmtDateTime(a.created_at)}</td>
      <td>${esc(a.actor_store)}${a.actor_name ? `<br><small style="color:var(--muted)">${esc(a.actor_name)}</small>` : ""}</td>
      <td><span class="badge ${a.action.includes("FREEZE") || a.action.includes("MERGE") || a.action === "ESCALATE" ? "frozen" : "open"}">${esc(a.action)}</span></td>
      <td>${esc(a.entity)} ${esc(a.entity_id || "")}</td>
      <td style="max-width:480px"><code style="font-size:12px;white-space:pre-wrap;word-break:break-all">${esc(a.detail || "")}</code></td></tr>`).join("");
  };

  boot().catch((e) => { app.innerHTML = `<div class="card">启动失败：${esc(e.message)}</div>`; });
})();
