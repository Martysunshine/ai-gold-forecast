import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { api, ApiError } from "./api";
import type { Horizon, NewsPayload, Prediction, PricePayload } from "./types";
import { Banner, EmptyState, ErrorBlock, Skeleton } from "./components/Common";
import { TopBar } from "./components/TopBar";
import { MarketStrip } from "./components/MarketStrip";
import { PriceChart } from "./components/PriceChart";
import { ForecastPanel } from "./components/ForecastPanel";
import { Backtest } from "./components/Backtest";
import { MarketContext } from "./components/MarketContext";
import { Methodology } from "./components/Methodology";
import { GldComparison } from "./components/GldComparison";

const msg = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong.");
type Tab = "backtest" | "context" | "gld" | "method";
const TABS: { id: Tab; label: string }[] = [{ id: "backtest", label: "Backtest" }, { id: "context", label: "Market Context" }, { id: "gld", label: "GLD Comparison" }, { id: "method", label: "Methodology" }];

export default function App() {
  const [prices, setPrices] = useState<PricePayload | null>(null);
  const [pricesErr, setPricesErr] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<null | "refresh" | "upload">(null);
  const [toast, setToast] = useState<{ kind: "error" | "info"; text: string } | null>(null);

  const [news, setNews] = useState<NewsPayload | null>(null);
  const [newsErr, setNewsErr] = useState<string | null>(null);
  const [newsLoading, setNewsLoading] = useState(true);

  const [horizon, setHorizon] = useState<Horizon>(5);
  // Results are stored under "<data_key>:<horizon>" and only ever *displayed* for the current data + selected horizon,
  // so changing the horizon or the dataset can never show an old result as if it were current.
  const [results, setResults] = useState<Record<string, Prediction>>({});
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("backtest");
  const fileRef = useRef<HTMLInputElement>(null);
  const researchRef = useRef<HTMLElement>(null);

  const result = prices ? results[`${prices.data_key}:${horizon}`] ?? null : null;

  const loadNews = useCallback(async (refresh = false) => {
    setNewsLoading(true); setNewsErr(null);
    try { setNews(await api.news(refresh)); } catch (e) { setNewsErr(msg(e)); } finally { setNewsLoading(false); }
  }, []);

  const applyPrices = useCallback((p: PricePayload) => { setPrices(p); setPricesErr(null); setRunError(null); }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    void loadNews();
    try { applyPrices(await api.prices()); }
    catch (e) { setPrices(null); setPricesErr(e instanceof ApiError ? e : new ApiError("unknown", msg(e))); }
    finally { setLoading(false); }
  }, [applyPrices, loadNews]);

  useEffect(() => { void loadAll(); }, [loadAll]);

  const run = async () => {
    if (!prices) return;
    setRunning(true); setRunError(null);
    try {
      const p = await api.predict(horizon);
      setResults((m) => ({ ...m, [`${p.data_key}:${p.horizon}`]: p }));
      if (p.data_key !== prices.data_key) {
        // The server's data changed since this page loaded (for example the provider cache refreshed). Resync instead of mixing data.
        applyPrices(await api.prices());
        setToast({ kind: "info", text: "Price data was updated on the server; the display has been refreshed. Run the forecast again if needed." });
      }
    } catch (e) { setRunError(msg(e)); }
    finally { setRunning(false); }
  };

  const doRefresh = async (call: () => Promise<PricePayload>, label: string) => {
    setBusy("refresh"); setToast(null);
    try {
      const p = await call();
      applyPrices(p);
      if (p.provider_error) setToast({ kind: "error", text: `${label} failed: ${p.provider_error} Showing the last valid data (${p.status.label.toLowerCase()}).` });
      void loadNews(true);
    } catch (e) {
      const err = e instanceof ApiError ? e : new ApiError("unknown", msg(e));
      if (!prices) setPricesErr(err); else setToast({ kind: "error", text: `${label} failed: ${err.message}` });
    } finally { setBusy(null); }
  };

  const onFile = async (file?: File | null) => {
    if (!file) return;
    setBusy("upload"); setToast(null);
    try { applyPrices(await api.upload(file)); setToast({ kind: "info", text: `Loaded ${file.name}. Forecasts will be computed from this file; “Use live data” switches back.` }); }
    catch (e) { setToast({ kind: "error", text: `Upload rejected — active data unchanged. ${msg(e)}` }); }
    finally { setBusy(null); if (fileRef.current) fileRef.current.value = ""; }
  };

  const openBacktest = () => { setTab("backtest"); researchRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }); };
  const onTabKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex((t) => t.id === tab);
    const next = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i + TABS.length - 1) % TABS.length : -1;
    if (next >= 0) { e.preventDefault(); setTab(TABS[next].id); document.getElementById(`tab-${TABS[next].id}`)?.focus(); }
  };
  const noData = !loading && !prices && pricesErr?.code === "no_data";

  return (
    <div className="app">
      <TopBar prices={prices} busy={busy} onRefresh={() => doRefresh(api.refresh, "Refresh")} onUseLive={() => doRefresh(api.useProvider, "Switch to live data")} onUpload={() => fileRef.current?.click()} />
      <input ref={fileRef} type="file" accept=".csv,text/csv" hidden onChange={(e) => onFile(e.target.files?.[0])} data-testid="file-input" />

      <main>
        {toast && <Banner kind={toast.kind} onClose={() => setToast(null)}><span data-testid="toast">{toast.text}</span></Banner>}
        {prices?.provider_error && !toast && <Banner kind="warn"><b>Live refresh failed.</b> Showing the last valid data, last updated {Math.round(prices.age_minutes) < 90 ? `${Math.round(prices.age_minutes)} min` : `${Math.round(prices.age_minutes / 60)} h`} ago. {prices.provider_error}</Banner>}
        {prices?.freshness.status === "stale" && <Banner kind="warn">{prices.freshness.message}</Banner>}

        {loading && (
          <div className="panel" aria-busy="true" aria-label="Loading market data"><Skeleton h={90} /><div style={{ height: 16 }} /><Skeleton h={380} /></div>
        )}
        {noData && <EmptyState message={pricesErr!.message} busy={busy !== null} onUpload={() => fileRef.current?.click()} onRetry={() => doRefresh(api.refresh, "Refresh")} />}
        {!loading && pricesErr && !noData && <ErrorBlock title="Could not load prices" message={pricesErr.message} onRetry={loadAll} />}

        {prices && (
          <>
            <MarketStrip p={prices} />
            <section className="workspace" aria-label="Chart and forecast">
              <div className="ws-main"><PriceChart prices={prices} result={result} /></div>
              <ForecastPanel prices={prices} horizon={horizon} onHorizon={setHorizon} onRun={run} running={running} error={runError} result={result} onOpenBacktest={openBacktest} />
            </section>
          </>
        )}

        {!noData && (
          <section className="research" ref={researchRef} aria-label="Research">
            <div className="tabs" role="tablist" aria-label="Research sections" onKeyDown={onTabKey}>
              {TABS.map((t) => (
                <button key={t.id} id={`tab-${t.id}`} role="tab" aria-selected={tab === t.id} aria-controls={`panel-${t.id}`} tabIndex={tab === t.id ? 0 : -1}
                  className={tab === t.id ? "on" : ""} onClick={() => setTab(t.id)} data-testid={`tab-${t.id}`}>{t.label}</button>
              ))}
            </div>
            <div className="tabpanel" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
              {tab === "backtest" && <Backtest result={result} horizon={horizon} />}
              {tab === "context" && <MarketContext news={news} loading={newsLoading} error={newsErr} onRetry={() => loadNews(true)} />}
              {tab === "gld" && <GldComparison />}
              {tab === "method" && prices && <Methodology prices={prices} result={result} />}
            </div>
          </section>
        )}

        <footer className="credits">AI Gold Forecast · prices: Yahoo Finance (GC=F, COMEX gold futures) · headlines: Google News RSS · headlines are not a model input · forecasts are model outputs, not trading signals</footer>
      </main>
    </div>
  );
}
