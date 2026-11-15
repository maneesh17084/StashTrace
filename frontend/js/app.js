// Hash router and application shell (navigation, breadcrumbs, user chip, API status).
import { api, getToken, setToken } from "./api.js";
import { h, mount } from "./ui.js";
import * as dashboard from "./views/dashboard.js";
import * as recon from "./views/reconciliation.js";
import * as count from "./views/count.js";
import * as imports from "./views/imports.js";
import * as alerts from "./views/alerts.js";
import * as cases from "./views/investigation.js";
import * as login from "./views/login.js";
import { session } from "./session.js";

// [pattern, view, nav key, section label for the breadcrumb]
const routes = [
  [/^\/dashboard$/, dashboard.render, "dashboard", "Dashboard"],
  [/^\/reconciliations$/, recon.renderList, "reconciliations", "Reconciliations"],
  [/^\/reconciliations\/(REC-[\d-]+)$/, recon.renderDetail, "reconciliations", "Reconciliations"],
  [/^\/counts\/new$/, count.render, "counts", "Count entry"],
  [/^\/imports$/, imports.renderList, "imports", "Imports"],
  [/^\/imports\/(IMP-[\d-]+)$/, imports.renderDetail, "imports", "Imports"],
  [/^\/alerts$/, alerts.renderList, "alerts", "Alerts"],
  [/^\/alerts\/(SHR-[\d-]+)$/, alerts.renderDetail, "alerts", "Alerts"],
  [/^\/investigations$/, cases.renderList, "investigations", "Cases"],
  [/^\/investigations\/(INV-[\d-]+)$/, cases.renderDetail, "investigations", "Cases"],
];

const ROLE_LABEL = {
  ADMIN: "Administrator", OPS_MANAGER: "Operations manager", ANALYST: "Analyst", LOSS_PREVENTION: "Loss prevention",
  STORE_MANAGER: "Store manager", STOCK_CLERK: "Stock clerk", CASHIER: "Cashier",
};

function initials(name = "") {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
}

function renderWho() {
  const who = document.getElementById("who");
  who.replaceChildren();
  if (!session.me) return;
  const me = session.me;
  const role = me.roles.map((r) => ROLE_LABEL[r] || r).join(", ");
  const scope = me.stores === "ALL" ? "All stores" : `${me.stores.length} store${me.stores.length === 1 ? "" : "s"}`;
  who.append(
    h("div", { class: "person", title: `${me.employee_id} · ${role} · ${scope}` },
      h("div", { class: "person-text" }, h("b", {}, me.display_name), h("span", {}, `${role} · ${scope}`)),
      h("div", { class: "avatar", "aria-hidden": "true" }, initials(me.display_name))),
    h("button", { class: "secondary", onclick: () => { setToken(null); session.me = null; location.hash = "#/login"; } }, "Sign out"));
}

function setCrumbs(section, detail) {
  const c = document.getElementById("crumbs");
  mount(c,
    detail ? h("span", {}, section) : h("strong", {}, section),
    detail ? h("span", { class: "sep", "aria-hidden": "true" }, "/") : null,
    detail ? h("strong", {}, detail) : null);
}

function setNavOpen(open) {
  document.body.classList.toggle("nav-open", open);
  document.getElementById("scrim").hidden = !open;
  document.getElementById("menu-toggle").setAttribute("aria-expanded", String(open));
}

async function renderSysinfo() {
  const el = document.getElementById("sysinfo");
  try {
    const r = await fetch("/api/v1/health").then((x) => x.json());
    mount(el, h("div", {}, h("span", { class: "dot", "aria-hidden": "true" }), "API online"),
      h("div", {}, `Schema ${r.schema_revision} · ${r.jobs_pending} job${r.jobs_pending === 1 ? "" : "s"} queued`),
      r.jobs_failed ? h("div", { style: "color:#fca5a5" }, `${r.jobs_failed} failed job(s)`) : null);
  } catch {
    el.replaceChildren(h("div", {}, h("span", { class: "dot down", "aria-hidden": "true" }), "API unreachable"));
  }
}

async function route() {
  setNavOpen(false);
  const view = document.getElementById("view");
  const path = (location.hash.slice(1) || "/dashboard").split("?")[0];
  if (path === "/login" || !getToken()) {
    document.body.classList.add("signed-out");
    document.getElementById("nav").hidden = true;
    document.title = "Sign in · StashTrace";
    session.me = null;
    renderWho();
    return login.render(view, async () => { await loadMe(); location.hash = "#/dashboard"; });
  }
  document.body.classList.remove("signed-out");
  if (!session.me) await loadMe();
  document.getElementById("nav").hidden = false;
  renderSysinfo();
  for (const [re, fn, nav, section] of routes) {
    const m = path.match(re);
    if (m) {
      document.querySelectorAll("#nav a").forEach((a) => {
        if (a.dataset.nav === nav) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
      });
      setCrumbs(section, m[1]);
      document.title = `${m[1] || section} · StashTrace`;
      view.replaceChildren();
      document.getElementById("main").scrollTo?.(0, 0);
      window.scrollTo(0, 0);
      return fn(view, ...m.slice(1));
    }
  }
  setCrumbs("Not found");
  view.replaceChildren(h("div", { class: "state" }, "Page not found. ", h("a", { href: "#/dashboard" }, "Go to the dashboard")));
}

async function loadMe() {
  try { session.me = await api.me(); } catch { session.me = null; }
  renderWho();
}

document.getElementById("menu-toggle").addEventListener("click", () => setNavOpen(!document.body.classList.contains("nav-open")));
document.getElementById("scrim").addEventListener("click", () => setNavOpen(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setNavOpen(false); });
window.addEventListener("hashchange", route);
window.addEventListener("stashtrace:unauthenticated", () => { location.hash = "#/login"; });
route();
