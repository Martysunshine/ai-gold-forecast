import { api } from "../api";
import { usePoll } from "../hooks";
import { etDate, fmtAgo, fmtEt } from "../format";
import { AlertsPanel } from "./AlertsPanel";
import { DataSettings } from "./Details";

const when = (t: number | null | undefined) => (t ? `${etDate(t * 1000)}, ${fmtEt(t, { hour: "2-digit", minute: "2-digit", hour12: false })} New York` : "never");

/** Which process is actually keeping the app running, and how to control it. The browser cannot (and should not) install system services itself. */
function Service() {
  const { data, error } = usePoll(api.worker, 20000, "worker");
  return (
    <section className="gx-card" data-testid="settings-service">
      <div className="gx-card-head"><h2>BACKGROUND SERVICE</h2>
        <span className={`gx-chip ${data?.daemon_running ? "" : "warnchip"}`} data-testid="svc-state">{!data ? "…" : data.daemon_running ? `Running · ${data.runtime ?? "unknown"}` : "Not running"}</span></div>
      {!data ? <p className="gx-muted">{error ?? "Loading…"}</p> : (
        <>
          <p className="gx-micro" data-testid="svc-note">{data.runtime_note}</p>
          <dl className="gx-kv gx-al-kv">
            <div><dt>Heartbeat</dt><dd>{data.heartbeat_age_s != null ? `${data.heartbeat_age_s}s ago${data.pid ? ` · pid ${data.pid}` : ""}` : "none"}</dd></div>
            <div><dt>launchd agent installed</dt><dd>{data.launchd_installed ? "yes" : "no"}</dd></div>
            <div><dt>Last data collection</dt><dd>{when(data.jobs.collect?.last_ok)} {data.jobs.collect?.age_s != null ? `(${fmtAgo(data.jobs.collect.age_s)})` : ""}</dd></div>
            <div><dt>Last full cycle</dt><dd>{data.last_cycle ? `#${data.last_cycle.id} · ${data.last_cycle.trigger} · ${data.last_cycle.status} · ${when(data.last_cycle.started_at)}` : "none recorded yet"}</dd></div>
          </dl>
          <p className="gx-micro">Control it from a terminal (the browser cannot install system services):</p>
          <pre className="gx-code" data-testid="svc-commands">{`./scripts/worker.sh install     # install and start (launchd)
./scripts/worker.sh status
./scripts/worker.sh logs -f
./scripts/worker.sh restart
./scripts/worker.sh uninstall`}</pre>
          <p className="gx-micro">The worker has no network listener. For an always-on host see <code>deploy/README.md</code> (container, untested here). Detailed job status is on the Learning page.</p>
        </>
      )}
    </section>
  );
}

/** Alerts, notification configuration, data-source status and the service that runs it all. Nothing here changes a forecast. */
export function AlertsPage({ onDataChanged }: { onDataChanged: () => void }) {
  return (
    <div className="gx-page" data-testid="page-alerts">
      <AlertsPanel />
      <Service />
      <section className="gx-card" data-testid="settings-data">
        <h2>DATA</h2>
        <DataSettings onChanged={onDataChanged} />
      </section>
    </div>
  );
}
