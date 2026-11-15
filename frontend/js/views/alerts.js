import { api } from "../api.js";
import { session } from "../session.js";
import { badge, empty, errorState, fmt, h, hashParams, link, loading, pager, setHashParams, storeSelect, table, toast, mount } from "../ui.js";

export async function renderList(view) {
  const p = { status: "OPEN", ...hashParams() };
  const form = h("form", { class: "filters", onsubmit: (e) => {
    e.preventDefault();
    setHashParams("/alerts", { ...Object.fromEntries(new FormData(form)), page: 1 });
  } });
  const opt = (name, values, cur, all = "Any") => h("select", { name },
    h("option", { value: "" }, all), values.map(([v, l]) => h("option", { value: v, selected: cur === v }, l)));
  form.append(
    h("label", {}, "Store", await storeSelect(api, "store_id", p.store_id)),
    h("label", {}, "Class", opt("alert_class", [["SHRINKAGE", "Suspected shrinkage"], ["DATA_QUALITY", "Data quality"], ["PATTERN", "Adjustment pattern"], ["TRANSFER", "Transfer discrepancy"]], p.alert_class)),
    h("label", {}, "Severity", opt("severity", [["HIGH", "High"], ["MEDIUM", "Medium"], ["LOW", "Low"]], p.severity)),
    h("label", {}, "Status", opt("status", [["OPEN", "Open"], ["CLOSED", "Closed"]], p.status, "Any")),
    h("button", { type: "submit" }, "Apply"));
  const canRun = session.me?.actions?.includes("RUN_DETECTION") && session.me?.stores === "ALL";
  const runBox = h("span", { "aria-live": "polite" });
  const results = h("div", {}, loading());
  view.append(h("div", { class: "row" }, h("h1", {}, "Alert queue"),
    canRun ? h("span", { class: "right row" }, runBox, h("button", { class: "secondary", onclick: async (e) => {
      e.target.disabled = true; runBox.replaceChildren(loading("Queuing run…"));
      try { const r = await api.runDetection({}); runBox.replaceChildren(`Run ${r.run_id} queued`); toast(`Detection ${r.run_id} queued; the worker will process it.`); }
      catch (err) { runBox.replaceChildren(errorState(err)); } finally { e.target.disabled = false; }
    } }, "Run detection")) : null),
    h("p", { class: "sub" }, "Every alert names its rule version, threshold, observed value and evidence. Data-quality findings are kept apart from suspected shrinkage."),
    form, results);
  try {
    const data = await api.alerts({ ...p, page: p.page || 1, page_size: 50 });
    if (!data.items.length) return results.replaceChildren(empty("No alerts match these filters."));
    results.replaceChildren(table([
      { label: "Alert", render: (a) => link(`#/alerts/${a.alert_id}`, a.alert_id) },
      { label: "Class", render: (a) => badge(a.alert_class) },
      { label: "Finding", key: "alert_type" },
      { label: "Severity", render: (a) => badge(a.severity) },
      { label: "Store", render: (a) => a.related_store_id ? `${a.related_store_id} → ${a.store_id}` : a.store_id },
      { label: "SKU / ref", render: (a) => a.sku_id || a.transfer_id || "–" },
      { label: "Observed / threshold", num: true, render: (a) => `${fmt.int(a.observed_value)} / ${fmt.int(a.threshold_value)}` },
      { label: "Rule", render: (a) => `${a.rule_code} v${a.rule_version}` },
      { label: "Case", render: (a) => h("span", {}, link(`#/investigations/${a.investigation_id}`, a.investigation_id), " ", badge(a.case_status)) },
    ], data.items), pager(data, (n) => setHashParams("/alerts", { ...p, page: n })));
  } catch (e) { results.replaceChildren(errorState(e)); }
}

export async function renderDetail(view, id) {
  mount(view, loading());
  let a;
  try { a = await api.alert(id); } catch (e) { return mount(view, errorState(e, () => renderDetail(view, id))); }
  const calc = a.calculation;
  let calcView;
  if (a.alert_type === "UNEXPLAINED_SHORTAGE") {
    calcView = h("div", { class: "equation" },
      ...[["Opening", calc.opening_qty, ""], ["Receipts", calc.receipts_qty, "+"], ["Transfer receipts", calc.transfer_in_qty, "+"],
        ["Restocked returns", calc.restocked_returns_qty, "+"], ["Sales", calc.sales_qty, "−"], ["Transfer dispatches", calc.transfer_out_qty, "−"],
        ["Approved adjustments", calc.adjustments_qty, "+"]].flatMap(([l, v, o]) => [h("span", { class: "op" }, `${o} ${l}`), h("span", { class: "num" }, fmt.int(v))]),
      h("span", { class: "total" }, "= Expected"), h("span", { class: "total num" }, fmt.int(calc.expected_qty)),
      h("span", {}, "Counted"), h("span", { class: "num" }, fmt.int(calc.counted_qty)),
      h("span", {}, "Shortage"), h("span", { class: "num" }, fmt.int(calc.shortage_qty)),
      h("span", { class: "op" }, "− Explained by quarantined rows"), h("span", { class: "num" }, fmt.int(calc.explained_by_quarantined_rows_qty)),
      h("span", { class: "total" }, "= Remaining unexplained"), h("span", { class: "total num" }, fmt.int(calc.remaining_shortage_qty)),
      h("span", { class: "op" }, "Threshold"), h("span", { class: "num" }, `≥ ${calc.threshold_units}`),
      h("span", { class: "op" }, "Value at cost"), h("span", { class: "num" }, fmt.money(calc.remaining_value_cents)));
  } else if (calc.lines) {
    calcView = table([
      { label: "SKU", key: "sku_id" }, { label: "Dispatched", num: true, key: "dispatched" }, { label: "Received", num: true, key: "received" },
      { label: "Shortfall", num: true, key: "shortfall" }, { label: "Written off at sender", num: true, key: "written_off_at_sender" },
      { label: "Written off at receiver", num: true, key: "written_off_at_receiver" }, { label: "Open in transit", num: true, key: "open_in_transit" },
      { label: "Double count", render: (l) => l.double_count ? h("span", { class: "badge bad" }, "Yes") : "No" },
      { label: "Overdue", render: (l) => l.overdue ? h("span", { class: "badge warn" }, "Yes") : "No" },
    ], calc.lines);
  } else {
    calcView = h("dl", { class: "kv" }, Object.entries(calc).map(([k, v]) => [h("dt", {}, k), h("dd", {}, typeof v === "object" ? JSON.stringify(v) : String(v))]));
  }
  const evRole = (e) => e.evidence_role.replaceAll("_", " ").toLowerCase();
  mount(view, 
    h("p", {}, link("#/alerts", "← Alert queue")),
    h("h1", {}, `${a.alert_id} · ${a.alert_type}`),
    h("p", { class: "sub" }, badge(a.alert_class), " ", badge(a.severity), " ", badge(a.status), ` Case `, link(`#/investigations/${a.investigation_id}`, a.investigation_id), ` (${a.case_status})`),
    h("section", { class: "card" }, h("h2", { style: "margin-top:0" }, "Why this alert exists"), h("p", {}, a.reason_text),
      h("dl", { class: "kv" },
        h("dt", {}, "Rule"), h("dd", {}, `${a.rule.rule_code} v${a.rule.rule_version} — ${a.rule.description}`),
        h("dt", {}, "Parameters"), h("dd", {}, h("code", {}, JSON.stringify(a.rule.params))),
        h("dt", {}, "Store"), h("dd", {}, a.related_store_id ? `${a.related_store_id} → ${a.store_id}` : a.store_id),
        h("dt", {}, "Detection run"), h("dd", {}, `${a.first_run_id} (first); seen in ${a.detections.length} run(s)`),
        h("dt", {}, "Case key"), h("dd", {}, h("code", {}, a.case_key)),
        a.recon_id ? [h("dt", {}, "Reconciliation"), h("dd", {}, link(`#/reconciliations/${a.recon_id}`, a.recon_id))] : null)),
    h("h2", {}, "Calculation"), h("div", { class: "card" }, calcView),
    h("h2", {}, `Evidence (${a.evidence.length})`),
    a.evidence.length ? table([
      { label: "Role", render: evRole },
      { label: "Type", key: "evidence_type" },
      { label: "Record", render: (e) => e.recon_id ? link(`#/reconciliations/${e.recon_id}`, e.recon_id)
        : (e.movement_id || e.count_id || (e.import_row_id ? `import row ${e.import_row_id} (${e.import_batch_id})` : e.transfer_id) || "–") },
      { label: "Qty", num: true, render: (e) => fmt.signed(e.qty) },
      { label: "When", render: (e) => fmt.ts(e.occurred_at) },
      { label: "Actor / approver", render: (e) => e.actor_id ? `${e.actor_id}${e.approved_by ? " / " + e.approved_by : ""}` : "–" },
      { label: "Note", render: (e) => e.note || e.error_code || "–" },
    ], a.evidence) : empty("No evidence rows."));
}
