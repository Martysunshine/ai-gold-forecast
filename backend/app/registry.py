"""Versioned model registry: one champion per task, challengers, an auditable decision log and rollback.

A *version* is a frozen procedure: algorithm + hyper-parameters + feature schema + calibration method for one task. It is
refit on fresh data at each forecast (a standard online-refit procedure); every forecast records the data cutoff it used,
so its forward outcomes are a fair, look-ahead-free test of that procedure. Changing hyper-parameters or the feature schema
creates a NEW version with a fresh forward record.
"""
from __future__ import annotations

import json
import time

from . import db

TASKS = ("nowcast", "fwd_1", "fwd_5", "fwd_20")
FEATURE_SCHEMAS = ("gold_v1", "drivers_v1")
# Bounded additions (ridge + one tree model only; not "every algorithm on every feature set"). patterns_v1 and the EXPERIMENTAL fib_v1 are compared
# on identical origins; fib_v1 is rejected (stops issuing) unless the pre-set paired test supports it. See feature_groups.py and learning.fib_verdict.
PATTERN_ALGOS = ("ridge", "hgb")
PATTERN_SCHEMAS = ("patterns_v1", "fib_v1")
FWD_ALGOS = ("ridge", "hgb", "random_forest")
NOWCAST_ALGOS = ("ridge", "hgb", "extra_trees")

# What the app was already serving before the learning loop existed (the previous validation-selected default per horizon).
SEED_CHAMPIONS = {"fwd_1": ("ridge", "gold_v1"), "fwd_5": ("random_forest", "gold_v1"), "fwd_20": ("ridge", "gold_v1"),
                  "nowcast": ("unchanged", "price_now_v1")}


def version_id(task: str, algorithm: str, schema: str) -> str:
    return f"{task}:{algorithm}:{schema}"


def baseline_version(task: str) -> str:
    return version_id(task, "unchanged", "price_now_v1")


def candidate_specs(task: str) -> list[tuple[str, str]]:
    """The (algorithm, schema) pairs tried for a task. Deliberately small: every extra candidate raises the chance of a lucky win."""
    if task == "nowcast":
        return [(a, "intraday_v1") for a in NOWCAST_ALGOS]
    return [(a, s) for a in FWD_ALGOS for s in FEATURE_SCHEMAS] + [(a, s) for a in PATTERN_ALGOS for s in PATTERN_SCHEMAS]


def seed() -> None:
    """Create the version rows (idempotent). The seeded champion is whatever the app was already serving."""
    now = time.time()
    with db.tx() as c:
        for task in TASKS:
            champ = SEED_CHAMPIONS[task]
            specs = [("unchanged", "price_now_v1")] + candidate_specs(task)
            for algo, schema in specs:
                v = version_id(task, algo, schema)
                status = "champion" if (algo, schema) == champ else "challenger"
                cur = c.execute("INSERT OR IGNORE INTO models (version,task,algorithm,feature_schema,status,created_at,status_changed_at,notes) VALUES (?,?,?,?,?,?,?,?)",
                                (v, task, algo, schema, status, now, now, "seeded: the model already in production" if status == "champion" else None))
                if cur.rowcount and status == "champion":
                    c.execute("INSERT INTO decisions (at,task,kind,champion_before,champion_after,decision,reason) VALUES (?,?,?,?,?,?,?)",
                              (now, task, "seed", None, v, "seed", "Initial champion = the model already serving forecasts before the learning loop was added."))


def champion(task: str) -> dict:
    r = db.rows("SELECT * FROM models WHERE task=? AND status='champion'", (task,))
    if not r:
        seed()
        r = db.rows("SELECT * FROM models WHERE task=? AND status='champion'", (task,))
    return r[0]


def versions(task: str, include_rejected: bool = False) -> list[dict]:
    """Versions that currently take part in forecasting and evaluation. Rejected ones keep their rows (and all their ledger history) but are idle."""
    q = "SELECT * FROM models WHERE task=?" + ("" if include_rejected else " AND status!='rejected'") + " ORDER BY status='champion' DESC, version"
    return db.rows(q, (task,))


def reject_schema(schema: str, reason: str, evidence: dict | None = None) -> list[str]:
    """Stop using every non-champion version of a feature schema. History is kept; nothing is deleted or rewritten."""
    now, done = time.time(), []
    for v in db.rows("SELECT * FROM models WHERE feature_schema=? AND status IN ('challenger','retired')", (schema,)):
        db.connect().execute("UPDATE models SET status='rejected', status_changed_at=?, notes=? WHERE version=?", (now, reason[:300], v["version"]))
        log_decision(v["task"], "evaluation", "reject", reason, v["version"], v["version"], evidence)
        done.append(v["version"])
    return done


def restore_schema(schema: str) -> list[str]:
    """Reinstate rejected versions when later evidence supports the group (they re-enter as challengers)."""
    now, done = time.time(), []
    for v in db.rows("SELECT * FROM models WHERE feature_schema=? AND status='rejected'", (schema,)):
        db.connect().execute("UPDATE models SET status='challenger', status_changed_at=?, notes=NULL WHERE version=?", (now, v["version"]))
        done.append(v["version"])
    return done


def mark_trained(version: str, trained_through: str) -> None:
    db.connect().execute("UPDATE models SET last_trained_at=?, trained_through=? WHERE version=?", (time.time(), trained_through, version))


def log_decision(task: str, kind: str, decision: str, reason: str, before: str | None, after: str | None, evidence: dict | None = None) -> None:
    db.connect().execute("INSERT INTO decisions (at,task,kind,champion_before,champion_after,decision,reason,evidence) VALUES (?,?,?,?,?,?,?,?)",
                         (time.time(), task, kind, before, after, decision, reason, json.dumps(evidence, default=float) if evidence else None))


def promote(task: str, version: str, reason: str, evidence: dict | None = None) -> None:
    now = time.time()
    before = champion(task)["version"]
    if version == before:
        return
    with db.tx() as c:
        c.execute("UPDATE models SET status='retired', status_changed_at=? WHERE task=? AND status='champion'", (now, task))
        c.execute("UPDATE models SET status='champion', status_changed_at=? WHERE version=?", (now, version))
        c.execute("INSERT INTO decisions (at,task,kind,champion_before,champion_after,decision,reason,evidence) VALUES (?,?,?,?,?,?,?,?)",
                  (now, task, "promotion", before, version, "promote", reason, json.dumps(evidence, default=float) if evidence else None))


def rollback(task: str, to_version: str | None = None, reason: str = "manual rollback") -> str:
    """Restore a previous champion (default: the most recent champion before the current one, else the seeded model)."""
    cur = champion(task)["version"]
    if to_version is None:
        prev = db.rows("SELECT champion_before FROM decisions WHERE task=? AND decision IN ('promote','seed') AND champion_after=? "
                       "AND champion_before IS NOT NULL ORDER BY id DESC LIMIT 1", (task, cur))
        to_version = prev[0]["champion_before"] if prev else version_id(task, *SEED_CHAMPIONS[task])
    if not db.rows("SELECT 1 FROM models WHERE version=? AND task=?", (to_version, task)):
        raise ValueError(f"unknown version {to_version} for {task}")
    now = time.time()
    with db.tx() as c:
        c.execute("UPDATE models SET status='retired', status_changed_at=? WHERE task=? AND status='champion'", (now, task))
        c.execute("UPDATE models SET status='champion', status_changed_at=? WHERE version=?", (now, to_version))
        c.execute("INSERT INTO decisions (at,task,kind,champion_before,champion_after,decision,reason) VALUES (?,?,?,?,?,?,?)",
                  (now, task, "rollback", cur, to_version, "rollback", reason))
    return to_version


def decisions(limit: int = 40, task: str | None = None) -> list[dict]:
    q, p = "SELECT * FROM decisions", ()
    if task:
        q, p = q + " WHERE task=?", (task,)
    out = db.rows(q + " ORDER BY id DESC LIMIT ?", p + (limit,))
    for d in out:
        d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else None
    return out
