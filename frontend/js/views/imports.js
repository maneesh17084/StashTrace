import { api } from "../api.js";
import { badge, empty, errorState, fmt, h, hashParams, link, loading, pager, setHashParams, success, table, toast, mount } from "../ui.js";

export async function renderList(view) {
  const p = hashParams();
  const status = h("div", { "aria-live": "polite" });
  const file = h("input", { type: "file", name: "file", accept: ".csv,text/csv", required: true });
  const form = h("form", { class: "card row", onsubmit: async (e) => {
    e.preventDefault();
    if (!form.reportValidity()) return;
    const fd = new FormData(form);
    status.replaceChildren(loading("Uploading… rows are saved before validation starts."));
    try {
      const out = await api.upload(fd);
      status.replaceChildren(success(h("span", {}, `Batch ${out.batch_id} received (${fmt.int(out.received_count)} rows, job ${out.job_id ?? "inline"}). `,
        link(`#/imports/${out.batch_id}`, "Track progress"))));
      form.reset();
    } catch (err) {
      status.replaceChildren(errorState(err));
    }
  } },
    h("label", {}, "CSV file", file),
    h("label", {}, "Contents", h("select", { name: "batch_kind" }, h("option", { value: "MOVEMENTS" }, "Stock movements"), h("option", { value: "COUNTS" }, "Cycle counts"))),
    h("label", {}, "Default source system", h("select", { name: "source_system" }, ["STORE_OPS", "POS", "WMS"].map((s) => h("option", { value: s }, s)))),
    h("button", { type: "submit" }, "Upload"));
  const list = h("div", {}, loading());
  view.append(h("h1", {}, "Imports and exceptions"),
    h("p", { class: "sub" }, "Raw rows are kept separately from the trusted ledger. Failed rows are quarantined with an error code and never touch stock."),
    form, status, h("h2", {}, "Recent batches"), list);
  try {
    const data = await api.imports({ page: p.page || 1, page_size: 25 });
    if (!data.items.length) return list.replaceChildren(empty("No batches yet. Upload a CSV above."));
    list.replaceChildren(table([
      { label: "Batch", render: (b) => link(`#/imports/${b.batch_id}`, b.batch_id) },
      { label: "File", key: "file_name" },
      { label: "Kind", key: "batch_kind" },
      { label: "Status", render: (b) => badge(b.status) },
      { label: "Received", num: true, render: (b) => fmt.int(b.received_count) },
      { label: "Posted", num: true, render: (b) => fmt.int(b.posted_count) },
      { label: "Quarantined", num: true, render: (b) => fmt.int(b.quarantined_count) },
      { label: "Duplicate", num: true, render: (b) => fmt.int(b.duplicate_count) },
      { label: "Conflict", num: true, render: (b) => fmt.int(b.conflict_count) },
      { label: "Submitted", render: (b) => `${fmt.ts(b.submitted_at)} by ${b.submitted_by}` },
    ], data.items), pager(data, (n) => setHashParams("/imports", { page: n })));
  } catch (e) { list.replaceChildren(errorState(e)); }
}

export async function renderDetail(view, id) {
  const p = hashParams();
  mount(view, loading());
  let b;
  try { b = await api.importBatch(id); } catch (e) { return mount(view, errorState(e, () => renderDetail(view, id))); }
  const kpi = (label, v, hint) => h("div", { class: "card kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, fmt.int(v)), hint ? h("div", { class: "hint" }, hint) : null);
  const progress = b.received_count ? Math.round((b.processed_count / b.received_count) * 100) : 100;
  const rowsBox = h("div", {}, loading("Loading rows…"));
  const outcomeSel = h("select", { name: "outcome", onchange: () => setHashParams(`/imports/${id}`, { outcome: outcomeSel.value, error_code: codeSel.value }) },
    ["QUARANTINED", "CONFLICT", "DUPLICATE", "POSTED", ""].map((o) => h("option", { value: o, selected: (p.outcome ?? "QUARANTINED") === o }, o || "All outcomes")));
  const codeSel = h("select", { name: "error_code", onchange: () => setHashParams(`/imports/${id}`, { outcome: outcomeSel.value, error_code: codeSel.value }) },
    h("option", { value: "" }, "Any error code"), Object.keys(b.errors_by_code).map((c) => h("option", { value: c, selected: p.error_code === c }, `${c} (${b.errors_by_code[c]})`)));
  mount(view, 
    h("p", {}, link("#/imports", "← Imports")),
    h("h1", {}, `${b.batch_id} · ${b.file_name}`),
    h("p", { class: "sub" }, badge(b.status), ` ${b.batch_kind} · ${progress}% processed · submitted ${fmt.ts(b.submitted_at)} by ${b.submitted_by}`,
      b.status === "PROCESSING" || b.status === "RECEIVED" ? h("button", { class: "secondary", style: "margin-left:10px", onclick: () => renderDetail(view, id) }, "Refresh") : null),
    b.error_message ? errorState({ code: "BATCH_FAILED", message: b.error_message }) : null,
    h("div", { class: "grid kpis" }, kpi("Received", b.received_count), kpi("Posted", b.posted_count, "in the trusted ledger"),
      kpi("Quarantined", b.quarantined_count, "rejected with an error code"), kpi("Duplicate", b.duplicate_count, "safe replays, no effect"),
      kpi("Conflict", b.conflict_count, "same ID, different payload")),
    h("h2", {}, "Rows"), h("form", { class: "filters", onsubmit: (e) => e.preventDefault() }, h("label", {}, "Outcome", outcomeSel), h("label", {}, "Error code", codeSel)),
    rowsBox);
  try {
    const data = await api.importRows(id, { outcome: p.outcome ?? "QUARANTINED", error_code: p.error_code, page: p.page || 1, page_size: 50 });
    if (!data.items.length) return rowsBox.replaceChildren(empty("No rows with this outcome."));
    const header = data.header.split(",");
    rowsBox.replaceChildren(table([
      { label: "Row", num: true, key: "row_number" },
      { label: "Outcome", render: (r) => badge(r.outcome) },
      { label: "Source event", key: "source_event_id" },
      { label: "Store (submitted → authoritative)", render: (r) => r.authoritative_store_id && r.authoritative_store_id !== r.submitted_store_id ? `${r.submitted_store_id} → ${r.authoritative_store_id}` : (r.submitted_store_id || "–") },
      { label: "Errors", render: (r) => r.errors.length ? h("ul", { style: "margin:0;padding-left:16px" }, r.errors.map((x) => h("li", {}, h("code", {}, x.code), ` ${x.field ? x.field + ": " : ""}${x.message}`))) : "–" },
      { label: "Linked", render: (r) => r.corrected_by_row_id ? `corrected by row ${r.corrected_by_row_id}` : (r.corrects_row_id ? `corrects row ${r.corrects_row_id}` : (r.movement_id || r.count_id || "–")) },
      { label: "", render: (r) => r.outcome === "QUARANTINED" && !r.corrected_by_row_id ? h("button", { class: "secondary", onclick: () => openCorrection(r, header) }, "Correct…") : null },
    ], data.items), pager(data, (n) => setHashParams(`/imports/${id}`, { outcome: p.outcome, error_code: p.error_code, page: n })));
  } catch (e) { rowsBox.replaceChildren(errorState(e)); }
}

function openCorrection(row, header) {
  const values = parseCsvLine(row.raw_line);
  const original = Object.fromEntries(header.map((k, i) => [k, values[i] ?? ""]));
  const editable = ["store_id", "reason_code", "approved_by", "actor_id", "shift_id", "source_doc_id", "transfer_id", "quantity", "disposition", "counted_qty", "counted_by", "count_cutoff_at"].filter((k) => k in original);
  const dlg = h("dialog", { "aria-labelledby": "corr-h" });
  const out = h("div", { "aria-live": "polite" });
  const form = h("form", { method: "dialog", style: "display:grid;gap:10px;min-width:min(520px,90vw)", onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    for (const k of editable) {
      const v = form.elements[k].value.trim();
      if (v !== (original[k] || "")) body[k] = ["quantity", "counted_qty"].includes(k) ? Number(v) : v;
    }
    out.replaceChildren(loading("Submitting correction…"));
    try {
      const res = await api.correctRow(row.row_id, body);
      if (res.outcome === "POSTED") { toast(`Correction posted as ${res.movement_id || res.count_id}`); dlg.close(); dlg.remove(); window.dispatchEvent(new HashChangeEvent("hashchange")); }
      else out.replaceChildren(errorState({ code: res.error_code || res.outcome, message: res.errors.map((x) => x.message).join(" · ") }));
    } catch (err) { out.replaceChildren(errorState(err)); }
  } },
    h("h2", { id: "corr-h" }, `Correct row ${row.row_number} (${row.source_event_id})`),
    h("p", { class: "meta" }, "The original row stays quarantined and is linked to this new submission."),
    editable.map((k) => h("label", {}, k, h("input", { name: k, value: original[k] || "" }))),
    out,
    h("div", { class: "row" }, h("button", { type: "submit" }, "Submit correction"), h("button", { type: "button", class: "secondary", onclick: () => { dlg.close(); dlg.remove(); } }, "Cancel")));
  dlg.append(form);
  document.body.append(dlg);
  dlg.showModal();
}

function parseCsvLine(line) {
  const out = []; let cur = ""; let q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) { if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c; }
    else if (c === '"') q = true; else if (c === ",") { out.push(cur); cur = ""; } else cur += c;
  }
  out.push(cur);
  return out;
}
