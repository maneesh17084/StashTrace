// Shared API client. All official numbers come from the backend; this module only transports them.
const BASE = "/api/v1";
const TOKEN_KEY = "stashtrace.token";

export class ApiError extends Error {
  constructor(status, body) {
    const err = (body && body.error) || {};
    super(err.message || `Request failed (${status})`);
    this.status = status;
    this.code = err.code || "HTTP_" + status;
    this.details = err.details || {};
  }
}

export function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(token) {
  try { token ? sessionStorage.setItem(TOKEN_KEY, token) : sessionStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
}

function qs(params = {}) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, v);
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export async function request(method, path, { params, body, form } = {}) {
  const headers = { "X-Request-ID": crypto.randomUUID().slice(0, 16) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(BASE + path + qs(params), { method, headers, body: payload });
  const isJson = (res.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await res.json() : await res.text();
  if (res.status === 401 && path !== "/auth/login") {
    setToken(null);
    window.dispatchEvent(new CustomEvent("stashtrace:unauthenticated"));
  }
  if (!res.ok) throw new ApiError(res.status, isJson ? data : null);
  return data;
}

export const api = {
  login: (employee_id, password) => request("POST", "/auth/login", { body: { employee_id, password } }),
  me: () => request("GET", "/auth/me"),
  stores: () => request("GET", "/stores"),
  skus: (params) => request("GET", "/skus", { params }),
  shifts: (params) => request("GET", "/shifts", { params }),
  employees: (params) => request("GET", "/employees", { params }),
  dashboard: (params) => request("GET", "/dashboard/summary", { params }),
  reconciliations: (params) => request("GET", "/reconciliations", { params }),
  reconciliation: (id, params) => request("GET", `/reconciliations/${encodeURIComponent(id)}`, { params }),
  submitCount: (body) => request("POST", "/counts", { body }),
  imports: (params) => request("GET", "/imports", { params }),
  importBatch: (id) => request("GET", `/imports/${encodeURIComponent(id)}`),
  importRows: (id, params) => request("GET", `/imports/${encodeURIComponent(id)}/rows`, { params }),
  upload: (form) => request("POST", "/imports", { form }),
  correctRow: (rowId, body) => request("POST", `/imports/rows/${rowId}/correction`, { body }),
  alerts: (params) => request("GET", "/alerts", { params }),
  alert: (id) => request("GET", `/alerts/${encodeURIComponent(id)}`),
  investigations: (params) => request("GET", "/investigations", { params }),
  investigation: (id) => request("GET", `/investigations/${encodeURIComponent(id)}`),
  caseEvent: (id, body) => request("POST", `/investigations/${encodeURIComponent(id)}/events`, { body }),
  runDetection: (body) => request("POST", "/detection-runs", { body }),
  runs: (params) => request("GET", "/detection-runs", { params }),
  movement: (id) => request("GET", `/movements/${encodeURIComponent(id)}`),
};
