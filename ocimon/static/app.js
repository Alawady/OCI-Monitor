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
    filters: { compartment: "", region: "", type: "", category: "", state: "", q: "", problems: false, include_terminated: false },
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
  }
  function writeHash() {
    const f = S.filters; const p = new URLSearchParams();
    for (const k of ["compartment", "region", "type", "category", "state", "q"]) if (f[k]) p.set(k, f[k]);
    if (f.problems) p.set("problems", "1");
    if (f.include_terminated) p.set("terminated", "1");
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
      const [summary, tree, res] = await Promise.all([
        api("summary", baseParams()),
        api("compartments", { region: S.filters.region, include_terminated: S.filters.include_terminated }),
        api("resources", fullParams()),
      ]);
      if (seq !== loadSeq) return; // a newer request superseded this one
      S.summary = summary; S.tree = tree.tree; S.items = res.items; S.loadedAt = res.last_refresh;
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
      const health = await api("health");
      setConnection(true);
      const changed = health.last_refresh !== (S.health && S.health.last_refresh);
      S.health = health;
      renderStatus();
      renderErrors();
      if (changed && health.last_refresh) {
        if (!S.meta || !S.meta.regions.length) await loadMeta();
        await loadData();
      } else if (!health.last_refresh) {
        renderContent();
      }
    } catch (err) {
      handleError(err);
    }
  }

  async function loadMeta() {
    S.meta = await api("meta");
    $("#mode-badge").hidden = S.meta.mode !== "demo";
    $("#tenancy").textContent = S.meta.tenancy_name || "";
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
    const upd = $("#updated");
    if (hl.last_refresh) {
      upd.textContent = `Updated ${fmtAgo(hl.last_refresh)} · took ${hl.duration_s}s`;
      upd.title = new Date(hl.last_refresh * 1000).toLocaleString();
    } else {
      upd.textContent = hl.status === "error" ? "Refresh failed" : "Collecting…";
    }
    const auto = $("#auto-refresh");
    if (hl.refreshing) auto.textContent = "Refreshing now…";
    else if (hl.next_refresh_at) {
      auto.textContent = `Auto-refresh every ${fmtInterval(hl.refresh_interval)} · next in ${fmtDuration(hl.next_refresh_at - Date.now() / 1000)}`;
    } else auto.textContent = `Auto-refresh every ${fmtInterval(hl.refresh_interval)}`;

    const btn = $("#refresh-btn");
    btn.disabled = !!hl.refreshing;
    btn.classList.toggle("spinning", !!hl.refreshing);

    const eb = $("#errors-btn");
    const n = hl.errors.length;
    eb.hidden = n === 0;
    eb.textContent = `⚠ ${n} read issue${n === 1 ? "" : "s"}`;

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

  const COLUMNS = {
    instance: [C.shape, num("ocpus", "OCPUs"), num("memory_gb", "Memory GB"), ipList("private_ips"), ipList("public_ips"), C.ad],
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
    autonomous_database: [{ key: "workload", label: "Workload", get: det("workload") }, C.version,
      { key: "compute", label: "Compute", get: det("compute") }, num("storage_tb", "Storage TB"),
      { key: "free_tier", label: "Free tier", get: det("free_tier"), render: (r) => (r.details.free_tier ? "Yes" : "No") }],
    mysql_db_system: [C.version, C.shape, { key: "heatwave", label: "HeatWave", get: det("heatwave") },
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
  };

  function attachedCell(r) {
    const a = r.details.attached;
    if (a === null || a === undefined) return h("span", { class: "muted" }, "Unknown");
    if (!a) return h("span", { class: "tag" }, "Unattached");
    const to = rel("instance")(r);
    return to ? `Yes · ${to}` : "Yes";
  }
  function healthCell(r) { return r.health ? pill(r.health, r.health_class) : h("span", { class: "muted" }, "—"); }

  const SEVERITY = { bad: 4, warn: 3, stopped: 2, unknown: 1, ok: 0 };
  function columnsFor(type, multiRegion) {
    const cols = [
      { key: "name", label: "Name", get: (r) => r.name.toLowerCase(), render: (r) => [icon(CHEVRON, "chev"), r.name], cls: "name" },
      { key: "state", label: "State", get: (r) => SEVERITY[r.status] * 1000 + r.state.charCodeAt(0), render: (r) => pill(r.state, r.state_class) },
      ...(COLUMNS[type] || []),
      { key: "compartment", label: "Compartment", get: (r) => r.compartment_path || r.compartment_name || "" },
    ];
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

  function setSort(type, key) {
    const cur = S.sort[type];
    S.sort[type] = cur && cur.key === key ? { key, dir: cur.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" };
    store.set("sort", S.sort);
    renderContent();
  }

  const ROW_LIMIT = 200;

  function renderTable(type, items, multiRegion) {
    const cols = columnsFor(type, multiRegion);
    const sorted = sortItems(items, cols, type);
    const limited = S.showAll.has(type) ? sorted : sorted.slice(0, ROW_LIMIT);
    const s = S.sort[type];
    const thead = h("thead", {}, h("tr", {}, cols.map((c) => h("th", {
      class: `sortable ${c.num ? "num" : ""}`, scope: "col", tabindex: "0",
      "aria-sort": s && s.key === c.key ? (s.dir === "asc" ? "ascending" : "descending") : "none",
      onclick: () => setSort(type, c.key),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSort(type, c.key); } },
    }, c.label, h("span", { class: "arrow" }, s && s.key === c.key ? (s.dir === "asc" ? "▲" : "▼") : "")))));

    const tbody = h("tbody");
    for (const r of limited) {
      const open = S.expanded.has(r.id);
      const tr = h("tr", {
        class: `res-row ${r.problem ? "problem-" + r.problem : ""} ${r.state === "TERMINATED" || r.state === "DELETED" ? "terminated" : ""} ${open ? "expanded" : ""}`,
        tabindex: "0", "aria-expanded": String(open),
        onclick: (e) => { if (!e.target.closest("a,button")) toggleRow(r.id); },
        onkeydown: (e) => { if (e.key === "Enter") toggleRow(r.id); },
      }, cols.map((c) => {
        const v = c.render ? c.render(r) : (c.fmt ? c.fmt(c.get(r)) : c.num ? fmtNum(c.get(r)) : fmtValue(c.get(r)));
        return h("td", { class: [c.cls, c.num && "num", c.mono && "mono"].filter(Boolean).join(" ") || null, title: c.key === "name" ? r.name : null }, v);
      }));
      tbody.append(tr);
      if (open) tbody.append(h("tr", { class: "detail-row" }, h("td", { colspan: cols.length }, renderDetail(r))));
    }
    if (sorted.length > limited.length) {
      tbody.append(h("tr", { class: "more-row" }, h("td", { colspan: cols.length },
        h("button", { class: "link-btn", type: "button", onclick: () => { S.showAll.add(type); renderContent(); } },
          `Show all ${sorted.length} (${sorted.length - limited.length} more)`))));
    }
    return h("div", { class: "table-wrap" }, h("table", {}, thead, tbody));
  }

  function toggleRow(id) {
    if (S.expanded.has(id)) S.expanded.delete(id); else S.expanded.add(id);
    renderContent();
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
    const sets = r.details.backend_sets;
    if (sets && sets.length) {
      right.append(h("h4", {}, "Backend sets"), h("div", { class: "bs-list" }, sets.map((s) =>
        h("div", {}, pill(s.status, { OK: "ok", WARNING: "warn", CRITICAL: "bad" }[s.status] || "unknown"), " ", s.name,
          s.backends !== undefined && s.backends !== null ? h("span", { class: "muted" }, ` · ${s.backends} backends`) : null))));
    }
    const extra = Object.entries(r.details).filter(([k]) => !DETAIL_SKIP.has(k));
    if (extra.length) {
      right.append(h("h4", {}, "Details"), h("dl", { class: "kv" }, extra.map(([k, v]) =>
        kv(humanize(k), k === "size_bytes" ? fmtBytes(v) : fmtValue(v)))));
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

  // --------------------------------------------------------------- actions
  function setFilter(patch) {
    Object.assign(S.filters, patch);
    S.showAll.clear();
    writeHash();
    renderFilters();
    loadData();
  }
  function clearFilters() {
    setFilter({ type: "", category: "", state: "", q: "", problems: false, region: "" });
    $("#search").value = "";
  }

  async function refreshNow() {
    const btn = $("#refresh-btn");
    btn.disabled = true; btn.classList.add("spinning");
    try {
      const r = await api("refresh", null, { method: "POST", headers: { "X-OCIMON": "1" } });
      toast(r.accepted ? "Refresh started" : "A refresh is already running");
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
      if (++n > 300 || (S.health && !S.health.refreshing && n > 1)) clearInterval(fastTimer);
    }, 1000);
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
    $("#theme-btn").addEventListener("click", cycleTheme);
    $("#errors-btn").addEventListener("click", () => {
      const p = $("#errors"); p.dataset.open = p.dataset.open === "1" ? "0" : "1"; renderErrors();
      if (p.dataset.open === "1") p.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    $("#tree-collapse").addEventListener("click", () => { S.openTree.clear(); store.set("openTree", []); renderTree(); });
    $("#nav-toggle").addEventListener("click", () => (document.body.classList.contains("nav-open") ? closeNav() : openNav()));
    $("#scrim").addEventListener("click", closeNav);
    window.addEventListener("hashchange", () => { readHash(); renderFilters(); loadData(); });
  }

  async function init() {
    applyTheme();
    readHash();
    bind();
    renderContent();
    try { await loadMeta(); } catch (err) { handleError(err); }
    await loadHealth();
    setInterval(loadHealth, 5000);
    setInterval(renderStatus, 1000); // countdown + "x ago"
  }

  document.addEventListener("DOMContentLoaded", init);
})();
