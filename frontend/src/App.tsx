import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { ForecastPayload, Horizon, NewsPayload, PricePayload, Scenario } from "./types";
import { Banner, CardSkeleton, EmptyState, ErrorCard } from "./components/Common";
import { PriceCard } from "./components/PriceCard";
import { ForecastPanel } from "./components/ForecastPanel";
import { PriceChart } from "./components/PriceChart";
import { PerformancePanel, Signals } from "./components/Insights";
import { NewsSection } from "./components/News";

const msg = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Something went wrong.");

export default function App() {
  const [prices, setPrices] = useState<PricePayload | null>(null);
  const [pricesErr, setPricesErr] = useState<ApiError | null>(null);
  const [forecast, setForecast] = useState<ForecastPayload | null>(null);
  const [forecastErr, setForecastErr] = useState<string | null>(null);
  const [news, setNews] = useState<NewsPayload | null>(null);
  const [newsErr, setNewsErr] = useState<string | null>(null);
  const [newsLoading, setNewsLoading] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<null | "refresh" | "upload">(null);
  const [toast, setToast] = useState<{ kind: "error" | "info"; text: string } | null>(null);
  const [horizon, setHorizon] = useState<Horizon>(5);
  const [scenario, setScenario] = useState<Scenario>("base");
  const fileRef = useRef<HTMLInputElement>(null);

  const loadForecast = useCallback(async () => {
    setForecastErr(null);
    try { setForecast(await api.forecast()); } catch (e) { setForecast(null); setForecastErr(msg(e)); }
  }, []);

  const loadNews = useCallback(async (refresh = false) => {
    setNewsLoading(true); setNewsErr(null);
    try { setNews(await api.news(refresh)); } catch (e) { setNewsErr(msg(e)); } finally { setNewsLoading(false); }
  }, []);

  const applyPrices = useCallback(async (p: PricePayload) => { setPrices(p); setPricesErr(null); setForecast(null); await loadForecast(); }, [loadForecast]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    loadNews();
    try { await applyPrices(await api.prices()); }
    catch (e) { setPrices(null); setForecast(null); setPricesErr(e instanceof ApiError ? e : new ApiError("unknown", msg(e))); }
    finally { setLoading(false); }
  }, [applyPrices, loadNews]);

  useEffect(() => { void loadAll(); }, [loadAll]);

  const refresh = async () => {
    setBusy("refresh"); setToast(null);
    try {
      await applyPrices(await api.refresh());
      void loadNews(true);
    } catch (e) {
      const err = e instanceof ApiError ? e : new ApiError("unknown", msg(e));
      if (!prices) setPricesErr(err); else setToast({ kind: "error", text: err.message });
    } finally { setBusy(null); }
  };

  const onFile = async (file?: File | null) => {
    if (!file) return;
    setBusy("upload"); setToast(null);
    try {
      await applyPrices(await api.upload(file));
      setToast({ kind: "info", text: `Loaded ${file.name}. The forecast has been recomputed from this file.` });
    } catch (e) { setToast({ kind: "error", text: `Upload rejected: ${msg(e)}` }); }
    finally { setBusy(null); if (fileRef.current) fileRef.current.value = ""; }
  };

  const pickFile = () => fileRef.current?.click();
  const noData = !loading && !prices && pricesErr?.code === "no_data";
  const canDownload = !!forecast && !busy;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden="true">Au</span>
          <div>
            <h1>Gold Outlook</h1>
            <p>Historical behaviour, a simple forecast, and the news around it</p>
          </div>
        </div>
        <div className="actions">
          <button className="btn" onClick={refresh} disabled={busy !== null}>{busy === "refresh" ? "Refreshing…" : "Refresh"}</button>
          <button className="btn" onClick={pickFile} disabled={busy !== null}>{busy === "upload" ? "Uploading…" : "Upload CSV"}</button>
          <a className={`btn primary ${canDownload ? "" : "disabled"}`} href={canDownload ? api.downloadUrl : undefined} aria-disabled={!canDownload} download>Download forecast</a>
          <input ref={fileRef} type="file" accept=".csv,text/csv" hidden onChange={(e) => onFile(e.target.files?.[0])} />
        </div>
      </header>

      <main>
        {toast && <Banner kind={toast.kind} onClose={() => setToast(null)}>{toast.text}</Banner>}
        {prices?.provider_error && <Banner kind="warn"><b>Live refresh failed</b> — showing previously saved real data. {prices.provider_error}. You can also upload a CSV.</Banner>}
        {prices?.freshness.status === "stale" && <Banner kind="warn">{prices.freshness.message}</Banner>}

        {loading && (
          <div className="grid hero" aria-live="polite">
            <CardSkeleton label="Loading price" /><CardSkeleton label="Loading forecast" lines={4} />
          </div>
        )}

        {noData && <EmptyState message={pricesErr!.message} busy={busy !== null} onUpload={pickFile} onRetry={refresh} />}
        {!loading && pricesErr && !noData && <ErrorCard title="Could not load prices" message={pricesErr.message} onRetry={loadAll} />}

        {prices && (
          <>
            <div className="grid hero">
              <PriceCard p={prices} />
              {forecast ? (
                <ForecastPanel data={forecast} horizon={horizon} onHorizon={setHorizon} scenario={scenario} onScenario={setScenario} />
              ) : forecastErr ? (
                <ErrorCard title="Forecast unavailable" message={forecastErr} onRetry={loadForecast} />
              ) : <CardSkeleton label="Computing forecast" lines={4} />}
            </div>

            <PriceChart prices={prices} forecasts={forecast?.forecasts ?? null} horizon={horizon} scenario={scenario} />

            {forecast && (
              <div className="grid two">
                <Signals data={forecast} horizon={horizon} />
                <PerformancePanel data={forecast} horizon={horizon} />
              </div>
            )}
          </>
        )}

        {!noData && <NewsSection news={news} loading={newsLoading} error={newsErr} onRetry={() => loadNews(true)} />}

        <footer className="limits">
          <h2>Limitations</h2>
          <ul>
            <li>Gold is hard to forecast. This is a deliberately simple Ridge regression on past prices; it carries no promise of accuracy and the backtest above shows how it actually did.</li>
            <li>The range is an indicative 80% interval built from past out-of-sample errors. Real moves can fall outside it, especially around shocks and regime changes.</li>
            <li>News is shown as context only. It is not an input to the model, and the bullish/base/bearish control is a user assumption.</li>
            <li>The default series is COMEX gold futures (GC=F), not spot gold. Forecast dates skip weekends but not exchange holidays.</li>
            <li>Not financial advice. No trading or execution is provided.</li>
          </ul>
        </footer>
      </main>
    </div>
  );
}
