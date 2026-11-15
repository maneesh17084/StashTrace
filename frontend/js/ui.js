// Small DOM helpers shared by every screen.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v; // only used with static strings
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === undefined || c === null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** replaceChildren that skips null/false children (DOM would render them as text). */
export function mount(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

export const fmt = {
  int: (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString()),
  signed: (n) => (n === null || n === undefined ? "–" : (n > 0 ? "+" : "") + Number(n).toLocaleString()),
  money: (cents) => (cents === null || cents === undefined ? "–" :
    (cents / 100).toLocaleString(undefined, { style: "currency", currency: "USD" })),
  ts: (s) => (s ? s.replace("T", " ").replace("Z", " UTC") : "–"),
};

const STATUS = {
  RELIABLE: ["ok", "Reliable basis"], TIMING_REVIEW: ["warn", "Timing review"],
  POSTED: ["ok", "Posted"], QUARANTINED: ["bad", "Quarantined"], DUPLICATE: ["neutral", "Duplicate"],
  CONFLICT: ["warn", "Conflict"], COMPLETED: ["ok", "Completed"], PROCESSING: ["info", "Processing"],
  RECEIVED: ["neutral", "Received"], FAILED: ["bad", "Failed"], QUEUED: ["neutral", "Queued"], RUNNING: ["info", "Running"],
  OPEN: ["bad", "Open"], ASSIGNED: ["warn", "Assigned"], IN_REVIEW: ["info", "In review"], RESOLVED: ["ok", "Resolved"],
  CLOSED: ["ok", "Closed"], HIGH: ["bad", "High"], MEDIUM: ["warn", "Medium"], LOW: ["neutral", "Low"],
  SHRINKAGE: ["bad", "Suspected shrinkage"], DATA_QUALITY: ["info", "Data quality"], PATTERN: ["warn", "Pattern"],
  TRANSFER: ["warn", "Transfer"],
};
export function badge(value) {
  const [cls, label] = STATUS[value] || ["neutral", value ?? "–"];
  return h("span", { class: `badge ${cls}` }, label);
}

export function loading(text = "Loading…") {
  return h("div", { class: "state", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), text);
}
export function empty(text) {
  return h("div", { class: "state" },
    h("span", { class: "state-icon", "aria-hidden": "true",
      html: '<svg viewBox="0 0 24 24"><path d="M3 7l9-4 9 4-9 4z"/><path d="M3 7v10l9 4 9-4V7"/></svg>' }),
    h("span", {}, text));
}

/** Signed quantity coloured by direction (shortage red, overage green); text keeps the sign. */
export function delta(n) {
  if (n === null || n === undefined) return "–";
  return h("span", { class: n < 0 ? "delta-neg" : (n > 0 ? "delta-pos" : null) }, fmt.signed(n));
}
export function errorState(err, retry) {
  return h("div", { class: "state error", role: "alert" },
    h("strong", {}, err.code ? `${err.code}: ` : "Error: "), err.message || String(err),
    retry ? h("div", { style: "margin-top:10px" }, h("button", { class: "secondary", onclick: retry }, "Try again")) : null);
}
export function success(text) { return h("div", { class: "state success", role: "status" }, text); }

export function toast(text, bad = false) {
  const t = h("div", { class: `t${bad ? " bad" : ""}` }, text);
  document.getElementById("toast").append(t);
  setTimeout(() => t.remove(), 5000);
}

// IDs (MOV-2026-00000013) and timestamps (2026-03-06 15:00:00 UTC) should never break across lines.
const UNBREAKABLE = /^([A-Z]{2,5}-[\d-]+|\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?( UTC)?)?)$/;
function keepTogether(v) {
  return typeof v === "string" && UNBREAKABLE.test(v) ? h("span", { style: "white-space:nowrap" }, v) : v;
}

/** Render a server-paginated table. columns: [{label, key|render, num}] */
export function table(columns, rows, { caption } = {}) {
  return h("div", { class: "table-wrap" },
    h("table", {},
      caption ? h("caption", { class: "sr-only" }, caption) : null,
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", { scope: "col", class: c.num ? "num" : null }, c.label)))),
      h("tbody", {}, rows.map((r) => h("tr", {}, columns.map((c) =>
        h("td", { class: c.num ? "num" : null }, keepTogether(c.render ? c.render(r) : (r[c.key] ?? "–")))))))));
}

export function pager(page, onPage) {
  const pages = Math.max(1, Math.ceil(page.total / page.page_size));
  return h("div", { class: "pager" },
    h("button", { class: "secondary", disabled: page.page <= 1, onclick: () => onPage(page.page - 1) }, "Previous"),
    `Page ${page.page} of ${pages.toLocaleString()} · ${page.total.toLocaleString()} rows`,
    h("button", { class: "secondary", disabled: page.page >= pages, onclick: () => onPage(page.page + 1) }, "Next"));
}

export function link(href, text) { return h("a", { href }, text); }

/** Read/write filter params in the hash query string so views are linkable. */
export function hashParams() {
  const q = location.hash.split("?")[1] || "";
  return Object.fromEntries(new URLSearchParams(q));
}
export function setHashParams(path, params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) p.set(k, v);
  const s = p.toString();
  location.hash = `#${path}${s ? "?" + s : ""}`;
}

export async function storeSelect(api, name, value, { required = false, allowAll = true } = {}) {
  const sel = h("select", { name, required });
  if (allowAll) sel.append(h("option", { value: "" }, "All authorised stores"));
  try {
    const { items } = await api.stores();
    for (const s of items) sel.append(h("option", { value: s.store_id, selected: s.store_id === value }, `${s.store_id} · ${s.store_name}`));
  } catch (e) { sel.append(h("option", { value: "" }, "Could not load stores")); }
  return sel;
}
