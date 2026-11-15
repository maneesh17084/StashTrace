import { api, setToken } from "../api.js";
import { errorState, h, loading } from "../ui.js";

// SVG must be created from markup (document.createElement("svg") is not an SVG element).
const check = () => h("span", { "aria-hidden": "true", html: '<svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>' });

export function render(view, onDone) {
  const out = h("div", { "aria-live": "polite" });
  const btn = h("button", { type: "submit" }, "Sign in");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    if (!form.reportValidity()) return;
    btn.disabled = true;
    out.replaceChildren(loading("Signing in…"));
    try {
      const r = await api.login(form.employee_id.value.trim(), form.password.value);
      setToken(r.token);
      out.replaceChildren();
      await onDone();
    } catch (err) {
      out.replaceChildren(errorState(err));
    } finally { btn.disabled = false; }
  } },
    h("label", {}, "Employee ID", h("input", { name: "employee_id", required: true, pattern: "EMP-\\d{6}", placeholder: "EMP-000005", autocomplete: "username", autofocus: true })),
    h("label", {}, "Password", h("input", { name: "password", type: "password", required: true, autocomplete: "current-password" })),
    btn, out);
  view.replaceChildren(h("div", { class: "login-wrap" },
    h("section", { class: "login-hero", "aria-hidden": "true" },
      h("div", { class: "brand" },
        h("span", { class: "brand-mark", html: '<svg viewBox="0 0 24 24"><path d="M6 9l6-3 6 3v6l-6 3-6-3z"/><path d="M6 9l6 3 6-3M12 12v6"/></svg>' }),
        h("span", { class: "brand-text" }, "StashTrace", h("small", {}, "Inventory integrity"))),
      h("div", {},
        h("h2", {}, "Find inventory loss you can explain — and prove."),
        h("p", {}, "Reconcile physical counts against a trusted ledger, separate data-quality issues from shrinkage, and take every finding through an auditable investigation."),
        h("ul", {},
          h("li", {}, check(), "Every expected-stock number traces back to ledger rows"),
          h("li", {}, check(), "Versioned, explainable detection rules with evidence"),
          h("li", {}, check(), "Append-only case history and role-based access"))),
      h("p", { style: "font-size:12.5px;color:#93a7c7" }, "Synthetic demo data · no personal information")),
    h("section", { class: "login-panel" },
      h("div", { class: "login" },
        h("h1", {}, "Sign in"),
        h("p", { class: "sub" }, "Use your StashTrace employee ID."),
        h("div", { class: "card" }, form),
        h("p", { class: "meta", style: "margin-top:14px" }, "Demo: EMP-000005 (operations manager) with the password set when the demo was loaded — see the README.")))));
}
