# Results: what was measured, and what was not

**Every fault-injection result below used the scripted mock model. None used a real model. Nothing here
measures model accuracy.** Mock rows are plumbing checks. Each row is labelled MOCK where it appears.

## 1. Stored real-model usage (measured, 12 sessions)

Source: TrueForge's session store, copied and read with `eval/metrics_report.py`. Real-model sessions only;
mock sessions (102, identified by the mock's fixed 10/10 usage) and 2 sessions with no model call are excluded.
Model: `openai/gpt-4-1` (assumed from the TrueForge log; the model id is not stored per call).

| measure | value |
|---|---|
| sessions | 12 |
| model calls | 257 |
| input tokens (summed over calls) | 2,556,310 |
| output tokens | 35,775 |
| cache-read tokens | 2,016,384 |
| TrueForge-recorded duration | 1,083 s total |
| **cost** | **not computed** (`eval/prices.json` has no prices) |

Input-token breakdown by component, all calls (TrueForge `input_tokens_breakdown`):

| component | tokens | share |
|---|---|---|
| messages (conversation and tool results) | 1,346,205 | 52.5% |
| harness | 657,309 | 25.6% |
| instructions | 561,112 | 21.9% |
| skills, tool_definitions | 0 | 0% |

Tool results, by bytes (real-model sessions): `kubectl_get` 36.8%, `kubectl_describe` 35.4%, `exec` 14.0%,
`get_tool_info` 10.3%, all others under 2%.

**Not measured:** whether these runs reached a correct outcome; whether the counts match the provider's
bill (cache handling is as TrueForge records it, not reconciled with the provider).

## 2. Estimated effect of trimming tool output (estimate, not a measurement)

`eval/token_analysis.py` simulates a rule on the stored tool results: cap each result at 2,000 characters,
and replace a byte-identical repeat within a run with a one-line pointer. Tokens are approximated as bytes / 4.
Each removed byte is counted once for every later model call in the same run, because results are re-sent.

- Tool-result bytes: 279,787 → 182,279 (**−34.9%**, simulated).
- Input tokens saved, summed over later calls: about **330,000** (**≈12.9%** of the measured input, estimate).
- Caveat: `exec` results grew slightly under the rule, because the replacement markers are longer than the
  short results they replace.

**Not measured:** the effect on a live run, on quality, or on cost. The trimming flag
(`SENTINEL_TRIM_TOOL_OUTPUT=1`) is implemented but **off by default**, and its effect on a real model is unmeasured.

## 3. Fault-injection runs (MOCK model: plumbing check, not accuracy)

Runner: `eval/fault_runner.py --rehearse`. Table: `eval/faults.json`. Only `sentinel-demo-app` has a mock
script; every other fault gets no tool calls from the mock, so its approval outcome is NOT EXERCISED.

| run (file) | fault | mock or real | detected | expected detect | approval asked | run | pod Ready after approval | status |
|---|---|---|---|---|---|---|---|---|
| 140816 | crash-missing-env | MOCK | yes | yes | yes (runner approved) | finished | yes (kubectl) | MATCH |
| 140816 | readiness-probe-fails | MOCK | no | no | no | none | n/a | MATCH |
| 144125 (retry script) | crash-missing-env | MOCK | yes | yes | yes | finished | yes (kubectl) | MATCH |
| 143801 (rejudged) | oom-low-limit | MOCK | yes | yes | no | finished | n/a | NOT EXERCISED |
| 143801 (rejudged) | image-bad-tag | MOCK | yes | yes | no | finished | n/a | NOT EXERCISED |
| 143801 (rejudged) | bad-command | MOCK | yes | yes | no | finished | n/a | NOT EXERCISED |
| 143801 (rejudged) | missing-configmap | MOCK | **no** | yes | no | none | n/a | **MISMATCH (detection)** |
| 143801 (rejudged) | unschedulable-memory | MOCK | **no** | yes | no | none | n/a | **MISMATCH (detection)** |

Notes:
- Results `20261118T140406Z` is superseded: its cluster-state check read the wrong pods (wrong label), so
  its "pod Ready after: no" is wrong. Use `20261118T140816Z`.
- Results `20261118T143801Z.md` predates the detection-first labelling; use `20261118T143801Z-rejudged.md`.
- Detection in the table: 5 of 7 faults detected as expected. The 2 misses (missing ConfigMap, unschedulable)
  were not investigated further. The cause is unknown; no `WARN` or exception appeared in the watcher log.
- "Pod Ready after approval" is read with `kubectl` from the Deployment's own selector, not from the agent's text.

**Not measured:** time to detect (the runner does not record it); sandbox fidelity (not scored); anything
about the outcome of the five unscripted faults with a real model.

## 4. Watcher behaviour against TrueForge 0.185.1 (unit tests and mock end to end)

- Java unit tests: **59 passing** (DetectionModuleTest 16, TrueForgeAgentTriggerTest 26, DiagnosisModuleTest 10,
  PodInfoTest 7). The fake TrueForge server is not TrueForge; these tests check the watcher's handling of the
  shapes described in the SDK, not a live service.
- Verified end to end on Kind with the mock: approval read from the events, decided through the events
  endpoint, the paused turn resumed in place, the run finished, the production pod became Ready.

**Not measured:** any of this with a real model, and any TrueForge version other than 0.185.1.

## 5. Not measured anywhere in this work

- Any real-model accuracy, or how a real model does on the seven faults.
- Time to detect, per fault.
- The effect of the one bounded sandbox retry on a real model (the instruction exists; the mock shows plumbing only).
- Cost in dollars (no prices entered).
- The five unscripted faults under a real model, and the two undetected faults' cause.
- Daytona sandbox behaviour (not run).
- The effect of `SENTINEL_TRIM_TOOL_OUTPUT=1` on a live run.
