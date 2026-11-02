/* OCI Monitor UI: vanilla JS, no dependencies. All URLs are relative so the
   page works under any --base-path. */
"use strict";

(() => {
  // ---------------------------------------------------------------- helpers
  const $ = (sel) => document.querySelector(sel);

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  function icon(path, cls) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    if (cls) svg.setAttribute("class", cls);
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", path);
    svg.append(p);
    return svg;
  }
  const CHEVRON = "m9 6 6 6-6 6";

  const store = {
    get(k, d) { try { const v = localStorage.getItem("ocimon." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("ocimon." + k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const humanize = (s) => String(s).replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
  const stateLabel = (s) => String(s || "UNKNOWN").replace(/_/g, " ");
  const fmtNum = (n) => (n === null || n === undefined || n === "" ? "—" : Number(n).toLocaleString());
  function fmtBytes(n) {
    if (n === null || n === undefined) return "—";
    const u = ["B", "KB", "MB", "GB", "TB", "PB"];
    let i = 0; let v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`;
  }
  function fmtAgo(ts) {
    if (!ts) return "never";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }
  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(sec));
    const m = Math.floor(sec / 60); const s = sec % 60;
    return m ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
  }
  const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
  const fmtInterval = (sec) => (sec % 60 === 0 ? `${sec / 60} min` : `${sec}s`);
  function fmtValue(v) {
    if (v === null || v === undefined || v === "") return "—";
    if (typeof v === "boolean") return v ? "Yes" : "No";
    if (Array.isArray(v)) return v.length ? v.map((x) => (typeof x === "object" ? JSON.stringify(x) : x)).join(", ") : "—";
    if (typeof v === "object") return Object.entries(v).map(([k, n]) => `${stateLabel(k)}: ${n}`).join(", ") || "—";
    return String(v);
  }

  const pill = (state, cls) => h("span", { class: `pill s-${cls || "unknown"}` }, stateLabel(state));

  let toastTimer;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 1800);
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Fallback for non-secure contexts (plain http on a non-localhost name).
      const ta = h("textarea", { class: "sr-only" }); ta.value = text;
      document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
    }
    toast("Copied to clipboard");
  }

  // ------------------------------------------------------------------- API
  class HttpError extends Error { constructor(status, msg) { super(msg); this.status = status; } }

  async function api(path, params, opts = {}) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) if (v !== "" && v !== null && v !== undefined && v !== false) qs.set(k, v);
    const url = `api/${path}${qs.toString() ? "?" + qs : ""}`;
    const res = await fetch(url, { credentials: "same-origin", cache: "no-store", ...opts });
    if (!res.ok) throw new HttpError(res.status, `${res.status} ${res.statusText}`);
    return res.json();
  }

  // ----------------------------------------------------------------- state
  const S = {
    meta: null,
    health: null,
    summary: null,
    tree: null,
    items: [],
    loadedAt: null,         // last_refresh of the data on screen
    loading: true,
    filters: { compartment: "", region: "", type: "", category: "", state: "", q: "", problems: false, include_terminated: false, view: "", days: 7 },
    alarms: [],             // alarm rows for the Alerts tab (current compartment/region/search)
    cost: null,             // /api/cost response for the current compartment/region/range
    costCurrency: null,     // set when resource rows carry cost
    costTable: false,       // chart shown as a table
    expanded: new Set(),
    sort: store.get("sort", {}),
    openTree: new Set(store.get("openTree", [])),
    collapsedSections: new Set(store.get("collapsedSections", [])),
    showAll: new Set(),
    connectionLost: false,
    authRequired: false,
  };

  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    const f = S.filters;
    for (const k of ["compartment", "region", "type", "category", "state", "q"]) f[k] = p.get(k) || "";
    f.problems = p.get("problems") === "1";
    f.include_terminated = p.get("terminated") === "1";
    f.view = ["cost", "alerts"].includes(p.get("view")) ? p.get("view") : "";
    f.days = [7, 14, 30].includes(Number(p.get("days"))) ? Number(p.get("days")) : 7;
  }
  function writeHash() {
    const f = S.filters; const p = new URLSearchParams();
    for (const k of ["compartment", "region", "type", "category", "state", "q"]) if (f[k]) p.set(k, f[k]);
    if (f.problems) p.set("problems", "1");
    if (f.include_terminated) p.set("terminated", "1");
    if (f.view) p.set("view", f.view);
    if (f.view === "cost" && f.days !== 7) p.set("days", f.days);
    const hash = p.toString();
    history.replaceState(null, "", hash ? `#${hash}` : location.pathname + location.search);
  }

  const baseParams = () => ({
    compartment: S.filters.compartment, region: S.filters.region, q: S.filters.q,
    include_terminated: S.filters.include_terminated,
  });
  const fullParams = () => ({
    ...baseParams(), type: S.filters.type, category: S.filters.category,
    state: S.filters.state, problems: S.filters.problems,
  });

  // ------------------------------------------------------------ data load
  let loadSeq = 0;
  async function loadData() {
    const seq = ++loadSeq;
    try {
      // Cards and filter options ignore type/state/problem filters so they stay usable for navigation.
      const [summary, tree, res, alarms] = await Promise.all([
        api("summary", baseParams()),
        api("compartments", { region: S.filters.region, include_terminated: S.filters.include_terminated }),
        api("resources", fullParams()),
        api("resources", { type: "alarm", compartment: S.filters.compartment, region: S.filters.region, q: S.filters.q }),
      ]);
      if (seq !== loadSeq) return; // a newer request superseded this one
      S.alarms = alarms.items;
      S.summary = summary; S.tree = tree.tree; S.items = res.items; S.loadedAt = res.last_refresh;
      S.costCurrency = res.currency || null;
      S.loading = false;
      setConnection(true);
      render();
    } catch (err) {
      if (seq !== loadSeq) return;
      handleError(err);
    }
  }

  async function loadHealth() {
    try {
      // viewer=1 tells the server someone is looking, which keeps OCI refreshes running.
      const health = await api("health", { viewer: document.hidden ? "" : "1" });
      setConnection(true);
      const changed = health.last_refresh !== (S.health && S.health.last_refresh);
      const prevCost = S.health && S.health.cost;
      const costChanged = !!health.cost && (!prevCost || health.cost.last_refresh !== prevCost.last_refresh
        || health.cost.status !== prevCost.status);
      S.health = health;
      renderStatus();
      renderErrors();
      if (changed && health.last_refresh) {
        if (!S.meta || !S.meta.regions.length) await loadMeta();
        await loadData();
      } else if (!health.last_refresh) {
        renderContent();
      }
      // New cost data: refresh the Cost tab and the cost column on resource rows.
      if (costChanged) {
        if (!changed && health.last_refresh && health.cost.last_refresh) await loadData();
        await loadCost();
      }
    } catch (err) {
      handleError(err);
    }
  }

  async function loadMeta() {
    S.meta = await api("meta");
    $("#mode-badge").hidden = S.meta.mode !== "demo";
    $("#tenancy").textContent = S.meta.tenancy_name || "";
    $("#cost-tab").hidden = !S.meta.cost_enabled;
    renderRegions();
  }

  function handleError(err) {
    if (err instanceof HttpError && err.status === 401) S.authRequired = true;
    else S.connectionLost = true;
    updateBanner();
  }

  function setConnection(ok) {
    if (ok) { S.connectionLost = false; S.authRequired = false; } else S.connectionLost = true;
    updateBanner();
  }

  // One banner, highest-priority message wins.
  function updateBanner() {
    const hl = S.health;
    let text = null;
    if (S.authRequired) text = "Access token required. Re-open the dashboard using the URL with ?token=… printed at startup.";
    else if (S.connectionLost) text = "Lost connection to the OCI Monitor server. Retrying…";
    else if (hl && hl.last_failure && hl.last_refresh) text = `Last refresh failed (${hl.last_failure}). Showing data from ${fmtAgo(hl.last_refresh)}.`;
    else if (hl && hl.auth && hl.auth.token_expired) text = `Your OCI session token has expired. Run: oci session refresh --profile ${hl.auth.profile || "DEFAULT"}`;
    const b = $("#banner");
    b.hidden = !text;
    if (text && b.textContent !== text) b.textContent = text;
  }

  // ---------------------------------------------------------------- render
  function render() {
    renderFilters();
    renderCards();
    renderTree();
    renderScope();
    renderContent();
    renderView();
  }

  function renderRegions() {
    const sel = $("#region");
    const regions = (S.meta && S.meta.regions) || [];
    sel.replaceChildren(
      h("option", { value: "" }, regions.length > 1 ? `All regions (${regions.length})` : regions[0] || "All regions"),
      ...regions.map((r) => h("option", { value: r }, r)),
    );
    sel.value = S.filters.region;
    sel.hidden = regions.length <= 1 && !S.filters.region;
  }

  function renderStatus() {
    const hl = S.health; if (!hl) return;
    if (S.filters.view === "cost" && hl.cost && hl.cost.status !== "disabled") { renderCostStatus(hl); return; }
    const upd = $("#updated");
    if (hl.last_refresh) {
      upd.textContent = `Updated ${fmtAgo(hl.last_refresh)} · took ${hl.duration_s}s`;
      upd.title = new Date(hl.last_refresh * 1000).toLocaleString();
    } else {
      upd.textContent = hl.status === "error" ? "Refresh failed" : "Collecting…";
    }
    const auto = $("#auto-refresh");
    renderPauseButton(hl.auto_refresh && hl.auto_refresh.paused);
    if (hl.refreshing) auto.textContent = "Refreshing now…";
    else if (hl.auto_refresh && hl.auto_refresh.paused) auto.textContent = "Auto-refresh paused · use Refresh now to update";
    else if (hl.next_refresh_at) {
      auto.textContent = `Auto-refresh every ${fmtInterval(hl.refresh_interval)} · next in ${fmtDuration(hl.next_refresh_at - Date.now() / 1000)}`;
    } else auto.textContent = `Auto-refresh every ${fmtInterval(hl.refresh_interval)}`;

    setRefreshButton(!!hl.refreshing);
    renderErrorsButton();
    updateBanner();
  }

  function setRefreshButton(busy) {
    const btn = $("#refresh-btn");
    btn.disabled = busy;
    btn.classList.toggle("spinning", busy);
  }

  function renderErrorsButton() {
    const hl = S.health;
    const eb = $("#errors-btn");
    const n = hl.errors.length;
    eb.hidden = n === 0;
    eb.title = `${n} read issue${n === 1 ? "" : "s"}`;
    eb.replaceChildren(`⚠ ${n}`, h("span", { class: "eb-label" }, ` read issue${n === 1 ? "" : "s"}`));
  }

  function renderCostStatus(hl) {
    const c = hl.cost;
    const upd = $("#updated");
    upd.textContent = c.last_refresh ? `Cost updated ${fmtAgo(c.last_refresh)}` : (c.status === "error" ? "Cost unavailable" : "Fetching cost…");
    upd.title = c.last_refresh ? new Date(c.last_refresh * 1000).toLocaleString() : "";
    const auto = $("#auto-refresh");
    renderPauseButton(c.paused);
    if (c.refreshing) auto.textContent = "Fetching cost data…";
    else if (c.paused) auto.textContent = "Auto-refresh paused · use Refresh now to update";
    else if (c.next_refresh_at) auto.textContent = `Cost refresh every ${fmtInterval(c.refresh_interval)} · next in ${fmtDuration(c.next_refresh_at - Date.now() / 1000)}`;
    else auto.textContent = `Cost refresh every ${fmtInterval(c.refresh_interval)}`;
    setRefreshButton(!!c.refreshing);
    renderErrorsButton();
    updateBanner();
  }

  function statusLegend(by) {
    const parts = [["ok", "healthy"], ["stopped", "stopped"], ["warn", "warning"], ["bad", "critical"], ["unknown", "unknown"]];
    return parts.filter(([k]) => by[k]).map(([k, label]) => h("span", {}, h("span", { class: `dot bg-${k}` }), h("b", {}, by[k]), ` ${label}`));
  }
  function statusBar(by, total) {
    const bar = h("div", { class: "card-bar" });
    for (const k of ["ok", "stopped", "unknown", "warn", "bad"]) {
      if (!by[k]) continue;
      const seg = h("span", { class: `bg-${k}` });
      seg.style.width = `${(100 * by[k]) / total}%`;
      bar.append(seg);
    }
    return bar;
  }

  function renderCards() {
    const el = $("#cards");
    const sm = S.summary; if (!sm) return;
    const t = sm.totals;
    // Cards are views: Problems shows every problem; a category card shows that whole category.
    // Clicking the card that is already the current view clears it.
    const problemsView = S.filters.problems && !S.filters.category;
    const problems = h("button", {
      class: `card problems-card ${t.problems ? "" : "none"} ${S.filters.problems ? "active" : ""}`,
      type: "button", title: problemsView ? "Show everything" : "Show only problems",
      onclick: () => setFilter(problemsView ? { problems: false } : { problems: true, category: "", type: "", state: "" }),
    },
    h("div", { class: "card-label" }, "Problems"),
    h("div", { class: "card-total" }, t.problems),
    h("div", { class: "card-legend" }, t.problems
      ? [h("span", {}, h("span", { class: "dot bg-bad" }), h("b", {}, t.critical), " critical"),
        h("span", {}, h("span", { class: "dot bg-warn" }), h("b", {}, t.warning), " warning")]
      : "All monitored resources healthy"));

    const cards = sm.cards.map((c) => {
      const isView = S.filters.category === c.category && !S.filters.problems;
      return h("button", {
        class: `card ${c.total ? "" : "empty"} ${S.filters.category === c.category ? "active" : ""}`,
        type: "button", title: isView ? "Show all categories" : `Show all ${c.label}`,
        onclick: () => setFilter(isView ? { category: "" } : { category: c.category, type: "", state: "", problems: false }),
      },
      h("div", { class: "card-label" }, c.label, c.problems ? h("span", { class: "problem-badge", title: `${c.problems} problems` }, c.problems) : null),
      h("div", { class: "card-total" }, c.total),
      c.total ? statusBar(c.by_status, c.total) : h("div", { class: "card-bar" }),
      h("div", { class: "card-legend" }, c.total ? statusLegend(c.by_status) : "None"));
    });
    el.replaceChildren(problems, ...cards);
  }

  function renderFilters() {
    const types = (S.meta && S.meta.types) || [];
    const counts = (S.summary && S.summary.types) || {};
    const cats = (S.meta && S.meta.categories) || [];
    const tsel = $("#type-filter");
    const groups = cats.map((c) => {
      const opts = types.filter((t) => t.category === c.key && (counts[t.key] || S.filters.type === t.key))
        .map((t) => h("option", { value: t.key }, `${t.label} (${counts[t.key] ? counts[t.key].total : 0})`));
      return opts.length ? h("optgroup", { label: c.label }, opts) : null;
    }).filter(Boolean);
    tsel.replaceChildren(h("option", { value: "" }, "All types"), ...groups);
    tsel.value = S.filters.type;

    const ssel = $("#state-filter");
    const states = Object.entries((S.summary && S.summary.by_state) || {}).sort((a, b) => b[1] - a[1]);
    if (S.filters.state && !states.some(([s]) => s === S.filters.state)) states.push([S.filters.state, 0]);
    ssel.replaceChildren(h("option", { value: "" }, "All states"), ...states.map(([s, n]) => h("option", { value: s }, `${stateLabel(s)} (${n})`)));
    ssel.value = S.filters.state;

    $("#region").value = S.filters.region;
    $("#problems-toggle").checked = S.filters.problems;
    $("#terminated-toggle").checked = S.filters.include_terminated;
    const search = $("#search");
    if (document.activeElement !== search) search.value = S.filters.q;
    const f = S.filters;
    $("#clear-filters").hidden = !(f.type || f.category || f.state || f.q || f.problems || f.region);
  }

  function renderScope() {
    const node = S.filters.compartment && findNode(S.tree, S.filters.compartment);
    const name = node ? (node.id === S.tree.id ? `${node.name} (all compartments)` : node.path) : (S.tree ? `${S.tree.name} (all compartments)` : "");
    $("#scope").textContent = name;
    $("#scope").title = name;
  }

  function findNode(node, id) {
    if (!node) return null;
    if (node.id === id) return node;
    for (const c of node.children) { const f = findNode(c, id); if (f) return f; }
    return null;
  }

  function renderTree() {
    const nav = $("#tree");
    if (!S.tree) { nav.replaceChildren(h("div", { class: "muted small" }, S.loading ? "Loading…" : "No compartments")); return; }
    const selected = S.filters.compartment || S.tree.id;
    // Always show the path to the selected node.
    const openPath = (node, trail) => {
      if (node.id === selected) { trail.forEach((id) => S.openTree.add(id)); return true; }
      return node.children.some((c) => openPath(c, [...trail, node.id]));
    };
    openPath(S.tree, []);

    const build = (node, depth) => {
      const hasKids = node.children.length > 0;
      const open = depth === 0 || S.openTree.has(node.id);
      const row = h("div", { class: `tree-row ${node.id === selected ? "selected" : ""}` },
        hasKids && depth > 0
          ? h("button", {
            class: `caret ${open ? "open" : ""}`, type: "button", "aria-label": open ? "Collapse" : "Expand",
            onclick: (e) => { e.stopPropagation(); toggleTree(node.id); },
          }, icon(CHEVRON))
          : h("span", { class: "caret-spacer" }),
        h("button", {
          class: "tree-name", type: "button", title: node.description ? `${node.path}\n${node.description}` : node.path,
          onclick: () => { setFilter({ compartment: node.id === S.tree.id ? "" : node.id }); closeNav(); },
        }, node.name),
        node.total_problems ? h("span", { class: "problem-badge", title: `${node.total_problems} problem${node.total_problems === 1 ? "" : "s"} in this compartment or below` }, node.total_problems) : null,
        h("span", { class: "count", title: `${node.total} resources (${node.count} directly here)` }, node.total));
      const li = h("li", { role: "treeitem", "aria-expanded": hasKids ? String(open) : null, "aria-selected": String(node.id === selected) }, row);
      if (hasKids && open) li.append(h("ul", { role: "group" }, node.children.map((c) => build(c, depth + 1))));
      return li;
    };
    nav.replaceChildren(h("ul", {}, build(S.tree, 0)));
  }

  function toggleTree(id) {
    if (S.openTree.has(id)) S.openTree.delete(id); else S.openTree.add(id);
    store.set("openTree", [...S.openTree]);
    renderTree();
  }

  function renderErrors() {
    const panel = $("#errors");
    const errs = (S.health && S.health.errors) || [];
    if (!errs.length) { panel.hidden = true; return; }
    if (panel.dataset.open !== "1") { panel.hidden = true; return; }
    panel.hidden = false;
    const rows = errs.map((e) => h("tr", {},
      h("td", {}, h("span", { class: `tag ${e.kind === "permission" ? "perm" : "err"}` }, e.kind === "permission" ? "No access" : "Error")),
      h("td", {}, e.region || "global"),
      h("td", { title: e.compartment_id || "" }, e.compartment_path || e.compartment_name || (e.compartment_id ? e.compartment_id.slice(0, 32) + "…" : "—")),
      h("td", {}, e.service),
      h("td", { class: "mono" }, e.operation),
      h("td", {}, e.status ? `${e.status}${e.code ? " " + e.code : ""}` : "—"),
      h("td", { class: "wrap" }, e.message, e.count > 1 ? h("span", { class: "muted" }, ` (×${e.count})`) : null)));
    panel.replaceChildren(
      h("div", { class: "errors-head" },
        h("span", {}, `${errs.length} compartment/service combination${errs.length === 1 ? "" : "s"} could not be read`),
        h("button", { class: "link-btn", type: "button", onclick: () => { panel.dataset.open = "0"; renderErrors(); } }, "Hide")),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["", "Region", "Compartment", "Service", "Operation", "Status", "Message"].map((c) => h("th", {}, c)))),
        h("tbody", {}, rows))));
  }

  // ------------------------------------------------------ resource tables
  const det = (k) => (r) => r.details[k];
  const rel = (name) => (r) => { const x = r.related.find((x) => x.rel === name); return x ? x.name || x.id.slice(0, 24) + "…" : null; };
  const ipList = (k) => ({ key: k, label: k === "public_ips" ? "Public IP" : "Private IP", get: (r) => (r.details[k] || []).join(", "), mono: true });

  const C = {
    shape: { key: "shape", label: "Shape", get: det("shape") },
    ad: { key: "availability_domain", label: "AD", get: det("availability_domain") },
    version: { key: "version", label: "Version", get: det("version") },
    vcn: { key: "vcn", label: "VCN", get: rel("vcn") },
    visibility: { key: "visibility", label: "Visibility", get: det("visibility"), render: (r) => visibilityTag(r.details.visibility) },
  };
  function num(key, label, fmt) { return { key, label, get: det(key), num: true, fmt }; }
  function visibilityTag(v) { return v ? h("span", { class: `tag ${/public/i.test(v) ? "public" : ""}` }, v) : "—"; }
  function backendSummary(r) {
    const sets = r.details.backend_sets || [];
    if (!sets.length) return "—";
    const bad = sets.filter((s) => s.status !== "OK");
    return bad.length ? `${bad.length}/${sets.length} unhealthy` : `${sets.length} OK`;
  }

  const HEALTH_COL = { key: "health", label: "Health", get: (r) => ({ CRITICAL: 3, WARNING: 2, OK: 1 }[r.health] || 0), render: healthCell };

  const COLUMNS = {
    instance: [HEALTH_COL, C.shape, num("ocpus", "OCPUs"), num("memory_gb", "Memory GB"), pctCol("cpu_pct", "CPU"),
      pctCol("memory_pct", "Mem used"), ipList("private_ips"), ipList("public_ips")],
    volume: [num("size_gb", "Size GB"), num("vpus_per_gb", "VPUs/GB"),
      { key: "attached", label: "Attached", get: det("attached"), render: (r) => attachedCell(r) }, C.ad],
    boot_volume: [num("size_gb", "Size GB"), num("vpus_per_gb", "VPUs/GB"),
      { key: "attached", label: "Attached", get: det("attached"), render: (r) => attachedCell(r) }, C.ad],
    load_balancer: [{ key: "health", label: "Health", get: (r) => r.health, render: healthCell }, C.shape, C.visibility,
      { key: "ips", label: "IP addresses", get: (r) => [...(r.details.public_ips || []), ...(r.details.private_ips || [])].join(", "), mono: true },
      { key: "backend_sets", label: "Backend sets", get: backendSummary }],
    network_load_balancer: [{ key: "health", label: "Health", get: (r) => r.health, render: healthCell }, C.visibility,
      { key: "ips", label: "IP addresses", get: (r) => [...(r.details.public_ips || []), ...(r.details.private_ips || [])].join(", "), mono: true },
      { key: "backend_sets", label: "Backend sets", get: backendSummary }],
    db_system: [C.version, C.shape, { key: "edition", label: "Edition", get: (r) => r.details.edition && humanize(r.details.edition.replace("_EDITION", "")) },
      num("cpu_cores", "Cores"), num("nodes", "Nodes"), num("storage_gb", "Storage GB")],
    autonomous_database: [HEALTH_COL, pctCol("storage_pct", "Storage used"), pctCol("cpu_pct", "CPU"),
      { key: "workload", label: "Workload", get: det("workload") }, C.version,
      { key: "compute", label: "Compute", get: det("compute") }, num("storage_tb", "Storage TB"),
      { key: "free_tier", label: "Free tier", get: det("free_tier"), render: (r) => (r.details.free_tier ? "Yes" : "No") }],
    mysql_db_system: [HEALTH_COL, pctCol("storage_pct", "Storage used"), pctCol("cpu_pct", "CPU"), pctCol("memory_pct", "Mem used"),
      C.version, C.shape, { key: "heatwave", label: "HeatWave", get: det("heatwave") },
      { key: "highly_available", label: "HA", get: det("highly_available"), render: (r) => (r.details.highly_available ? "Yes" : "No") },
      { key: "endpoint", label: "Endpoint", get: det("endpoint"), mono: true }],
    oke_cluster: [{ key: "kubernetes_version", label: "Kubernetes", get: det("kubernetes_version") },
      { key: "cluster_type", label: "Type", get: (r) => r.details.cluster_type && humanize(r.details.cluster_type.replace("_CLUSTER", "")) },
      num("node_pools", "Node pools")],
    oke_node_pool: [{ key: "health", label: "Nodes health", get: (r) => r.health, render: healthCell },
      { key: "cluster", label: "Cluster", get: rel("cluster") }, { key: "node_shape", label: "Shape", get: det("node_shape") },
      num("size", "Size"), { key: "nodes", label: "Nodes", get: (r) => fmtValue(r.details.nodes) },
      { key: "kubernetes_version", label: "Kubernetes", get: det("kubernetes_version") }],
    vcn: [{ key: "cidr_blocks", label: "CIDR blocks", get: (r) => (r.details.cidr_blocks || []).join(", "), mono: true },
      { key: "dns_label", label: "DNS label", get: det("dns_label") }],
    subnet: [{ key: "cidr", label: "CIDR", get: det("cidr"), mono: true }, { key: "access", label: "Access", get: det("access"), render: (r) => visibilityTag(r.details.access) },
      { key: "scope", label: "Scope", get: det("scope") }, C.vcn],
    internet_gateway: [C.vcn, { key: "enabled", label: "Enabled", get: det("enabled"), render: (r) => (r.details.enabled ? "Yes" : "No") }],
    nat_gateway: [C.vcn, { key: "nat_ip", label: "NAT IP", get: det("nat_ip"), mono: true },
      { key: "blocking_traffic", label: "Blocking", get: det("blocking_traffic"), render: (r) => (r.details.blocking_traffic ? "Yes" : "No") }],
    service_gateway: [C.vcn, { key: "services", label: "Services", get: (r) => (r.details.services || []).join(", ") }],
    drg: [],
    local_peering_gateway: [C.vcn, { key: "peering_status", label: "Peering", get: det("peering_status") },
      { key: "peer_cidr", label: "Peer CIDR", get: det("peer_cidr"), mono: true }],
    bucket: [C.visibility, num("objects", "Objects"), { key: "size_bytes", label: "Size", get: det("size_bytes"), num: true, fmt: fmtBytes },
      { key: "storage_tier", label: "Tier", get: det("storage_tier") }, { key: "versioning", label: "Versioning", get: det("versioning") }],
    functions_application: [num("functions", "Functions"), C.shape],
    api_gateway: [{ key: "endpoint_type", label: "Endpoint", get: det("endpoint_type") }, { key: "hostname", label: "Hostname", get: det("hostname"), mono: true }],
    alarm: [
      { key: "severity", label: "Severity", get: (r) => SEV_RANK[r.details.severity] || 0, render: (r) => severityPill(r.details.severity) },
      { key: "triggered_at", label: "Last triggered", get: det("triggered_at"), render: triggeredCell },
      { key: "namespace", label: "Metric namespace", get: det("namespace") },
      { key: "query", label: "Query", get: det("query"), render: (r) => h("span", { class: "mono clip", title: r.details.query || null }, r.details.query || "—") },
      num("notifications", "Notify"),
    ],
  };

  function attachedCell(r) {
    const a = r.details.attached;
    if (a === null || a === undefined) return h("span", { class: "muted" }, "Unknown");
    if (!a) return h("span", { class: "tag" }, "Unattached");
    const to = rel("instance")(r);
    return to ? `Yes · ${to}` : "Yes";
  }
  function healthCell(r) {
    if (!r.health) return h("span", { class: "muted" }, "—");
    const reasons = (r.health_reasons || []).join("\n");
    return h("span", { title: reasons || null }, pill(r.health, r.health_class));
  }

  // Utilization % with a small meter colored by the server's thresholds (see /api/meta).
  function pctCell(v, key) {
    if (v === undefined || v === null) return h("span", { class: "muted" }, "—");
    const t = (S.meta && S.meta.thresholds && S.meta.thresholds[key]) || {};
    const cls = t.crit != null && v >= t.crit ? "bad" : t.warn != null && v >= t.warn ? "warn" : "ok";
    const bar = h("span", { class: "mini-meter" }, h("span", { class: `bg-${cls}` }));
    bar.firstChild.style.width = `${Math.min(100, Math.max(0, v))}%`;
    return h("span", { class: "pct" }, bar, `${v.toFixed(1)}%`);
  }
  function pctCol(key, label) {
    return { key, label, num: true, get: (r) => (r.metrics ? r.metrics[key] : undefined), render: (r) => pctCell(r.metrics && r.metrics[key], key) };
  }

  const SEV_RANK = { CRITICAL: 4, ERROR: 3, WARNING: 2, INFO: 1 };
  function severityPill(sev) {
    return sev ? pill(sev, { CRITICAL: "bad", ERROR: "bad", WARNING: "warn" }[sev] || "unknown") : "—";
  }
  function triggeredCell(r) {
    const iso = r.details.triggered_at;
    if (!iso) return h("span", { class: "muted" }, "never");
    const ago = fmtAgo(Date.parse(iso) / 1000);
    return h("span", { title: new Date(iso).toLocaleString() }, r.state === "FIRING" ? `firing since ${ago.replace(" ago", "")}` : ago);
  }

  const SEVERITY = { bad: 4, warn: 3, stopped: 2, unknown: 1, ok: 0 };
  // Types whose billing can't be tied to the individual resource, and why.
  const UNATTRIBUTED_COST = {
    mysql_db_system: "OCI bills MySQL HeatWave under internal instance IDs that aren't linked to the DB system, "
      + "so per-database cost isn't available. The total is on the Cost tab under the MySQL service, "
      + "and the biggest items appear in Top 10 resources.",
  };
  function columnsFor(type, multiRegion) {
    const cols = [
      { key: "name", label: "Name", get: (r) => r.name.toLowerCase(), render: (r) => [icon(CHEVRON, "chev"), r.name], cls: "name" },
      { key: "state", label: "State", get: (r) => SEVERITY[r.status] * 1000 + r.state.charCodeAt(0), render: (r) => pill(r.state, r.state_class) },
      ...(COLUMNS[type] || []),
      { key: "compartment", label: "Compartment", get: (r) => r.compartment_path || r.compartment_name || "" },
    ];
    if (S.costCurrency) {
      cols.push({
        key: "cost", label: "Cost/day", num: true, title: "Average daily cost over the last 7 full days (UTC)",
        get: (r) => (r.cost ? r.cost.avg_day : null),
        render: (r) => (r.cost ? money(r.cost.avg_day)
          : UNATTRIBUTED_COST[r.type] ? h("span", { class: "muted", title: UNATTRIBUTED_COST[r.type] }, "n/a") : "—"),
      });
    }
    if (multiRegion) cols.push({ key: "region", label: "Region", get: (r) => r.region });
    return cols;
  }

  function sortItems(items, cols, type) {
    const s = S.sort[type];
    if (!s) return items;
    const col = cols.find((c) => c.key === s.key);
    if (!col) return items;
    const dir = s.dir === "desc" ? -1 : 1;
    return [...items].sort((a, b) => {
      let x = col.get(a); let y = col.get(b);
      const nx = x === null || x === undefined || x === ""; const ny = y === null || y === undefined || y === "";
      if (nx || ny) return nx && ny ? 0 : nx ? 1 : -1; // empty values last
      if (typeof x === "boolean") { x = +x; y = +y; }
      if (typeof x === "number" && typeof y === "number") return (x - y) * dir;
      return String(x).localeCompare(String(y), undefined, { numeric: true }) * dir;
    });
  }

  function setSort(type, key, rerender = renderContent) {
    const cur = S.sort[type];
    S.sort[type] = cur && cur.key === key ? { key, dir: cur.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" };
    store.set("sort", S.sort);
    rerender();
  }

  const ROW_LIMIT = 200;

  // rerender: the view that owns the table (Resources or Alerts), called after sort/expand.
  function renderTable(type, items, multiRegion, rerender = renderContent) {
    const cols = columnsFor(type, multiRegion);
    const sorted = sortItems(items, cols, type);
    const limited = S.showAll.has(type) ? sorted : sorted.slice(0, ROW_LIMIT);
    const s = S.sort[type];
    const thead = h("thead", {}, h("tr", {}, cols.map((c) => h("th", {
      class: `sortable ${c.num ? "num" : ""}`, scope: "col", tabindex: "0", title: c.title || null,
      "aria-sort": s && s.key === c.key ? (s.dir === "asc" ? "ascending" : "descending") : "none",
      onclick: () => setSort(type, c.key, rerender),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSort(type, c.key, rerender); } },
    }, c.label, h("span", { class: "arrow" }, s && s.key === c.key ? (s.dir === "asc" ? "▲" : "▼") : "")))));

    const tbody = h("tbody");
    for (const r of limited) {
      const open = S.expanded.has(r.id);
      const tr = h("tr", {
        class: `res-row ${r.problem ? "problem-" + r.problem : ""} ${r.state === "TERMINATED" || r.state === "DELETED" ? "terminated" : ""} ${open ? "expanded" : ""}`,
        tabindex: "0", "aria-expanded": String(open),
        onclick: (e) => { if (!e.target.closest("a,button")) toggleRow(r.id, rerender); },
        onkeydown: (e) => { if (e.key === "Enter") toggleRow(r.id, rerender); },
      }, cols.map((c) => {
        const v = c.render ? c.render(r) : (c.fmt ? c.fmt(c.get(r)) : c.num ? fmtNum(c.get(r)) : fmtValue(c.get(r)));
        return h("td", { class: [c.cls, c.num && "num", c.mono && "mono"].filter(Boolean).join(" ") || null, title: c.key === "name" ? r.name : null }, v);
      }));
      tbody.append(tr);
      if (open) tbody.append(h("tr", { class: "detail-row" }, h("td", { colspan: cols.length }, renderDetail(r))));
    }
    if (sorted.length > limited.length) {
      tbody.append(h("tr", { class: "more-row" }, h("td", { colspan: cols.length },
        h("button", { class: "link-btn", type: "button", onclick: () => { S.showAll.add(type); rerender(); } },
          `Show all ${sorted.length} (${sorted.length - limited.length} more)`))));
    }
    return h("div", { class: "table-wrap" }, h("table", {}, thead, tbody));
  }

  function toggleRow(id, rerender = renderContent) {
    if (S.expanded.has(id)) S.expanded.delete(id); else S.expanded.add(id);
    rerender();
  }

  const DETAIL_SKIP = new Set(["backend_sets", "note"]);
  function renderDetail(r) {
    const actions = h("div", { class: "actions" },
      r.console_url ? h("a", { class: "btn", href: r.console_url, target: "_blank", rel: "noopener noreferrer" }, "Open in OCI Console ↗") : null);
    const left = h("div", {},
      h("h4", {}, "OCID"),
      h("div", { class: "ocid-line" }, h("code", {}, r.id),
        h("button", { class: "btn copy-btn", type: "button", onclick: () => copy(r.id) }, "Copy")),
      actions,
      r.source === "search" ? h("p", { class: "note small" }, "Details could not be read (see read issues); basic data from Resource Search.") : null,
      h("dl", { class: "kv" },
        kv("Type", r.type_label),
        kv("State", pill(r.state, r.state_class)),
        r.health ? kv("Health", pill(r.health, r.health_class)) : null,
        kv("Compartment", r.compartment_path || r.compartment_id),
        kv("Region", r.region),
        kv("Created", fmtDate(r.time_created)),
        ...r.related.map((x) => kv(humanize(x.rel), h("span", {}, x.name || "", " ",
          h("button", { class: "link-btn small mono", type: "button", title: "Copy OCID", onclick: () => copy(x.id) }, x.id.slice(0, 28) + "…"))))));

    const right = h("div", {});
    const metricKeys = Object.keys(r.metrics || {});
    if (metricKeys.length || (r.health_reasons || []).length) {
      const label = { cpu_pct: "CPU", memory_pct: "Memory used", storage_pct: "Storage used", fs_status: "Filesystem" };
      // DOM append() would print a literal "null", so add the optional list separately.
      right.append(h("h4", {}, "Health signals · last 5 min"));
      if ((r.health_reasons || []).length) right.append(h("ul", { class: "reasons" }, r.health_reasons.map((x) => h("li", {}, x))));
      right.append(
        h("dl", { class: "kv" }, metricKeys.map((k) => kv(label[k] || humanize(k),
          k === "fs_status" ? (r.metrics[k] > 0 ? pill("PROBLEM", "bad") : pill("OK", "ok")) : pctCell(r.metrics[k], k)))));
    }
    if (!r.cost && UNATTRIBUTED_COST[r.type] && S.costCurrency) {
      right.append(h("h4", {}, "Cost"), h("p", { class: "muted small wrap" }, UNATTRIBUTED_COST[r.type]));
    }
    if (r.cost) {
      right.append(h("h4", {}, "Cost · last 7 days"), h("div", { class: "cost-detail" },
        sparkline(r.cost.daily, 160, 36, "Daily cost, last 7 full days"),
        h("div", {}, h("b", {}, money(r.cost.total_7d)), h("span", { class: "muted" }, ` total · ${money(r.cost.avg_day)}/day`))));
    }
    const sets = r.details.backend_sets;
    if (sets && sets.length) {
      right.append(h("h4", {}, "Backend sets"), h("div", { class: "bs-list" }, sets.map((s) =>
        h("div", {}, pill(s.status, { OK: "ok", WARNING: "warn", CRITICAL: "bad" }[s.status] || "unknown"), " ", s.name,
          s.backends !== undefined && s.backends !== null ? h("span", { class: "muted" }, ` · ${s.backends} backends`) : null))));
    }
    const extra = Object.entries(r.details).filter(([k]) => !DETAIL_SKIP.has(k));
    if (extra.length) {
      right.append(h("h4", {}, "Details"), h("dl", { class: "kv" }, extra.map(([k, v]) =>
        kv(humanize(k), k === "size_bytes" ? fmtBytes(v) : k === "triggered_at" && v ? fmtDate(v) : fmtValue(v)))));
    }
    return h("div", { class: "detail" }, left, right);
  }
  function kv(k, v) { return [h("dt", {}, k), h("dd", {}, v)]; }

  function renderContent() {
    const el = $("#content");
    const hl = S.health;
    if (S.loading || !hl || !hl.last_refresh) {
      if (hl && hl.status === "error") {
        el.replaceChildren(h("div", { class: "state-box" }, h("h3", {}, "Could not collect data from OCI"),
          h("p", {}, hl.last_failure || "Unknown error"), h("p", { class: "small" }, "Check the terminal running ocimon for details.")));
      } else {
        el.replaceChildren(h("div", { class: "state-box" }, h("div", { class: "spinner" }),
          h("h3", {}, "Collecting inventory from OCI…"),
          h("p", {}, "The first refresh can take up to a minute for large tenancies or many regions.")));
        if (!S.summary) $("#cards").replaceChildren(...Array.from({ length: 6 }, () => h("div", { class: "skeleton" })));
      }
      return;
    }
    if (!S.items.length) {
      const f = S.filters;
      const chips = activeFilterChips();
      const where = S.tree && f.compartment ? (findNode(S.tree, f.compartment) || {}).name : null;
      el.replaceChildren(h("div", { class: "state-box" },
        h("h3", {}, chips.length ? "No resources match these filters" : "No resources in this compartment"),
        f.problems && !(f.type || f.state || f.q) ? h("p", {}, `Nothing unhealthy${where ? " in " + where : ""}. 🎉`) : null,
        chips.length ? h("p", { class: "small" }, "Remove a filter:") : null,
        chips.length ? h("div", { class: "chips" }, chips) : null,
        chips.length > 1 ? h("button", { class: "btn", type: "button", onclick: clearFilters }, "Clear all filters") : null));
      return;
    }

    const types = (S.meta && S.meta.types) || [];
    const cats = (S.meta && S.meta.categories) || [];
    const multiRegion = !S.filters.region && ((S.meta && S.meta.regions.length) || 0) > 1;
    const byType = new Map();
    for (const r of S.items) { if (!byType.has(r.type)) byType.set(r.type, []); byType.get(r.type).push(r); }

    const sections = [];
    for (const cat of cats) {
      const blocks = types.filter((t) => t.category === cat.key && byType.has(t.key));
      if (!blocks.length) continue;
      const total = blocks.reduce((n, t) => n + byType.get(t.key).length, 0);
      const problems = blocks.reduce((n, t) => n + byType.get(t.key).filter((r) => r.problem).length, 0);
      const collapsed = S.collapsedSections.has(cat.key);
      sections.push(h("section", { class: `section ${collapsed ? "collapsed" : ""}` },
        h("div", { class: "section-head" },
          h("button", { class: "section-toggle", type: "button", "aria-expanded": String(!collapsed), onclick: () => toggleSection(cat.key) },
            icon("m6 9 6 6 6-6"), h("h2", {}, cat.label)),
          h("span", { class: "count" }, total),
          problems ? h("span", { class: "problem-badge" }, problems) : null),
        h("div", { class: "section-body" }, blocks.map((t) => h("div", { class: "type-block" },
          blocks.length > 1 || t.label !== cat.label ? h("div", { class: "type-title" }, `${t.label} · ${byType.get(t.key).length}`) : null,
          renderTable(t.key, byType.get(t.key), multiRegion))))));
    }
    el.replaceChildren(...sections);
  }

  // One removable chip per active filter (the compartment selection isn't a filter here).
  function activeFilterChips() {
    const f = S.filters;
    const typeLabel = (k) => ((S.meta && S.meta.types.find((t) => t.key === k)) || {}).label || k;
    const catLabel = (k) => ((S.meta && S.meta.categories.find((c) => c.key === k)) || {}).card || k;
    const chip = (label, patch) => h("button", { class: "chip", type: "button", title: "Remove this filter", onclick: () => setFilter(patch) },
      label, h("span", { class: "chip-x", "aria-hidden": "true" }, "×"));
    return [
      f.problems && chip("Only problems", { problems: false }),
      f.category && chip(`Category: ${catLabel(f.category)}`, { category: "" }),
      f.type && chip(`Type: ${typeLabel(f.type)}`, { type: "" }),
      f.state && chip(`State: ${stateLabel(f.state)}`, { state: "" }),
      f.region && chip(`Region: ${f.region}`, { region: "" }),
      f.q && chip(`Search: “${f.q}”`, { q: "" }),
    ].filter(Boolean);
  }

  function toggleSection(key) {
    if (S.collapsedSections.has(key)) S.collapsedSections.delete(key); else S.collapsedSections.add(key);
    store.set("collapsedSections", [...S.collapsedSections]);
    renderContent();
  }

  // ---------------------------------------------------------------- alerts
  function renderAlertsBadge() {
    const firing = S.alarms.filter((a) => a.state === "FIRING").length;
    const badge = $("#alerts-badge");
    badge.hidden = firing === 0;
    badge.textContent = firing;
    badge.title = `${firing} alarm${firing === 1 ? "" : "s"} firing`;
  }

  function renderAlerts() {
    const el = $("#alerts-view");
    if (el.hidden) return;
    const hl = S.health;
    const head = h("div", { class: "cost-head" }, h("div", { class: "scope" }, h("span", { class: "muted small" }, "Alerts for"),
      h("span", { class: "scope-name", title: S.tree ? scopeLabel() : "" }, S.tree ? scopeLabel() : ""),
      S.filters.region ? h("span", { class: "tag" }, S.filters.region) : null));
    if (S.loading || !hl || !hl.last_refresh) {
      el.replaceChildren(head, h("div", { class: "state-box" }, h("div", { class: "spinner" }), h("h3", {}, "Collecting alarms…")));
      return;
    }
    const all = S.alarms;
    if (!all.length) {
      el.replaceChildren(head, h("div", { class: "state-box" },
        h("h3", {}, S.filters.q ? "No alarms match the search" : "No alarms here"),
        h("p", {}, "Alarms are defined in OCI Monitoring; the ones you can read appear here with their current status.")));
      return;
    }
    const dayAgo = Date.now() - 864e5;
    const firing = all.filter((a) => a.state === "FIRING")
      .sort((a, b) => (SEV_RANK[b.details.severity] || 0) - (SEV_RANK[a.details.severity] || 0) || a.name.localeCompare(b.name));
    const recent = all.filter((a) => a.state !== "FIRING" && a.details.triggered_at && Date.parse(a.details.triggered_at) >= dayAgo);
    const rest = all.filter((a) => a.state !== "FIRING" && !recent.includes(a));
    const count = (st) => all.filter((a) => a.state === st).length;
    const crit = firing.filter((a) => a.problem === "critical").length;

    const tile = (label, value, sub, cls) => h("div", { class: `tile ${cls || ""}` },
      h("div", { class: "tile-label" }, label), h("div", { class: "tile-value" }, value), sub ? h("div", { class: "tile-sub" }, sub) : null);
    const tiles = h("div", { class: "tiles" },
      tile("Firing now", firing.length, firing.length
        ? [h("span", {}, h("span", { class: "dot bg-bad" }), h("b", {}, crit), " critical  "),
          h("span", {}, h("span", { class: "dot bg-warn" }), h("b", {}, firing.length - crit), " warning/info")]
        : h("span", { class: "muted" }, "all clear"), firing.length ? "tile-alert" : ""),
      tile("Triggered in last 24 h", recent.length, h("span", { class: "muted" }, "now back to OK")),
      tile("Suppressed", count("SUPPRESSED"), h("span", { class: "muted" }, "notifications paused")),
      tile("Disabled", count("DISABLED"), null),
      tile("Total alarms", all.length, null));

    const multiRegion = !S.filters.region && ((S.meta && S.meta.regions.length) || 0) > 1;
    const section = (key, title, items, emptyText, closedByDefault) => {
      const collapsed = closedByDefault ? !S.collapsedSections.has(`${key}:open`) : S.collapsedSections.has(key);
      const toggle = () => {
        const k = closedByDefault ? `${key}:open` : key;
        if (S.collapsedSections.has(k)) S.collapsedSections.delete(k); else S.collapsedSections.add(k);
        store.set("collapsedSections", [...S.collapsedSections]);
        renderAlerts();
      };
      return h("section", { class: `section ${collapsed ? "collapsed" : ""}` },
        h("div", { class: "section-head" },
          h("button", { class: "section-toggle", type: "button", "aria-expanded": String(!collapsed), onclick: toggle },
            icon("m6 9 6 6 6-6"), h("h2", {}, title)),
          h("span", { class: "count" }, items.length)),
        h("div", { class: "section-body" }, items.length ? renderTable("alarm", items, multiRegion, renderAlerts)
          : h("p", { class: "muted" }, emptyText)));
    };
    el.replaceChildren(head, tiles,
      section("alerts-firing", "Firing now", firing, "No alarms are firing. 🎉"),
      section("alerts-recent", "Triggered in the last 24 hours", recent, "Nothing triggered in the last 24 hours."),
      section("alerts-other", "All other alarms", rest, "None.", true));
  }

  // ------------------------------------------------------------------ cost
  function money(v, compact) {
    const cur = (S.cost && S.cost.currency) || S.costCurrency || "";
    const opts = compact ? { notation: "compact", maximumFractionDigits: 1 } : { minimumFractionDigits: 2, maximumFractionDigits: 2 };
    if (/^[A-Z]{3}$/.test(cur)) {
      try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, ...opts }).format(v); } catch { /* unknown code */ }
    }
    return `${new Intl.NumberFormat(undefined, opts).format(v)}${cur ? " " + cur : ""}`;
  }
  const utcDay = (iso, opts) => new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, { timeZone: "UTC", ...opts });

  function svg(tag, attrs, ...kids) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, v);
    for (const c of kids.flat()) if (c !== null && c !== undefined) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return el;
  }

  function sparkline(values, w, hgt, label) {
    const max = Math.max(...values); const min = Math.min(0, ...values);
    const step = values.length > 1 ? (w - 6) / (values.length - 1) : 0;
    const y = (v) => 3 + (hgt - 6) * (1 - (v - min) / (max - min || 1));
    const last = values.length - 1;
    const el = svg("svg", { class: "viz spark", viewBox: `0 0 ${w} ${hgt}`, role: "img",
      "aria-label": `${label}: ${values.map((v) => money(v)).join(", ")}` },
    svg("polyline", { class: "spark-line", points: values.map((v, i) => `${3 + i * step},${y(v)}`).join(" ") }),
    svg("circle", { class: "spark-dot", cx: 3 + last * step, cy: y(values[last]), r: 3 }));
    el.style.width = `${w}px`; el.style.height = `${hgt}px`;
    return el;
  }

  function delta(pct, period) {
    if (pct === null || pct === undefined) return h("span", { class: "muted" }, `no data for the previous ${period}`);
    if (Math.abs(pct) < 0.05) return h("span", { class: "muted" }, `unchanged vs previous ${period}`);
    // For cost, up is bad: color carries direction × meaning, the arrow and sign carry it without color.
    const up = pct > 0;
    return h("span", { class: up ? "delta-bad" : "delta-good" }, `${up ? "▲ +" : "▼ "}${pct.toFixed(1)}%`,
      h("span", { class: "muted" }, ` vs previous ${period}`));
  }

  let costSeq = 0;
  async function loadCost() {
    if (!S.meta || !S.meta.cost_enabled || S.filters.view !== "cost") return;
    const seq = ++costSeq;
    try {
      const data = await api("cost", { days: S.filters.days, compartment: S.filters.compartment, region: S.filters.region });
      if (seq !== costSeq) return;
      S.cost = data;
      renderCost();
    } catch (err) {
      if (seq === costSeq) handleError(err);
    }
  }

  function currentView() {
    if (S.filters.view === "cost") return S.meta && S.meta.cost_enabled ? "cost" : "resources";
    return S.filters.view === "alerts" ? "alerts" : "resources";
  }

  function renderView() {
    const view = currentView();
    $("#resources-view").hidden = view !== "resources";
    $("#alerts-view").hidden = view !== "alerts";
    $("#cost-view").hidden = view !== "cost";
    for (const t of document.querySelectorAll(".tab")) {
      const on = t.dataset.view === view;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", String(on));
    }
    const search = $("#search");
    search.disabled = view === "cost";
    search.placeholder = view === "cost" ? "Search applies to Resources and Alerts"
      : view === "alerts" ? "Search alarm name, metric, query…" : "Search name, OCID, IP, shape…";
    renderStatus();
    renderAlertsBadge();
    if (view === "cost") renderCost();
    if (view === "alerts") renderAlerts();
  }

  function scopeLabel() {
    const node = S.filters.compartment && findNode(S.tree, S.filters.compartment);
    if (node && node.id !== S.tree.id) return node.path;
    return S.tree ? `${S.tree.name} (all compartments)` : "";
  }

  function renderCost() {
    const el = $("#cost-view");
    if (el.hidden) return;
    const c = S.cost;
    const meta = (S.health && S.health.cost) || {};
    const head = h("div", { class: "cost-head" },
      h("div", { class: "scope" }, h("span", { class: "muted small" }, "Cost for"),
        h("span", { class: "scope-name", title: scopeLabel() }, scopeLabel()),
        S.filters.region ? h("span", { class: "tag" }, S.filters.region) : null),
      h("div", { class: "range", role: "group", "aria-label": "Date range" }, [7, 14, 30].map((d) => h("button", {
        class: `range-btn ${S.filters.days === d ? "active" : ""}`, type: "button", "aria-pressed": String(S.filters.days === d),
        onclick: () => setFilter({ days: d }),
      }, `Last ${d} days`))));

    if (!c || !c.totals) {
      const failed = (c && c.status === "error") || meta.status === "error";
      el.replaceChildren(head, h("div", { class: "state-box" },
        failed ? null : h("div", { class: "spinner" }),
        h("h3", {}, failed ? "Cost data is not available" : "Fetching cost data from the OCI Usage API…"),
        h("p", { class: failed ? "wrap" : "" }, failed ? (meta.error || "Unknown error")
          : "This runs in the background and refreshes hourly; the Resources tab is unaffected.")));
      return;
    }

    const t = c.totals;
    const period = `${c.days} days`;
    const tile = (label, value, sub) => h("div", { class: "tile" },
      h("div", { class: "tile-label" }, label), h("div", { class: "tile-value" }, value), sub ? h("div", { class: "tile-sub" }, sub) : null);
    const tiles = h("div", { class: "tiles" },
      tile(`Last ${period}`, money(t.period), delta(t.delta_pct, period)),
      tile("Daily average", money(t.daily_avg), h("span", { class: "muted" }, `over ${c.days} full days`)),
      tile("Yesterday", money(t.yesterday), h("span", { class: "muted" }, utcDay(c.chart[c.chart.length - 2].date, { weekday: "long", month: "short", day: "numeric" }))),
      tile("Today so far", money(t.today_partial), h("span", { class: "muted" }, "partial · usage data lags a few hours")),
      t.covered_pct !== null && t.covered_pct !== undefined
        ? tile("Matched to resources", `${t.covered_pct}%`, h("span", { class: "muted" }, "of spend; the rest is backups, VNICs, logs…"))
        : null);

    const chartPanel = h("section", { class: "panel" },
      h("div", { class: "panel-head" },
        h("div", {}, h("h3", {}, "Daily cost by service"), h("div", { class: "muted small" }, "UTC days · today is partial")),
        h("button", { class: "link-btn small", type: "button", onclick: () => { S.costTable = !S.costTable; renderCost(); } },
          S.costTable ? "View as chart" : "View as table")),
      costLegend(c.series),
      S.costTable ? costDailyTable(c) : costChart(c));

    el.replaceChildren(head, tiles, chartPanel,
      h("div", { class: "grid-2" }, costServiceTable(c), costCompartmentTable(c)),
      costTopResources(c));
  }

  const slotClass = (slot) => (slot ? `slot-${slot}` : "slot-other");
  function costSlot(c, name) { const s = c.series.find((x) => x.name === name); return s ? s.slot : null; }

  function costLegend(series) {
    return h("div", { class: "legend" }, series.map((s) => h("span", { class: "legend-item" },
      h("span", { class: `swatch ${slotClass(s.slot)}` }), s.name)));
  }

  function niceScale(max, count) {
    if (!(max > 0)) return { step: 1, top: 1 };
    const raw = max / count; const mag = 10 ** Math.floor(Math.log10(raw)); const n = raw / mag;
    const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
    return { step, top: Math.ceil(max / step) * step };
  }

  let chartObserver = null;
  function costChart(c) {
    const wrap = h("div", { class: "chart" });
    const tip = h("div", { class: "chart-tip", role: "status" });
    tip.hidden = true;
    wrap.append(tip);
    let lastW = 0;
    const draw = () => {
      const W = Math.floor(wrap.clientWidth);
      if (!W || W === lastW) return;
      lastW = W;
      const old = wrap.querySelector("svg.chart-svg");
      if (old) old.remove();
      wrap.prepend(buildChart(c, W, tip));
    };
    if (chartObserver) chartObserver.disconnect();
    // Draw on the next frame: drawing inside the callback resizes the page and re-triggers the observer.
    chartObserver = new ResizeObserver(() => requestAnimationFrame(draw));
    chartObserver.observe(wrap);
    return wrap;
  }

  function buildChart(c, W, tip) {
    const H = 280; const m = { l: 64, r: 12, t: 22, b: 30 };
    const pw = W - m.l - m.r; const ph = H - m.t - m.b;
    const days = c.chart; const n = days.length;
    const { step, top } = niceScale(Math.max(0, ...days.map((d) => d.total)), 4);
    const y = (v) => m.t + ph - (v / top) * ph;
    const band = pw / n; const bw = Math.min(24, Math.max(4, band * 0.62));
    const order = c.series.map((s) => s.name);
    const root = svg("svg", { class: "viz chart-svg", viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": `Stacked daily cost by service for ${n} days. Use “View as table” for the values.` });
    root.style.width = `${W}px`; root.style.height = `${H}px`;

    for (let v = 0; v <= top + step / 1e6; v += step) {
      root.append(svg("line", { class: v === 0 ? "axis" : "grid", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
      root.append(svg("text", { class: "tick", x: m.l - 8, y: y(v) + 4, "text-anchor": "end" }, money(v, true)));
    }
    const every = n <= 15 ? 1 : 3;
    const lastFull = n - 2;
    const showTip = (i, g, x) => {
      const d = days[i];
      root.classList.add("hovering");
      g.classList.add("hover");
      tip.replaceChildren(
        h("div", { class: "tip-date" }, utcDay(d.date, { weekday: "short", month: "short", day: "numeric" }), d.partial ? " (partial)" : ""),
        h("div", { class: "tip-total" }, h("b", {}, money(d.total)), h("span", { class: "muted" }, " total")),
        ...order.slice().reverse().filter((k) => d.values[k]).map((k) => h("div", { class: "tip-row" },
          h("span", { class: `key ${slotClass(costSlot(c, k))}` }), h("b", {}, money(d.values[k])), h("span", { class: "muted" }, ` ${k}`))));
      tip.hidden = false;
      // Right of the column, or left of it when it would overflow the chart.
      const right = x + band + 8;
      tip.style.left = `${right + tip.offsetWidth > W ? Math.max(0, x - tip.offsetWidth - 8) : right}px`;
      tip.style.top = `${m.t}px`;
    };
    const hideTip = (g) => { root.classList.remove("hovering"); g.classList.remove("hover"); tip.hidden = true; };

    days.forEach((d, i) => {
      const x0 = m.l + i * band; const x = x0 + (band - bw) / 2;
      const g = svg("g", { class: `col ${d.partial ? "partial" : ""}` });
      const present = order.filter((k) => (d.values[k] || 0) > 0);
      let base = m.t + ph;
      present.forEach((k, j) => {
        const hgt = (d.values[k] / top) * ph;
        const yTop = base - hgt;
        const cls = `seg ${slotClass(costSlot(c, k))}`;
        if (j === present.length - 1) {
          const r = Math.min(4, bw / 2, hgt);
          g.append(svg("path", { class: cls, d: `M${x},${base}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + bw - r}Q${x + bw},${yTop} ${x + bw},${yTop + r}V${base}Z` }));
        } else if (hgt > 2.5) {
          // 2px surface gap above each segment separates it from the next one.
          g.append(svg("rect", { class: cls, x, y: yTop + 2, width: bw, height: hgt - 2 }));
        }
        base = yTop;
      });
      if (i === lastFull && d.total > 0) {
        g.append(svg("text", { class: "bar-label", x: x + bw / 2, y: y(d.total) - 6, "text-anchor": "middle" }, money(d.total, true)));
      }
      if (i % every === (n - 1) % every || i === n - 1) {
        g.append(svg("text", { class: "tick", x: x + bw / 2, y: H - 10, "text-anchor": "middle" },
          d.partial ? "Today" : utcDay(d.date, { month: "short", day: "numeric" })));
      }
      const hit = svg("rect", { class: "hit", x: x0, y: m.t, width: band, height: ph, tabindex: "0",
        "aria-label": `${utcDay(d.date, { month: "long", day: "numeric" })}: ${money(d.total)}${d.partial ? " (partial day)" : ""}` });
      hit.addEventListener("pointerenter", () => showTip(i, g, x0));
      hit.addEventListener("focus", () => showTip(i, g, x0));
      hit.addEventListener("pointerleave", () => hideTip(g));
      hit.addEventListener("blur", () => hideTip(g));
      g.append(hit);
      root.append(g);
    });
    return root;
  }

  function costDailyTable(c) {
    const names = c.series.map((s) => s.name);
    return h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Date (UTC)"), h("th", { class: "num" }, "Total"), names.map((n) => h("th", { class: "num" }, n)))),
      h("tbody", {}, c.chart.slice().reverse().map((d) => h("tr", {},
        h("td", {}, utcDay(d.date, { weekday: "short", month: "short", day: "numeric" }), d.partial ? h("span", { class: "muted" }, " (partial)") : null),
        h("td", { class: "num" }, h("b", {}, money(d.total))),
        names.map((n) => h("td", { class: "num" }, d.values[n] ? money(d.values[n]) : "—")))))));
  }

  function shareCell(pct) {
    const bar = h("span", { class: "meter" }, h("span", { class: "meter-fill" }));
    bar.firstChild.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    return h("td", { class: "share" }, bar, h("span", { class: "num-inline" }, `${pct.toFixed(1)}%`));
  }

  function panel(title, sub, body) {
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, title), sub ? h("div", { class: "muted small" }, sub) : null)),
      body);
  }

  function costServiceTable(c) {
    if (!c.by_service.length) return panel("By service", null, h("p", { class: "muted" }, "No cost in this period."));
    return panel("By service", `Last ${c.days} days`, h("div", { class: "table-wrap flat" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Service"), h("th", { class: "num" }, "Cost"), h("th", {}, "Share"), h("th", {}, "vs previous"))),
      h("tbody", {}, c.by_service.map((s) => h("tr", {},
        h("td", {}, h("span", { class: `swatch ${slotClass(s.slot)}` }), s.service),
        h("td", { class: "num" }, money(s.amount)),
        shareCell(s.share),
        h("td", { class: "small" }, deltaShort(s.delta_pct))))))));
  }
  function deltaShort(pct) {
    if (pct === null || pct === undefined) return h("span", { class: "muted" }, "new");
    if (Math.abs(pct) < 0.05) return h("span", { class: "muted" }, "0%");
    return h("span", { class: pct > 0 ? "delta-bad" : "delta-good" }, `${pct > 0 ? "▲ +" : "▼ "}${pct.toFixed(1)}%`);
  }

  function costCompartmentTable(c) {
    if (!c.by_compartment.length) return panel("By compartment", null, h("p", { class: "muted" }, "No cost in this period."));
    return panel("By compartment", "Click a compartment to drill down", h("div", { class: "table-wrap flat" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Compartment"), h("th", { class: "num" }, "Cost"), h("th", {}, "Share"))),
      h("tbody", {}, c.by_compartment.map((x) => h("tr", {},
        h("td", { title: x.path || "" }, x.drilldown
          ? h("button", { class: "link-btn", type: "button", onclick: () => setFilter({ compartment: x.id }) }, x.name)
          : h("span", { class: x.id ? "" : "muted" }, x.name)),
        h("td", { class: "num" }, money(x.amount)),
        shareCell(x.share)))))));
  }

  function costTopResources(c) {
    const typeLabel = (k) => ((S.meta && S.meta.types.find((t) => t.key === k)) || {}).label;
    const body = c.top_resources.length ? h("div", { class: "table-wrap flat" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { class: "num" }, "#"), h("th", {}, "Resource"), h("th", {}, "Service"), h("th", {}, "Compartment"),
        h("th", {}, "Daily trend"), h("th", { class: "num" }, "Cost"), h("th", {}, "Share"))),
      h("tbody", {}, c.top_resources.map((r, i) => h("tr", {},
        h("td", { class: "num muted" }, i + 1),
        h("td", { class: "name", title: r.id }, r.on_dashboard
          ? h("button", { class: "link-btn", type: "button", title: "Show in Resources", onclick: () => openResource(r.id) }, r.name)
          : h("span", {}, r.name, " ", h("button", { class: "link-btn small mono", type: "button", title: "Copy OCID", onclick: () => copy(r.id) }, "copy OCID")),
        r.type ? h("div", { class: "muted small" }, typeLabel(r.type) || "") : null),
        h("td", {}, r.service || "—"),
        h("td", {}, r.compartment_path || "—"),
        h("td", {}, sparkline(r.daily, 110, 26, `${r.name} daily cost`)),
        h("td", { class: "num" }, h("b", {}, money(r.amount))),
        shareCell(r.share)))))) : h("p", { class: "muted" }, "No resource-level cost in this period.");
    return panel("Top 10 resources by cost", `Last ${c.days} days · includes items not shown on the dashboard, such as backups`, body);
  }

  function openResource(id) {
    S.expanded.add(id);
    setFilter({ view: "", q: id, type: "", category: "", state: "", problems: false, include_terminated: true });
  }

  // --------------------------------------------------------------- actions
  function setFilter(patch) {
    Object.assign(S.filters, patch);
    S.showAll.clear();
    writeHash();
    renderFilters();
    renderView();
    loadData();
    if (S.filters.view === "cost") loadCost();
  }
  function clearFilters() {
    setFilter({ type: "", category: "", state: "", q: "", problems: false, region: "" });
    $("#search").value = "";
  }

  async function refreshNow() {
    const btn = $("#refresh-btn");
    btn.disabled = true; btn.classList.add("spinning");
    try {
      const target = S.filters.view === "cost" ? "cost" : "resources";
      const r = await api("refresh", { target }, { method: "POST", headers: { "X-OCIMON": "1" } });
      toast(r.accepted ? (target === "cost" ? "Fetching cost data" : "Refresh started") : "A refresh is already running");
    } catch (err) { handleError(err); }
    pollFast();
  }

  // Poll quickly while a refresh is running, then fall back to the slow cadence.
  let fastTimer = null;
  function pollFast() {
    clearInterval(fastTimer);
    let n = 0;
    fastTimer = setInterval(async () => {
      await loadHealth();
      const busy = S.health && (S.health.refreshing || (S.health.cost && S.health.cost.refreshing));
      if (++n > 300 || (S.health && !busy && n > 1)) clearInterval(fastTimer);
    }, 1000);
  }

  function renderPauseButton(paused) {
    const btn = $("#pause-btn");
    const label = paused ? "Resume auto-refresh" : "Pause auto-refresh";
    btn.title = label;
    btn.setAttribute("aria-label", label);
    btn.setAttribute("aria-pressed", String(!!paused));
    btn.classList.toggle("paused", !!paused);
    $("#pause-icon").setAttribute("d", paused ? "M8 5.5v13l11-6.5z" : "M9 5v14M15 5v14");
    $("#auto-refresh").classList.toggle("paused-note", !!paused);
  }

  async function togglePause() {
    const paused = !!(S.health && S.health.auto_refresh && S.health.auto_refresh.paused);
    try {
      await api("auto-refresh", { enabled: paused ? "true" : "false" }, { method: "POST", headers: { "X-OCIMON": "1" } });
      toast(paused ? "Auto-refresh resumed" : "Auto-refresh paused");
    } catch (err) { handleError(err); }
    await loadHealth();
  }

  // Poll only while the tab is visible. A hidden tab sends nothing, so the server can stop
  // calling OCI; on return we poll at once and quickly while the catch-up refresh runs.
  let healthTimer = null; let statusTimer = null;
  function startPolling() {
    stopPolling();
    healthTimer = setInterval(loadHealth, 5000);
    statusTimer = setInterval(renderStatus, 1000); // countdown + "x ago"
  }
  function stopPolling() {
    clearInterval(healthTimer); clearInterval(statusTimer); clearInterval(fastTimer);
  }
  function onVisibilityChange() {
    if (document.hidden) { stopPolling(); return; }
    startPolling();
    pollFast();
  }

  function cycleTheme() {
    const order = ["auto", "light", "dark"];
    const next = order[(order.indexOf(store.get("theme", "auto")) + 1) % 3];
    store.set("theme", next);
    applyTheme();
    toast(`Theme: ${next}`);
  }
  function applyTheme() {
    const t = store.get("theme", "auto");
    if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
    $("#theme-btn").title = `Theme: ${t}`;
  }

  function openNav() { document.body.classList.add("nav-open"); $("#scrim").hidden = false; $("#nav-toggle").setAttribute("aria-expanded", "true"); }
  function closeNav() { document.body.classList.remove("nav-open"); $("#scrim").hidden = true; $("#nav-toggle").setAttribute("aria-expanded", "false"); }

  // ------------------------------------------------------------------ init
  function bind() {
    let qTimer;
    $("#search").addEventListener("input", (e) => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => setFilter({ q: e.target.value.trim() }), 250);
    });
    $("#search").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; setFilter({ q: "" }); e.target.blur(); } });
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && !/input|select|textarea/i.test(document.activeElement.tagName)) { e.preventDefault(); $("#search").focus(); }
    });
    $("#region").addEventListener("change", (e) => setFilter({ region: e.target.value }));
    $("#type-filter").addEventListener("change", (e) => setFilter({ type: e.target.value, category: "" }));
    $("#state-filter").addEventListener("change", (e) => setFilter({ state: e.target.value }));
    $("#problems-toggle").addEventListener("change", (e) => setFilter({ problems: e.target.checked }));
    $("#terminated-toggle").addEventListener("change", (e) => setFilter({ include_terminated: e.target.checked }));
    $("#clear-filters").addEventListener("click", clearFilters);
    $("#refresh-btn").addEventListener("click", refreshNow);
    $("#pause-btn").addEventListener("click", togglePause);
    document.addEventListener("visibilitychange", onVisibilityChange);
    $("#theme-btn").addEventListener("click", cycleTheme);
    $("#errors-btn").addEventListener("click", () => {
      if (S.filters.view === "cost") setFilter({ view: "" }); // read issues live on the Resources tab
      const p = $("#errors"); p.dataset.open = p.dataset.open === "1" ? "0" : "1"; renderErrors();
      if (p.dataset.open === "1") p.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    $("#tree-collapse").addEventListener("click", () => { S.openTree.clear(); store.set("openTree", []); renderTree(); });
    $("#nav-toggle").addEventListener("click", () => (document.body.classList.contains("nav-open") ? closeNav() : openNav()));
    $("#scrim").addEventListener("click", closeNav);
    window.addEventListener("hashchange", () => { readHash(); renderFilters(); renderView(); loadData(); loadCost(); });
    for (const tab of document.querySelectorAll(".tab")) {
      tab.addEventListener("click", () => setFilter({ view: tab.dataset.view === "resources" ? "" : tab.dataset.view }));
    }
  }

  async function init() {
    applyTheme();
    readHash();
    bind();
    renderContent();
    try { await loadMeta(); } catch (err) { handleError(err); }
    renderView();
    await loadHealth();
    if (!document.hidden) startPolling();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
