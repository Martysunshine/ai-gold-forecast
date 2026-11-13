import type { ForecastPayload, NewsPayload, PricePayload } from "./types";

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
    throw new ApiError("network", "Cannot reach the Gold Outlook API. Is the backend running?");
  }
  if (!res.ok) {
    let code = "http_" + res.status;
    let message = `Request failed (${res.status}).`;
    try {
      const body = await res.json();
      const d = body?.detail;
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
  upload: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<PricePayload>("/api/prices/upload", { method: "POST", body });
  },
  forecast: () => request<ForecastPayload>("/api/forecast"),
  news: (refresh = false) => request<NewsPayload>(`/api/news${refresh ? "?refresh=true" : ""}`),
  downloadUrl: "/api/forecast/download",
};
