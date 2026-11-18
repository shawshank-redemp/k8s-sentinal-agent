#!/usr/bin/env python3
"""Honest metrics from TrueForge's stored data, for REAL-model sessions only.

    python3 eval/metrics_report.py --db <path to a trueforge.sqlite> [--model <id>] [--json-out FILE]

Safety rules this script enforces:
  * It never opens the database it is given. It copies trueforge.sqlite (+ -wal/-shm) into a
    temporary directory and reads only that copy, so a running TrueForge is never touched.
  * It is stdlib only.
  * Mock-LLM sessions are excluded. A session counts as MOCK if its metadata says
    run_mode=rehearsal, or (for sessions recorded before run_mode existed) if every model call
    reports exactly 10 input / 10 output tokens - the hardcoded usage in demo/mock-llm/mock_llm.py.
    A session is REAL if run_mode=real, or (legacy) if any model call reports other numbers.
  * Cost is computed only when eval/prices.json has numbers for --model. No price is guessed.
    Model identity is NOT stored in TrueForge's database, so --model is an assumption you state.

Metrics are read from fields TrueForge actually stores:
  model.message.usage            input_tokens, output_tokens, cache_read_tokens,
                                 input_tokens_breakdown {harness, skills, instructions,
                                 tool_definitions, messages}
  session.metrics                total_duration_ms, total_turns (TrueForge's own counters)
  tool.response.content          byte length of each tool response, grouped by tool name
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict

MOCK_USAGE = (10, 10)  # hardcoded by demo/mock-llm/mock_llm.py
BREAKDOWN_KEYS = ["harness", "skills", "instructions", "tool_definitions", "messages"]
HERE = os.path.dirname(os.path.abspath(__file__))


def copy_db(src):
    tmp = tempfile.mkdtemp(prefix="tf-metrics-")
    dst = os.path.join(tmp, "trueforge.sqlite")
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(src + suffix):
            shutil.copy2(src + suffix, dst + suffix)
    return dst, tmp


def load_prices(model):
    path = os.path.join(HERE, "prices.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        table = json.load(f).get("models", {})
    entry = table.get(model) if model else None
    if not entry or entry.get("input_per_mtok_usd") is None or entry.get("output_per_mtok_usd") is None:
        return None
    return entry


def classify(run_mode, calls):
    if run_mode == "rehearsal":
        return "mock"
    if run_mode == "real":
        return "real"
    if not calls:
        return "no-model-calls"
    if all((c.get("input_tokens"), c.get("output_tokens")) == MOCK_USAGE for c in calls):
        return "mock"
    return "real"


def analyse(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    sessions = {}
    for r in con.execute(
        "select session_id, created_at, json(metadata) md, json(metrics) mx from session"
    ):
        meta = json.loads(r["md"] or "{}")
        mx = json.loads(r["mx"] or "{}")
        sessions[r["session_id"]] = {
            "session_id": r["session_id"], "created_at": r["created_at"],
            "run_mode": meta.get("run_mode"), "workload": meta.get("workload"),
            "severity": meta.get("severity"),
            "duration_ms": mx.get("total_duration_ms", 0), "turns": mx.get("total_turns", 0),
            "calls": [], "tool_bytes": defaultdict(int), "tool_count": defaultdict(int),
        }

    call_tool_name = {}  # tool_call_id -> tool name, from the model message that issued it
    rows = con.execute(
        "select session_id, json(event) ev from session_event "
        "where json_extract(event,'$.type') in ('model.message','tool.response') order by created_at"
    ).fetchall()
    for r in rows:
        s = sessions.get(r["session_id"])
        if s is None:
            continue
        ev = json.loads(r["ev"])
        if ev.get("type") == "model.message":
            for tc in ev.get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "call_tool":
                    name = args.get("tool_name", name)
                call_tool_name[tc.get("id")] = name
            u = ev.get("usage")
            if u:
                s["calls"].append({
                    "input_tokens": u.get("input_tokens", 0),
                    "output_tokens": u.get("output_tokens", 0),
                    "cache_read_tokens": u.get("cache_read_tokens", 0),
                    "breakdown": u.get("input_tokens_breakdown") or {},
                })
        elif ev.get("type") == "tool.response":
            name = call_tool_name.get(ev.get("tool_call_id"), "(unknown)")
            s["tool_bytes"][name] += len((ev.get("content") or "").encode("utf-8"))
            s["tool_count"][name] += 1
    con.close()

    out = []
    for s in sessions.values():
        s["kind"] = classify(s["run_mode"], s["calls"])
        out.append(s)
    return out


def summarise(sessions, model, prices):
    real = [s for s in sessions if s["kind"] == "real"]
    excluded = defaultdict(int)
    for s in sessions:
        if s["kind"] != "real":
            excluded[s["kind"]] += 1

    rows = []
    for s in real:
        calls = s["calls"]
        inp = sum(c["input_tokens"] for c in calls)
        out_t = sum(c["output_tokens"] for c in calls)
        cache = sum(c["cache_read_tokens"] for c in calls)
        comp = {k: sum((c["breakdown"].get(k) or 0) for c in calls) for k in BREAKDOWN_KEYS}
        cost = None
        if prices:
            cost = inp / 1e6 * prices["input_per_mtok_usd"] + out_t / 1e6 * prices["output_per_mtok_usd"]
        rows.append({
            "session_id": s["session_id"], "created_at": s["created_at"], "workload": s["workload"],
            "severity": s["severity"], "turns": s["turns"], "model_calls": len(calls),
            "input_tokens": inp, "output_tokens": out_t, "cache_read_tokens": cache,
            "duration_ms": s["duration_ms"], "breakdown_sum": comp,
            "tool_bytes": dict(s["tool_bytes"]), "tool_count": dict(s["tool_count"]),
            "cost_usd": cost,
        })
    return {
        "label": "REAL-MODEL SESSIONS ONLY. Model assumed: " + (model or "not stated")
                 + ". Cost: " + ("computed from eval/prices.json" if prices else "NOT computed (prices.json unfilled)"),
        "excluded": dict(excluded), "sessions": rows,
    }


def fmt_table(report):
    lines = [report["label"], f"Excluded: {report['excluded']}", ""]
    if not report["sessions"]:
        lines.append("No real-model sessions found.")
        return "\n".join(lines)
    hdr = f"{'session':<28}{'workload':<34}{'calls':>6}{'in_tok':>9}{'out_tok':>9}{'cache':>8}{'dur_s':>9}{'turns':>7}  cost_usd"
    lines.append(hdr)
    lines.append("-" * len(hdr))
    tot = defaultdict(float)
    for r in report["sessions"]:
        cost = "n/a" if r["cost_usd"] is None else f"{r['cost_usd']:.4f}"
        lines.append(f"{r['session_id'][:26]:<28}{(r['workload'] or '-')[:32]:<34}{r['model_calls']:>6}"
                     f"{r['input_tokens']:>9}{r['output_tokens']:>9}{r['cache_read_tokens']:>8}"
                     f"{r['duration_ms']/1000:>9.1f}{r['turns']:>7}  {cost}")
        for k in ("model_calls", "input_tokens", "output_tokens", "cache_read_tokens", "duration_ms"):
            tot[k] += r[k]
    lines.append("-" * len(hdr))
    lines.append(f"{'TOTAL (' + str(len(report['sessions'])) + ' sessions)':<62}{int(tot['model_calls']):>6}"
                 f"{int(tot['input_tokens']):>9}{int(tot['output_tokens']):>9}{int(tot['cache_read_tokens']):>8}"
                 f"{tot['duration_ms']/1000:>9.1f}")

    comp = defaultdict(int)
    for r in report["sessions"]:
        for k, v in r["breakdown_sum"].items():
            comp[k] += v
    comp_total = sum(comp.values()) or 1
    lines += ["", "input_tokens_breakdown, summed over all model calls of real sessions:"]
    for k in BREAKDOWN_KEYS:
        lines.append(f"  {k:<18}{int(comp[k]):>10}  {100*comp[k]/comp_total:5.1f}%")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path to a trueforge.sqlite (a copy is read, never the original)")
    ap.add_argument("--model", default=None, help="model id to price against (assumed; not stored by TrueForge)")
    ap.add_argument("--json-out", default=None, help="also write the full report as JSON to this file")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        sys.exit(f"no such database: {a.db}")
    copy, tmp = copy_db(a.db)
    try:
        sessions = analyse(copy)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    prices = load_prices(a.model)
    report = summarise(sessions, a.model, prices)
    print(fmt_table(report))
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nwrote {a.json_out}")


if __name__ == "__main__":
    main()
