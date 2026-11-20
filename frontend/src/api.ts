import type { PaperParams, PaperPayload, WorkerStatus, WorkspacePayload, AlertSettings, AlertsPayload, BasisCheck, DriversPanel, ForwardPayload, GldComparison, Horizon, InstrumentKey, LearningPayload, MarketSummary, NewsPayload, OhlcPayload, Prediction, PricePayload, RangeKey, TodayPayload } from "./types";

export class ApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError("network", "Cannot reach the AI Gold Forecast API. Is the backend running?");
  }
  if (!res.ok) {
    let code = "http_" + res.status;
    let message = `Request failed (${res.status}).`;
    try {
      const d = (await res.json())?.detail;
      if (d && typeof d === "object") { code = d.code ?? code; message = d.message ?? message; }
      else if (typeof d === "string") message = d;
    } catch { /* non-JSON error body */ }
    throw new ApiError(code, message);
  }
  return res.json() as Promise<T>;
}

export const api = {
  prices: () => request<PricePayload>("/api/prices"),
  refresh: () => request<PricePayload>("/api/prices/refresh", { method: "POST" }),
  useProvider: () => request<PricePayload>("/api/prices/use-provider", { method: "POST" }),
  upload: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<PricePayload>("/api/prices/upload", { method: "POST", body });
  },
  predict: (h: Horizon) => request<Prediction>(`/api/predict?horizon=${h}`),
  summary: (i: InstrumentKey) => request<MarketSummary>(`/api/market/summary?instrument=${i}`),
  ohlc: (i: InstrumentKey, r: RangeKey, since?: number) => request<OhlcPayload>(`/api/market/ohlc?instrument=${i}&range=${r}${since ? `&since=${since}` : ""}`),
  today: () => request<TodayPayload>("/api/forecast/today"),
  forward: (h: Horizon) => request<ForwardPayload>(`/api/forecast/forward?horizon=${h}`),
  learning: () => request<LearningPayload>("/api/learning"),
  drivers: () => request<DriversPanel>("/api/drivers"),
  gld: (refresh = false) => request<GldComparison>(`/api/compare/gld${refresh ? "?refresh=true" : ""}`),
  news: (refresh = false) => request<NewsPayload>(`/api/news${refresh ? "?refresh=true" : ""}`),
  alerts: () => request<AlertsPayload>("/api/alerts"),
  saveAlerts: (patch: Partial<AlertSettings>) => request<AlertsPayload>("/api/alerts/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }),
  alertsBasis: () => request<BasisCheck>("/api/alerts/basis"),
  exportForecastUrl: (forecastId: string) => `/api/forecast/export?forecast_id=${encodeURIComponent(forecastId)}`,
  workspace: () => request<WorkspacePayload>("/api/learning/workspace"),
  worker: () => request<WorkerStatus>("/api/worker"),
  paper: () => request<PaperPayload>("/api/paper"),
  paperNewBook: (body: { name?: string; initial_cash?: number }) => request<unknown>("/api/paper/book", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  paperParams: (id: number, patch: Partial<PaperParams>) => request<unknown>(`/api/paper/book/${id}/params`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }),
  paperPause: (id: number) => request<unknown>(`/api/paper/book/${id}/pause`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }),
  paperResume: (id: number) => request<unknown>(`/api/paper/book/${id}/resume`, { method: "POST" }),
  paperFlatten: (id: number) => request<unknown>(`/api/paper/book/${id}/flatten`, { method: "POST" }),
  paperEvaluate: () => request<unknown>("/api/paper/evaluate", { method: "POST" }),
  downloadUrl: (h: Horizon, key: string) => `/api/predict/download?horizon=${h}&key=${encodeURIComponent(key)}`,
};
