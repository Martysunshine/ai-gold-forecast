import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { Horizon, NewsPayload, Prediction, PricePayload } from "./types";
import { Banner, CardSkeleton, EmptyState, ErrorCard } from "./components/Common";
import { PriceCard } from "./components/PriceCard";
import { PredictionPanel } from "./components/PredictionPanel";
import { PriceChart } from "./components/PriceChart";
import { TestPanel } from "./components/TestPanel";
import { NewsSection } from "./components/News";

const msg = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong.");

export default function App() {
  const [prices, setPrices] = useState<PricePayload | null>(null);
  const [pricesErr, setPricesErr] = useState<ApiError | null>(null);
  const [news, setNews] = useState<NewsPayload | null>(null);
  const [newsErr, setNewsErr] = useState<string | null>(null);
  const [newsLoading, setNewsLoading] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<null | "refresh" | "upload">(null);
  const [toast, setToast] = useState<{ kind: "error" | "info"; text: string } | null>(null);
  const [horizon, setHorizon] = useState<Horizon>(5);
  const [predictions, setPredictions] = useState<Partial<Record<Horizon, Prediction>>>({});
  const [predicting, setPredicting] = useState(false);
  const [predictErr, setPredictErr] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loadNews = useCallback(async (refresh = false) => {
    setNewsLoading(true); setNewsErr(null);
    try { setNews(await api.news(refresh)); } catch (e) { setNewsErr(msg(e)); } finally { setNewsLoading(false); }
  }, []);

  const applyPrices = useCallback((p: PricePayload) => { setPrices(p); setPricesErr(null); setPredictions({}); setPredictErr(null); }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    void loadNews();
    try { applyPrices(await api.prices()); }
    catch (e) { setPrices(null); setPricesErr(e instanceof ApiError ? e : new ApiError("unknown", msg(e))); }
    finally { setLoading(false); }
  }, [applyPrices, loadNews]);

  useEffect(() => { void loadAll(); }, [loadAll]);

  const predict = async () => {
    setPredicting(true); setPredictErr(null);
    try { const p = await api.predict(horizon); setPredictions((m) => ({ ...m, [horizon]: p })); }
    catch (e) { setPredictErr(msg(e)); }
    finally { setPredicting(false); }
  };

  const refresh = async () => {
    setBusy("refresh"); setToast(null);
    try { applyPrices(await api.refresh()); void loadNews(true); }
    catch (e) {
      const err = e instanceof ApiError ? e : new ApiError("unknown", msg(e));
      if (!prices) setPricesErr(err); else setToast({ kind: "error", text: `Refresh failed: ${err.message}` });
    } finally { setBusy(null); }
  };

  const onFile = async (file?: File | null) => {
    if (!file) return;
    setBusy("upload"); setToast(null);
    try { applyPrices(await api.upload(file)); setToast({ kind: "info", text: `Loaded ${file.name}. Click Predict to run the models on this data.` }); }
    catch (e) { setToast({ kind: "error", text: `Upload rejected: ${msg(e)}` }); }
    finally { setBusy(null); if (fileRef.current) fileRef.current.value = ""; }
  };

  const pickFile = () => fileRef.current?.click();
  const prediction = predictions[horizon] ?? null;
  const noData = !loading && !prices && pricesErr?.code === "no_data";
  const canDownload = !!prediction && !busy;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden="true">Au</span>
          <div>
            <h1>Gold Outlook</h1>
            <p>Gold price history, a simple forecast, and an honest test of it</p>
          </div>
        </div>
        <div className="actions">
          <button className="btn" onClick={refresh} disabled={busy !== null} data-testid="refresh">{busy === "refresh" ? "Refreshing…" : "Refresh data"}</button>
          <button className="btn" onClick={pickFile} disabled={busy !== null} data-testid="upload">{busy === "upload" ? "Uploading…" : "Upload CSV"}</button>
          <a className={`btn ${canDownload ? "" : "disabled"}`} href={canDownload ? api.downloadUrl(horizon) : undefined} aria-disabled={!canDownload} download data-testid="download"
            title={canDownload ? "Download this prediction as CSV" : "Run Predict first"}>Download prediction</a>
          <input ref={fileRef} type="file" accept=".csv,text/csv" hidden onChange={(e) => onFile(e.target.files?.[0])} data-testid="file-input" />
        </div>
      </header>

      <main>
        {toast && <Banner kind={toast.kind} onClose={() => setToast(null)}><span data-testid="toast">{toast.text}</span></Banner>}
        {prices?.provider_error && <Banner kind="warn"><b>Live refresh failed</b> — showing previously saved real data. {prices.provider_error} You can also upload a CSV.</Banner>}
        {prices?.freshness.status === "stale" && <Banner kind="warn">{prices.freshness.message}</Banner>}

        {loading && <div className="grid hero" aria-live="polite"><CardSkeleton label="Loading price" /><CardSkeleton label="Loading" lines={4} /></div>}
        {noData && <EmptyState message={pricesErr!.message} busy={busy !== null} onUpload={pickFile} onRetry={refresh} />}
        {!loading && pricesErr && !noData && <ErrorCard title="Could not load prices" message={pricesErr.message} onRetry={loadAll} />}

        {prices && (
          <>
            <div className="grid hero">
              <PriceCard p={prices} />
              <PredictionPanel horizon={horizon} onHorizon={setHorizon} onPredict={predict} loading={predicting} error={predictErr} prediction={prediction} />
            </div>
            <PriceChart prices={prices} prediction={prediction} />
            {prediction && <TestPanel p={prediction} />}
          </>
        )}

        {!noData && <NewsSection news={news} loading={newsLoading} error={newsErr} onRetry={() => loadNews(true)} />}

        <footer className="credits">Gold Outlook · prices: Yahoo Finance (GC=F, COMEX gold futures) · news: Google News RSS · news is not used by the prediction model</footer>
      </main>
    </div>
  );
}
