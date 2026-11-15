import { api } from "../api.js";
import { empty, errorState, fmt, h, hashParams, link, loading, setHashParams, table, mount } from "../ui.js";

export async function render(view) {
  const p = hashParams();
  const form = h("form", { class: "filters", onsubmit: (e) => { e.preventDefault(); setHashParams("/dashboard", Object.fromEntries(new FormData(form))); } },
    h("label", {}, "From", h("input", { type: "date", name: "date_from", value: p.date_from || "" })),
    h("label", {}, "To", h("input", { type: "date", name: "date_to", value: p.date_to || "" })),
    h("button", { type: "submit" }, "Apply"));
  const body = h("div", {}, loading("Aggregating on the server…"));
  view.append(h("h1", {}, "Dashboard"), h("p", { class: "sub" }, "Open findings, recorded and unexplained loss, and exception volumes across the stores you are authorised for."), form, body);
  let d;
  try {
    d = await api.dashboard({ date_from: p.date_from ? p.date_from + "T00:00:00Z" : undefined, date_to: p.date_to ? p.date_to + "T23:59:59Z" : undefined });
  } catch (e) { return body.replaceChildren(errorState(e, () => render(mount(view) || view))); }
  const t = d.totals;
  const kpi = (label, value, hint, href) => h("div", { class: "card kpi" }, h("div", { class: "label" }, label),
    h("div", { class: "value" }, href ? link(href, value) : value), hint ? h("div", { class: "hint" }, hint) : null);
  const maxVal = Math.max(1, ...d.stores.map((s) => s.unexplained_value_cents));
  body.replaceChildren(
    h("div", { class: "grid kpis" },
      kpi("Unexplained loss (open)", fmt.money(t.unexplained_value_cents), `${fmt.int(t.unexplained_units)} units in open shrinkage alerts`, "#/alerts?alert_class=SHRINKAGE&status=OPEN"),
      kpi("Recorded write-offs", fmt.money(t.recorded_loss_value_cents), `${fmt.int(t.recorded_loss_units)} units via approved adjustments`),
      kpi("Open cases", fmt.int(t.open_cases), `${t.open_shrinkage} shrinkage · ${t.open_pattern} pattern · ${t.open_transfer} transfer · ${t.open_data_quality} data quality`, "#/investigations?status=OPEN"),
      kpi("Quarantined rows", fmt.int(t.quarantined_rows), "never posted to stock", "#/imports"),
      kpi("Counts reconciled", fmt.int(t.counts), `${fmt.int(t.timing_reviews)} in timing review`, "#/reconciliations")),
    h("div", { class: "grid two", style: "margin-top:14px" },
      h("section", {}, h("h2", {}, "Exceptions by error code"),
        d.exceptions_by_code.length ? table([{ label: "Error code", render: (r) => h("code", {}, r.error_code) }, { label: "Rows", num: true, render: (r) => fmt.int(r.n) }], d.exceptions_by_code)
          : empty("No quarantined rows in this period.")),
      h("section", {}, h("h2", {}, "How to read this"),
        h("ul", { class: "meta" },
          h("li", {}, "Unexplained loss only counts shortages left after quarantined rows and approved write-offs are taken into account."),
          h("li", {}, "Data-quality findings (timing review) are not counted as shrinkage."),
          h("li", {}, "Recorded write-offs are approved adjustments; they are not deducted again in reconciliation.")))),
    h("h2", {}, "Stores"),
    d.stores.length ? table([
      { label: "Store", render: (s) => link(`#/reconciliations?store_id=${s.store_id}`, s.store_id) },
      { label: "Name", key: "store_name" },
      { label: "Region", key: "region_code" },
      { label: "Unexplained (open)", num: true, render: (s) => h("div", { class: "row", style: "justify-content:flex-end" },
        h("div", { class: "bar", "aria-hidden": "true", style: "width:90px" }, h("span", { style: `width:${Math.round((s.unexplained_value_cents / maxVal) * 100)}%` })),
        fmt.money(s.unexplained_value_cents)) },
      { label: "Recorded write-offs", num: true, render: (s) => fmt.money(s.recorded_loss_value_cents) },
      { label: "Open cases", num: true, render: (s) => link(`#/alerts?store_id=${s.store_id}&status=OPEN`, fmt.int(s.open_cases)) },
      { label: "Quarantined", num: true, render: (s) => fmt.int(s.quarantined_rows) },
      { label: "Counts", num: true, render: (s) => fmt.int(s.counts) },
      { label: "Timing reviews", num: true, render: (s) => fmt.int(s.timing_reviews) },
    ], d.stores) : empty("You are not authorised for any store."));
}
