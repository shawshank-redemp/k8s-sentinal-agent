# Resume bullets (measured numbers only)

Each bullet states a measured result. Items that are not measured are listed under the bullets, so
nothing below implies them.

- Built a stdlib metrics pipeline that reads a copy of an agent platform's session database and reports
  token totals and the input-token breakdown by component for **12 real-model sessions (257 model calls)**,
  excluding 102 mock sessions identified by their fixed usage signature.
- Found that an approval pause in the platform's **new** version (0.185.1) appears as a `paused` turn
  with the request as an event, not as the `done` state the watcher expected; reworked the watcher to read
  unanswered requests from events, decide through the events endpoint, and fail closed on any unknown
  state. **59 Java unit tests pass.** Verified end to end on a local cluster with a scripted model.
- Built a fault-injection harness over **7 Kubernetes faults**. A detection miss found by the harness (quiet
  `Pending` pods were never revisited) was traced to its cause and fixed. With the scripted model, detection
  now matches the expected result for **7 of 7** faults. The one fault the scripted model can repair ended with
  the pod Ready (checked with `kubectl`).
- Measured that **tool results were 72% of tool-output bytes** (`kubectl_get` and `kubectl_describe`) and
  that the **messages component was 52.5%** of input tokens across real-model calls; a local simulation estimates
  about 12.9% fewer input tokens under a trimming rule (an estimate, not a measurement).

## Not measured (do not claim these)

- Any real-model accuracy, or how well a real model diagnoses or repairs the seven faults.
- Time to detect per fault.
- Cost in dollars (no prices entered; `eval/prices.json` is blank).
- Whether the one bounded retry improves outcomes with a real model.
- Whether the trimming flag saves tokens in a live run.
- Behaviour on Daytona, or on any TrueForge version other than 0.185.1.
- The cause of the two detection misses (missing ConfigMap, unschedulable pod).
