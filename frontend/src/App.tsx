import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { usePoll } from "./hooks";
import type { Conn, ForwardPayload, InstrumentKey, RangeKey, TodayPayload } from "./types";
import { rangesFor, type ForecastTarget, type Shown } from "./forecastText";
import { Header, PAGES, type PageId } from "./components/Header";
import { PriceSummary } from "./components/PriceSummary";
import { ChartCard } from "./components/ChartCard";
import { ForecastPanel, SettlementNote } from "./components/ForecastPanel";
import { AlertsPage } from "./components/AlertsPage";
import { LearningPage } from "./components/LearningPage";
import { PaperPage } from "./components/PaperPage";
import { DetailsPanel, type DetailsTab } from "./components/Details";

function useStored<T extends string | boolean | number>(key: string, init: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try { const raw = localStorage.getItem(key); return raw == null ? init : (JSON.parse(raw) as T); } catch { return init; }
  });
  const set = useCallback((x: T) => { setV(x); try { localStorage.setItem(key, JSON.stringify(x)); } catch { /* storage unavailable: the setting just won't persist */ } }, [key]);
  return [v, set];
}

const pageFromHash = (): PageId => {
  const id = window.location.hash.replace(/^#\/?/, "") as PageId;
  return PAGES.some((p) => p.id === id) ? id : "forecast";
};
const msg = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong.");
const fetchTarget = (t: ForecastTarget): Promise<TodayPayload | ForwardPayload> => (t === "next" ? api.today() : api.forward(t));

export default function App() {
  const [page, setPage] = useState<PageId>(pageFromHash);
  const [instrument, setInstrument] = useStored<InstrumentKey>("gold.instrument", "futures");
  const [range, setRange] = useStored<RangeKey>("gold.range", "1D");
  const [mode, setMode] = useStored<"line" | "candles">("gold.mode", "candles");
  const [target, setTarget] = useStored<ForecastTarget>("gold.target", 5);
  const [shown, setShown] = useState<Shown | null>(null);          // the forecast on the chart: set ONLY by a Predict click
  const [predicting, setPredicting] = useState(false);
  const [predictErr, setPredictErr] = useState<string | null>(null);
  const [fitToken, setFitToken] = useState(0);
  const [detailsTab, setDetailsTab] = useState<DetailsTab>("backtest");
  const [chartConn, setChartConn] = useState<Conn>("ok");
  const [dataRev, setDataRev] = useState(0);

  useEffect(() => {
    const on = () => setPage(pageFromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const goPage = (p: PageId) => { window.location.hash = `/${p}`; setPage(p); };

  const summary = usePoll(() => api.summary(instrument), instrument === "spot" ? 5000 : 15000, `summary:${instrument}:${dataRev}`);
  const conn: Conn = chartConn !== "ok" ? chartConn : summary.conn;

  // While a forecast is on the chart, quietly check whether a newer one exists. It is only ever offered, never swapped in.
  const latest = usePoll(() => fetchTarget(shown!.target), 300000, `latest:${shown?.target}:${dataRev}`, !!shown);
  const newerId = shown && latest.data?.forecast && latest.data.forecast.forecast_id !== shown.payload.forecast?.forecast_id ? latest.data.forecast.forecast_id : null;

  const changeTarget = (t: ForecastTarget) => { if (t !== target) { setTarget(t); setShown(null); setPredictErr(null); } };   // a different question: the old answer leaves the chart
  const changeInstrument = (i: InstrumentKey) => { setInstrument(i); if (i !== "futures") { setShown(null); setPredictErr(null); } };

  const predict = async () => {
    if (instrument !== "futures" || predicting) return;
    setPredicting(true); setPredictErr(null);
    try {
      const p = await fetchTarget(target);
      if (!p.forecast) { setShown(null); setPredictErr(p.message ?? "No forecast is available for this target yet."); return; }
      // Predict fits the view to the forecast. Only if the current history range cannot show this target well is the range changed.
      const fit = rangesFor(target, p.forecast);
      if (!fit.allowed.includes(range)) setRange(fit.dflt as RangeKey);
      setShown({ target, payload: p, fetchedAt: Date.now() });
      setFitToken((n) => n + 1);
    } catch (e) { setPredictErr(`Could not get the forecast: ${msg(e)}`); }
    finally { setPredicting(false); }
  };

  // "Details" opens the tab panel under the chart (on the Forecast page) and scrolls it into view.
  const openDetails = (tab: DetailsTab = "backtest") => {
    setDetailsTab(tab);
    if (page !== "forecast") goPage("forecast");
    setTimeout(() => document.getElementById("details")?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
  };

  return (
    <div className="gx">
      <Header page={page} onPage={goPage} summary={summary.data} conn={conn} onDetails={() => openDetails()} />

      {page === "forecast" && (
        <div className="gx-page" data-testid="page-forecast">
          <div className="gx-seg gx-inst" role="tablist" aria-label="Instrument">
            {(["spot", "futures"] as const).map((i) => (
              <button key={i} role="tab" aria-selected={instrument === i} className={instrument === i ? "on" : ""} onClick={() => changeInstrument(i)} data-testid={`inst-${i}`}>
                {i === "spot" ? "Spot gold" : "Gold futures"}
              </button>
            ))}
          </div>
          <PriceSummary s={summary.data} err={summary.error} />
          <div className="gx-workspace">
            <div className="gx-leftcol">
              <ChartCard instrument={instrument} summary={summary.data} range={range} onRange={setRange} mode={mode} onMode={setMode}
                shown={instrument === "futures" ? shown : null} onHide={() => { setShown(null); setPredictErr(null); }} fitToken={fitToken} onFit={() => setFitToken((n) => n + 1)} onConn={setChartConn} conn={conn} />
              <SettlementNote shown={instrument === "futures" ? shown : null} />
            </div>
            <ForecastPanel canPredict={instrument === "futures"} target={target} onTarget={changeTarget} onPredict={predict} predicting={predicting} predictErr={predictErr}
              shown={instrument === "futures" ? shown : null} newerId={newerId} onDetails={openDetails} />
          </div>
          <DetailsPanel shown={instrument === "futures" ? shown : null} tab={detailsTab} onTab={setDetailsTab} />
        </div>
      )}
      {page === "learning" && <LearningPage />}
      {page === "paper" && <PaperPage />}
      {page === "alerts" && <AlertsPage onDataChanged={() => setDataRev((n) => n + 1)} />}

      <footer className="gx-footer">
        For information only — not investment advice. Forecasts are statistical estimates; the price may differ. Charts: <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView Lightweight Charts™</a>.
      </footer>
    </div>
  );
}
