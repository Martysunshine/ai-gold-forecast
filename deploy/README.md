# Always-on worker (no exposed API)

The worker (`python -m app.worker run`) does everything that must happen without a browser: collect, validate, score, learn, publish forecasts,
evaluate alerts, run the paper book. It has **no HTTP listener**. Two supported runtimes:

| Runtime | How | Survives | Stops when |
|---|---|---|---|
| `launchd` (this Mac) | `./scripts/worker.sh install` | crash, login, restart | the Mac sleeps or is off |
| `container` (always-on host) | `docker compose -f deploy/docker-compose.worker.yml up -d --build` | host reboot (`restart: unless-stopped`) | the host stops |

The Learning page and `./scripts/worker.sh status` report which runtime is actually operating (from the worker's own heartbeat), and a sleeping or
powered-off Mac records nothing: gaps stay gaps.

**Untested here:** the Dockerfile and compose file were written but not built or run in this environment (no container runtime was used). Build and
check `docker compose ps` / `docker logs` before relying on them.

## Seeing the results of a remote worker
The container keeps its own SQLite database in the `gold-data` volume. To look at it, run the web app **next to the worker on that host**, bound to
loopback (the default), and reach it through an SSH tunnel:

    ssh -L 8750:127.0.0.1:8750 you@host

Do not publish the web port. If you ever must expose it, set `GOLD_API_TOKEN` (state-changing calls then need `X-Gold-Token`), terminate TLS in front of
it, and put real authentication in the proxy: the app itself has no user accounts.

## One runtime at a time
Only one worker holds the daemon lock per data directory. Running the launchd agent and the container against copies of the same data would create two
diverging ledgers: pick one runtime per data directory.
