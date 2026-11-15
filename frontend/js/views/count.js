import { api } from "../api.js";
import { session } from "../session.js";
import { errorState, h, link, loading, storeSelect, success } from "../ui.js";

export async function render(view) {
  const now = new Date();
  const isoDate = now.toISOString().slice(0, 10);
  const result = h("div", { "aria-live": "polite" });
  const shiftSel = h("select", { name: "shift_id", required: true }, h("option", { value: "" }, "Pick store and date first"));
  const skuInput = h("input", { name: "sku_id", required: true, pattern: "SKU-\\d{6}", placeholder: "SKU-000001", list: "sku-list", autocomplete: "off" });
  const skuList = h("datalist", { id: "sku-list" });
  const storeSel = await storeSelect(api, "store_id", "", { required: true, allowAll: false });
  storeSel.prepend(h("option", { value: "", selected: true }, "Choose a store"));
  const dateInput = h("input", { type: "date", name: "count_date", required: true, value: isoDate });
  const timeInput = h("input", { type: "time", name: "count_time", required: true, step: 60, value: now.toISOString().slice(11, 16) });
  const qty = h("input", { type: "number", name: "counted_qty", required: true, min: 0, max: 1000000, step: 1, inputmode: "numeric" });
  const cutoff = h("input", { type: "time", name: "cutoff_time", step: 60, placeholder: "same as count" });

  async function refreshShifts() {
    shiftSel.replaceChildren(h("option", { value: "" }, "Loading shifts…"));
    if (!storeSel.value || !dateInput.value) return shiftSel.replaceChildren(h("option", { value: "" }, "Pick store and date first"));
    try {
      const { items } = await api.shifts({ store_id: storeSel.value, date: dateInput.value });
      shiftSel.replaceChildren(...(items.length ? items.map((s) => h("option", { value: s.shift_id }, `${s.shift_id} · ${s.shift_type} ${s.start_at.slice(11, 16)}–${s.end_at.slice(11, 16)} UTC`))
        : [h("option", { value: "" }, "No shifts on that date")]));
    } catch (e) { shiftSel.replaceChildren(h("option", { value: "" }, e.message)); }
  }
  async function refreshSkus() {
    if (!storeSel.value) return;
    try {
      const { items } = await api.skus({ store_id: storeSel.value, q: skuInput.value || undefined, page_size: 50 });
      skuList.replaceChildren(...items.map((s) => h("option", { value: s.sku_id }, `${s.sku_name} (on hand ${s.on_hand ?? "–"})`)));
    } catch { /* suggestions are optional */ }
  }
  storeSel.addEventListener("change", () => { refreshShifts(); refreshSkus(); });
  dateInput.addEventListener("change", refreshShifts);
  skuInput.addEventListener("input", () => { if (skuInput.value.length >= 4) refreshSkus(); });

  const submit = h("button", { type: "submit" }, "Submit count");
  const form = h("form", { class: "card", style: "display:grid;gap:12px;max-width:560px", novalidate: false, onsubmit: async (e) => {
    e.preventDefault();
    if (!form.reportValidity()) return;
    const f = Object.fromEntries(new FormData(form));
    const counted_at = `${f.count_date}T${f.count_time}:00Z`;
    const body = { store_id: f.store_id, sku_id: f.sku_id, counted_qty: Number(f.counted_qty), counted_at,
      count_cutoff_at: f.cutoff_time ? `${f.count_date}T${f.cutoff_time}:00Z` : counted_at, shift_id: f.shift_id,
      count_type: f.count_type };
    submit.disabled = true;
    result.replaceChildren(loading("Submitting and reconciling…"));
    try {
      const out = await api.submitCount(body);
      if (out.outcome === "POSTED") {
        result.replaceChildren(success(h("span", {}, `Count ${out.count_id} accepted. `, link(`#/reconciliations/${out.recon_id}`, `Open reconciliation ${out.recon_id}`))));
        form.reset();
      } else {
        result.replaceChildren(errorState({ code: out.error_code || out.outcome,
          message: (out.errors || []).map((x) => `${x.field}: ${x.message}`).join(" · ") || "The count was not accepted." }));
      }
    } catch (err) {
      const fields = (err.details && err.details.fields) || [];
      result.replaceChildren(errorState({ code: err.code, message: err.message + (fields.length ? " " + fields.map((x) => `${x.field}: ${x.message}`).join("; ") : "") }));
    } finally { submit.disabled = false; }
  } },
    h("label", {}, "Store", storeSel),
    h("label", {}, "SKU", skuInput), skuList,
    h("div", { class: "row" }, h("label", {}, "Count date (UTC)", dateInput), h("label", {}, "Count time (UTC)", timeInput)),
    h("label", {}, "Shift", shiftSel),
    h("label", {}, "Physical quantity (units)", qty),
    h("label", {}, "Cutoff time (optional, defaults to count time)", cutoff),
    h("label", {}, "Count type", h("select", { name: "count_type" }, ["AD_HOC", "SCHEDULED", "BLIND"].map((t) => h("option", { value: t }, t)))),
    h("p", { class: "meta" }, `Counted by ${session.me?.employee_id}. The browser checks required fields; the server re-validates authority, assortment, shift and timing.`),
    h("div", {}, submit));
  view.append(h("h1", {}, "Cycle count entry"),
    h("p", { class: "sub" }, "A count is a snapshot. It never posts stock; the backend reconciles it against the ledger at the cutoff."),
    form, h("div", { style: "margin-top:14px" }, result));
}
