import { api } from "../api.js";
import { badge, empty, errorState, fmt, h, hashParams, link, loading, pager, setHashParams, storeSelect, table, toast, mount } from "../ui.js";

const RESOLUTIONS = ["CONFIRMED_THEFT", "COUNT_ERROR", "DATA_CORRECTION", "TIMING_CORRECTED", "TRANSFER_RECONCILED", "BENIGN_EXPLAINED", "NO_ACTION"];

export async function renderList(view) {
  const p = hashParams();
  const form = h("form", { class: "filters", onsubmit: (e) => { e.preventDefault(); setHashParams("/investigations", { ...Object.fromEntries(new FormData(form)), page: 1 }); } });
  form.append(h("label", {}, "Store", await storeSelect(api, "store_id", p.store_id)),
    h("label", {}, "Status", h("select", { name: "status" }, h("option", { value: "" }, "Any"),
      ["OPEN", "ASSIGNED", "IN_REVIEW", "RESOLVED"].map((s) => h("option", { value: s, selected: p.status === s }, s)))),
    h("button", { type: "submit" }, "Apply"));
  const results = h("div", {}, loading());
  view.append(h("h1", {}, "Investigations"), form, results);
  try {
    const data = await api.investigations({ ...p, page: p.page || 1, page_size: 50 });
    if (!data.items.length) return results.replaceChildren(empty("No cases match these filters."));
    results.replaceChildren(table([
      { label: "Case", render: (c) => link(`#/investigations/${c.investigation_id}`, c.investigation_id) },
      { label: "Status", render: (c) => badge(c.status) },
      { label: "Origin", key: "origin_type" },
      { label: "Finding", render: (c) => c.alert_id ? h("span", {}, link(`#/alerts/${c.alert_id}`, c.alert_id), ` ${c.alert_type}`) : "–" },
      { label: "Store", render: (c) => c.related_store_id ? `${c.related_store_id} → ${c.store_id}` : c.store_id },
      { label: "Assignee", render: (c) => c.assignee_id || "–" },
      { label: "Opened", render: (c) => fmt.ts(c.opened_at) },
      { label: "Resolution", render: (c) => c.resolution_code || "–" },
    ], data.items), pager(data, (n) => setHashParams("/investigations", { ...p, page: n })));
  } catch (e) { results.replaceChildren(errorState(e)); }
}

export async function renderDetail(view, id) {
  mount(view, loading());
  let c;
  try { c = await api.investigation(id); } catch (e) { return mount(view, errorState(e, () => renderDetail(view, id))); }
  const out = h("div", { "aria-live": "polite" });
  async function send(body, btn) {
    btn.disabled = true;
    out.replaceChildren(loading("Saving…"));
    try {
      const r = await api.caseEvent(id, { ...body, expected_seq: c.last_seq });
      toast(`${r.event_type}: ${r.from_status} → ${r.to_status}${r.write_off_movement_id ? ` · write-off ${r.write_off_movement_id}` : ""}`);
      return renderDetail(view, id);
    } catch (err) {
      out.replaceChildren(errorState(err));
      btn.disabled = false;
    }
  }
  const actions = h("div", { class: "grid two" });
  if (c.allowed_events.includes("ASSIGNED") && c.can.assign) {
    const sel = h("select", { name: "assignee_id", required: true }, h("option", { value: "" }, "Loading investigators…"));
    api.employees({ store_id: c.store_id, action: "INVESTIGATE" }).then(({ items }) =>
      sel.replaceChildren(h("option", { value: "" }, "Choose…"), ...items.map((e) => h("option", { value: e.employee_id, selected: e.employee_id === c.assignee_id }, `${e.employee_id} · ${e.display_name} (${e.role})`))))
      .catch((e) => sel.replaceChildren(h("option", { value: "" }, e.message)));
    const f = h("form", { class: "card", style: "display:grid;gap:8px", onsubmit: (e) => { e.preventDefault(); if (f.reportValidity()) send({ event_type: "ASSIGNED", assignee_id: sel.value, note: f.note.value || null }, f.querySelector("button")); } },
      h("h3", { style: "margin:0" }, c.assignee_id ? "Reassign" : "Assign"), h("label", {}, "Investigator", sel),
      h("label", {}, "Note (optional)", h("input", { name: "note" })), h("div", {}, h("button", { type: "submit" }, "Assign")));
    actions.append(f);
  }
  if (c.can.start_review) { // server-computed: status allows it AND this user is authorised
    const b = h("button", { onclick: () => send({ event_type: "REVIEW_STARTED" }, b) }, "Start review");
    actions.append(h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Review"), h("p", { class: "meta" }, "Moves the case to In review."), b));
  }
  if (c.allowed_events.includes("FINDING_ADDED") && c.can.investigate) {
    const f = h("form", { class: "card", style: "display:grid;gap:8px", onsubmit: (e) => { e.preventDefault(); if (f.reportValidity()) send({ event_type: "FINDING_ADDED", note: f.note.value }, f.querySelector("button")); } },
      h("h3", { style: "margin:0" }, "Add finding"), h("label", {}, "Finding", h("textarea", { name: "note", required: true, minlength: 3 })),
      h("div", {}, h("button", { type: "submit" }, "Add finding")));
    actions.append(f);
  }
  if (c.allowed_events.includes("RESOLVED") && c.can.resolve) {
    const shortage = c.alerts.find((a) => a.alert_type === "UNEXPLAINED_SHORTAGE");
    const f = h("form", { class: "card", style: "display:grid;gap:8px", onsubmit: (e) => {
      e.preventDefault();
      if (!f.reportValidity()) return;
      const wo = f.write_off_qty && f.write_off_qty.value ? Number(f.write_off_qty.value) : null;
      send({ event_type: "RESOLVED", resolution_code: f.resolution_code.value, note: f.note.value, write_off_qty: wo }, f.querySelector("button"));
    } },
      h("h3", { style: "margin:0" }, "Resolve"),
      h("label", {}, "Decision", h("select", { name: "resolution_code", required: true }, h("option", { value: "" }, "Choose…"), RESOLUTIONS.map((r) => h("option", { value: r }, r)))),
      h("label", {}, "Reason (required, at least 10 characters)", h("textarea", { name: "note", required: true, minlength: 10 })),
      shortage && c.can.approve_write_off ? h("label", {}, `Approve write-off (optional, units, max ${shortage.observed_value})`,
        h("input", { type: "number", name: "write_off_qty", min: 1, max: shortage.observed_value, step: 1 })) : null,
      h("p", { class: "meta" }, "Resolving does not change stock. An approved write-off is posted as a separate ledger entry linked to this case."),
      h("div", {}, h("button", { type: "submit" }, "Resolve case")));
    actions.append(f);
  }
  mount(view, 
    h("p", {}, link("#/investigations", "← Cases")),
    h("h1", {}, `${c.investigation_id}`),
    h("p", { class: "sub" }, badge(c.status), ` ${c.origin_type} · ${c.related_store_id ? `${c.related_store_id} → ` : ""}${c.store_id} · opened ${fmt.ts(c.opened_at)}${c.assignee_id ? ` · assigned to ${c.assignee_id}` : ""}`),
    h("h2", {}, "Linked alerts"),
    table([{ label: "Alert", render: (a) => link(`#/alerts/${a.alert_id}`, a.alert_id) }, { label: "Class", render: (a) => badge(a.alert_class) },
      { label: "Finding", key: "alert_type" }, { label: "Severity", render: (a) => badge(a.severity) }, { label: "Reason", key: "reason_text" }], c.alerts),
    c.status !== "RESOLVED" ? h("h2", {}, "Actions") : null,
    c.status !== "RESOLVED" ? (actions.childElementCount ? actions : empty("Your roles do not permit any action on this case right now.")) : null,
    out,
    h("h2", {}, "History"),
    h("ol", { class: "timeline" }, c.events.map((e) => h("li", {},
      h("div", {}, h("strong", {}, e.event_type), ` ${e.from_status ? e.from_status + " → " : ""}${e.to_status}`),
      h("div", { class: "meta" }, `${fmt.ts(e.occurred_at)} · ${e.actor_id} ${e.actor_name} (${e.actor_role_code})${e.assignee_id ? ` · assignee ${e.assignee_id}` : ""}`),
      e.resolution_code ? h("div", {}, `Decision: ${e.resolution_code}`) : null,
      e.note ? h("div", {}, e.note) : null,
      e.write_off_movement_id ? h("div", {}, `Write-off ledger entry: `, h("code", {}, e.write_off_movement_id)) : null))));
}
