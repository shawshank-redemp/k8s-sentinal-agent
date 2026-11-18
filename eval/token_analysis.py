#!/usr/bin/env python3
"""Token analysis over stored real-model sessions, plus a LOCAL estimate of tool-output trimming.

    python3 eval/token_analysis.py --db <copy of trueforge.sqlite> [--model <id>]

Measured (from stored data, real-model sessions only, via eval/metrics_report.py):
  * input_tokens_breakdown by component, summed over every model call
  * tool.response bytes by tool name, and their share

Estimated (NOT measured, and not a real-model result):
  * Method: tokens ~= bytes / 4 (a rough English/JSON approximation; no tokenizer is used, because
    none is in this repo and adding one needs your approval).
  * A tool result stays in context and is re-sent on every later model call of the same session.
    So removing N bytes from a result saves about N/4 input tokens for EACH later call.
  * Trimming rule simulated: (a) cap each tool result at CAP characters (log tails and long lists);
    (b) drop a result that is byte-identical to an earlier result in the same session, replacing it
    with a one-line pointer. Both are simulated on the stored text, not on a live run.
  * Savings are reported as bytes and tokens-per-call, not as cost (no prices are used).
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics_report as mr  # noqa: E402  (same folder; reuses the safe copy-and-read parser)

CAP = 2000  # characters kept per tool result under the simulated trimming rule


def tool_texts(db_copy, real_ids):
    """Return {session_id: [("call", None) | ("resp", (tool_name, text)), ...]} for real sessions only."""
    import sqlite3
    con = sqlite3.connect(db_copy)
    call_name = {}
    per_session = defaultdict(list)  # sid -> list of ("call"|"resp", payload)
    for r in con.execute(
        "select session_id, json(event) ev from session_event "
        "where json_extract(event,'$.type') in ('model.message','tool.response') order by created_at"
    ):
        if r[0] not in real_ids:
            continue
        ev = json.loads(r[1])
        if ev["type"] == "model.message":
            for tc in ev.get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "call_tool":
                    name = args.get("tool_name", name)
                call_name[tc.get("id")] = name
            if ev.get("usage"):
                per_session[r[0]].append(("call", None))
        else:
            name = call_name.get(ev.get("tool_call_id"), "(unknown)")
            per_session[r[0]].append(("resp", (name, ev.get("content") or "")))
    con.close()
    return dict(per_session)


def simulate(items):
    """Return (bytes_before, bytes_after_trim, token_saving_estimate, responses_by_tool_before)."""
    seen = set()
    calls_after = [0] * len(items)
    count = 0
    for i in range(len(items) - 1, -1, -1):
        calls_after[i] = count
        if items[i][0] == "call":
            count += 1
    before = after = 0
    saved_tokens = 0.0
    by_tool = defaultdict(lambda: [0, 0])  # name -> [bytes_before, bytes_after]
    for i, (kind, payload) in enumerate(items):
        if kind != "resp":
            continue
        name, text = payload
        b = len(text.encode("utf-8"))
        if text in seen:
            new = f"[duplicate of an earlier {name} result in this run; omitted]"
        elif len(text) > CAP:
            new = text[:CAP] + f"\n[... trimmed to {CAP} characters ...]"
        else:
            new = text
        seen.add(text)
        a = len(new.encode("utf-8"))
        before += b
        after += a
        by_tool[name][0] += b
        by_tool[name][1] += a
        saved_tokens += (b - a) / 4 * calls_after[i]
    return before, after, saved_tokens, by_tool


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    a = ap.parse_args()
    if not os.path.exists(a.db):
        sys.exit(f"no such database: {a.db}")
    copy, tmp = mr.copy_db(a.db)
    try:
        sessions = mr.analyse(copy)
        real_ids = {s["session_id"] for s in sessions if s["kind"] == "real"}
        sess_items = tool_texts(copy, real_ids)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    real = [s for s in sessions if s["kind"] == "real"]
    comp = defaultdict(int)
    for s in real:
        for c in s["calls"]:
            for k in mr.BREAKDOWN_KEYS:
                comp[k] += c["breakdown"].get(k) or 0
    tot_comp = sum(comp.values()) or 1
    print("MEASURED - input_tokens_breakdown, all model calls of real-model sessions")
    for k in mr.BREAKDOWN_KEYS:
        print(f"  {k:<18}{int(comp[k]):>12}  {100*comp[k]/tot_comp:5.1f}%")

    tool_bytes = defaultdict(int)
    tool_count = defaultdict(int)
    for s in real:
        for name, b in s["tool_bytes"].items():
            tool_bytes[name] += b
        for name, n in s["tool_count"].items():
            tool_count[name] += n
    tot_tool = sum(tool_bytes.values()) or 1
    print("\nMEASURED - tool.response bytes by tool (real-model sessions)")
    for name, b in sorted(tool_bytes.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<28}{tool_count[name]:>5} results {b:>10} bytes {100*b/tot_tool:5.1f}%")

    total_before = total_after = 0
    total_saved = 0.0
    trimmed_by_tool = defaultdict(lambda: [0, 0])
    for sid, items in sess_items.items():
        b, a, s, by_tool = simulate(items)
        total_before += b
        total_after += a
        total_saved += s
        for name, (bb, aa) in by_tool.items():
            trimmed_by_tool[name][0] += bb
            trimmed_by_tool[name][1] += aa
    print(f"\nESTIMATED (method in module docstring) - simulated trimming, cap={CAP} chars, dedup on")
    print(f"  tool-result bytes: {total_before} -> {total_after} "
          f"({100*(total_before-total_after)/max(total_before,1):.1f}% less)")
    print(f"  input tokens saved, summed over later calls: ~{int(total_saved):,}")
    total_input = sum(c["input_tokens"] for s in real for c in s["calls"]) or 1
    print(f"  as a share of measured input tokens ({total_input:,}): ~{100*total_saved/total_input:.1f}%")
    print("\n  per tool (bytes before -> after under the simulated rule):")
    for name, (bb, aa) in sorted(trimmed_by_tool.items(), key=lambda kv: -kv[1][0]):
        print(f"    {name:<28}{bb:>10} -> {aa:>10}")


if __name__ == "__main__":
    main()
