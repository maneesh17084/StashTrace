import { api } from "../api.js";
import { badge, empty, errorState, fmt, h, hashParams, link, loading, pager, setHashParams, storeSelect, table, mount, delta } from "../ui.js";

export async function renderList(view) {
  const p = hashParams();
  const page = Number(p.page || 1);
  const form = h("form", { class: "filters", onsubmit: (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(form));
    setHashParams("/reconciliations", { ...f, page: 1 });
  } });
  form.append(
    h("label", {}, "Store", await storeSelect(api, "store_id", p.store_id)),
    h("label", {}, "SKU", h("input", { name: "sku_id", value: p.sku_id || "", placeholder: "SKU-000001", pattern: "SKU-\\d{6}" })),
    h("label", {}, "Cutoff from", h("input", { type: "date", name: "date_from", value: p.date_from || "" })),
    h("label", {}, "Cutoff to", h("input", { type: "date", name: "date_to", value: p.date_to || "" })),
    h("label", {}, "Variance", h("select", { name: "variance" },
      h("option", { value: "" }, "Any"), h("option", { value: "short", selected: p.variance === "short" }, "Shortage"),
      h("option", { value: "over", selected: p.variance === "over" }, "Overage"),
      h("option", { value: "zero", selected: p.variance === "zero" }, "Zero"))),
    h("label", {}, "Basis", h("select", { name: "basis_status" },
      h("option", { value: "" }, "Any"), h("option", { value: "RELIABLE", selected: p.basis_status === "RELIABLE" }, "Reliable"),
      h("option", { value: "TIMING_REVIEW", selected: p.basis_status === "TIMING_REVIEW" }, "Timing review"))),
    h("button", { type: "submit" }, "Apply"));
  const results = h("div", {}, loading());
  view.append(h("h1", {}, "Reconciliations"),
    h("p", { class: "sub" }, "Current version of each count's expected-vs-physical calculation. Filtering and paging happen on the server."),
    form, results);
  try {
    const params = { ...p, page, page_size: 50 };
    if (params.date_to) params.date_to += "T23:59:59Z";
    if (params.date_from) params.date_from += "T00:00:00Z";
    const data = await api.reconciliations(params);
    if (!data.items.length) return results.replaceChildren(empty("No reconciliations match these filters."));
    results.replaceChildren(
      table([
        { label: "Reconciliation", render: (r) => link(`#/reconciliations/${r.recon_id}`, r.recon_id) },
        { label: "Count", key: "count_id" },
        { label: "Store", key: "store_id" },
        { label: "SKU", render: (r) => h("span", { title: r.sku_name }, r.sku_id) },
        { label: "Cutoff", render: (r) => fmt.ts(r.count_cutoff_at) },
        { label: "Expected", num: true, render: (r) => fmt.int(r.expected_qty) },
        { label: "Counted", num: true, render: (r) => fmt.int(r.counted_qty) },
        { label: "Variance", num: true, render: (r) => delta(r.variance_qty) },
        { label: "Value", num: true, render: (r) => fmt.money(r.variance_value_cents) },
        { label: "Version", num: true, key: "version" },
        { label: "Basis", render: (r) => badge(r.basis_status) },
      ], data.items),
      pager(data, (n) => setHashParams("/reconciliations", { ...p, page: n })));
  } catch (e) {
    results.replaceChildren(errorState(e, () => renderList(mount(view) || view)));
  }
}

export async function renderDetail(view, id, mpage = 1) {
  mount(view, loading(`Loading ${id}…`));
  let r;
  try {
    r = await api.reconciliation(id, { movements_page: mpage, movements_page_size: 100 });
  } catch (e) {
    return mount(view, errorState(e, () => renderDetail(view, id)));
  }
  const line = (label, value, op = "") => [h("span", { class: "op" }, `${op} ${label}`), h("span", { class: "num" }, fmt.int(value))];
  const eq = h("div", { class: "equation", role: "table", "aria-label": "Stock equation" },
    ...line("Opening balance", r.opening_qty),
    ...line("Receipts", r.receipts_qty, "+"),
    ...line("Transfer receipts", r.transfer_in_qty, "+"),
    ...line("Restocked returns", r.restocked_returns_qty, "+"),
    ...line("Sales", r.sales_qty, "−"),
    ...line("Transfer dispatches", r.transfer_out_qty, "−"),
    ...line("Signed approved adjustments", r.adjustments_qty, "+"),
    h("span", { class: "total" }, "= Expected stock"), h("span", { class: "total num" }, fmt.int(r.expected_qty)),
    h("span", {}, "Physical count"), h("span", { class: "num" }, fmt.int(r.counted_qty)),
    h("span", { class: "total" }, "Variance (count − expected)"), h("span", { class: "total num" }, delta(r.variance_qty)));
  const versions = table([
    { label: "Version", render: (v) => v.recon_id === r.recon_id ? h("strong", {}, `v${v.version}`) : link(`#/reconciliations/${v.recon_id}`, `v${v.version}`) },
    { label: "Trigger", key: "trigger" },
    { label: "Expected", num: true, render: (v) => fmt.int(v.expected_qty) },
    { label: "Variance", num: true, render: (v) => delta(v.variance_qty) },
    { label: "Ledger watermark", num: true, render: (v) => fmt.int(v.ledger_watermark_seq) },
    { label: "Basis", render: (v) => badge(v.basis_status) },
    { label: "Computed", render: (v) => fmt.ts(v.created_at) },
  ], r.versions);
  const mv = r.movements;
  const movTable = mv.items.length ? table([
    { label: "Movement", key: "movement_id" },
    { label: "Type", render: (m) => m.movement_type === "REVERSAL" ? `REVERSAL of ${m.reverses_movement_id}` : m.movement_type },
    { label: "Bucket", key: "bucket" },
    { label: "Qty", num: true, render: (m) => fmt.signed(m.quantity) },
    { label: "Stock effect", num: true, render: (m) => fmt.signed(m.stock_qty) },
    { label: "Occurred", render: (m) => fmt.ts(m.occurred_at) },
    { label: "Received", render: (m) => fmt.ts(m.received_at) },
    { label: "Actor (role)", render: (m) => `${m.actor_id} (${m.actor_role_code})` },
    { label: "Reference", render: (m) => m.source_doc_id || m.transfer_id || m.reason_code || "–" },
    { label: "Approved by", render: (m) => m.approved_by || "–" },
  ], mv.items) : empty("No ledger movements before this cutoff (opening balance only).");
  const notes = (r.basis_notes || []).map((n) => h("li", {}, `${n.code}: ${Object.entries(n).filter(([k]) => k !== "code").map(([k, v]) => `${k}=${v}`).join(", ")}`));
  mount(view, 
    h("p", {}, link("#/reconciliations", "← Reconciliations")),
    h("h1", {}, `${r.recon_id} · ${r.store_id} / ${r.sku_id}`),
    h("p", { class: "sub" }, `${r.sku_name} at ${r.store_name}. Count ${r.count_id}, version ${r.version}${r.is_current ? " (current)" : " (superseded)"}.`),
    h("div", { class: "grid two" },
      h("section", { class: "card", "aria-labelledby": "eqh" }, h("h2", { id: "eqh" }, "How expected stock was calculated"), eq,
        h("p", { class: "meta" }, `Counts never post stock. Approved write-offs are already in adjustments and are not deducted again. Variance value ${fmt.money(r.variance_value_cents)} at standard cost.`)),
      h("section", { class: "card" }, h("h2", {}, "Count basis"),
        h("p", {}, badge(r.basis_status)),
        h("dl", { class: "kv" },
          h("dt", {}, "Counted at"), h("dd", {}, fmt.ts(r.counted_at)),
          h("dt", {}, "Cutoff"), h("dd", {}, fmt.ts(r.count_cutoff_at)),
          h("dt", {}, "Submitted"), h("dd", {}, fmt.ts(r.submitted_at)),
          h("dt", {}, "Counted by"), h("dd", {}, `${r.counted_by} (${r.counter_role_code})`),
          h("dt", {}, "Shift"), h("dd", {}, r.shift_id || "–"),
          h("dt", {}, "Ledger watermark"), h("dd", {}, `ledger_seq ≤ ${fmt.int(r.ledger_watermark_seq)} · ${fmt.int(r.movement_count)} entries`),
          h("dt", {}, "Late arrivals since"), h("dd", {}, r.late_movements_after_watermark
            ? h("span", { class: "badge warn" }, `${r.late_movements_after_watermark} movement(s) posted after this version`)
            : "None")),
        notes.length ? h("div", {}, h("h3", {}, "Timing contradictions"), h("ul", {}, notes)) : null,
        r.promotions.length ? h("div", {}, h("h3", {}, "Promotions on this item"),
          h("ul", {}, r.promotions.map((p) => h("li", {}, `${p.promotion_id} ${p.promo_name} (${p.start_date} – ${p.end_date}, ${p.discount_pct}% off)`)))) : null,
        r.alerts.length ? h("div", {}, h("h3", {}, "Alerts"), h("ul", {}, r.alerts.map((a) => h("li", {}, link(`#/alerts/${a.alert_id}`, a.alert_id), ` ${a.alert_type} `, badge(a.severity))))) : null)),
    h("h2", {}, "Versions"), versions,
    h("h2", {}, `Contributing movements (${fmt.int(mv.total)})`),
    h("p", { class: "meta" }, "Rows with occurred_at ≤ cutoff and ledger_seq ≤ watermark. Reversals count in the bucket of the entry they reverse."),
    movTable,
    mv.total > mv.page_size ? pager(mv, (n) => renderDetail(view, id, n)) : null);
}
