#!/usr/bin/env python3
"""Fault-injection runner for Sentinel. PLUMBING CHECK ONLY when run with the mock model.

    python3 eval/fault_runner.py --rehearse [--only id,id] [--detect-timeout 150] [--run-timeout 480]

What it does, against a local Kind cluster only (refuses any non-kind context):
  1. Starts whatever is not already running: both Kubernetes MCP servers (scripts/start-k8s-mcp.sh),
     the scripted mock model on :9911, the TrueForge registration (scripts/setup-trueforge.sh with
     SENTINEL_REHEARSE=1, which registers no paid provider), and the Java watcher in ui approval mode.
  2. For each fault in eval/faults.json: applies its manifest, waits for detection, follows the
     agent run, and records what happened.
  3. When the agent asks for approval, the runner answers it through TrueForge's API, the same call
     the dashboard makes. It approves every request, so the run measures whether the agent PROPOSES a
     production change, not whether the proposal is good.
  4. Scores against the table, using ground truth where it exists: detection (watcher log), proposal
     (approval requested), and cluster state (pod Ready, read with kubectl) after any approval.
  5. Writes eval/results/<stamp>.json and .md. Both are labelled as a plumbing check.

Not measured here, and not auto-scored: whether the sandbox reproduction was faithful, and the
accuracy of any model. The mock has a script for sentinel-demo-app only; every other fault gets NO
tool calls from the mock, so its result is "not exercised", not a failure of the agent.
Stdlib only. Never calls a paid API: rehearsal registers no paid provider, and the mock is local.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTINEL_DIR = os.path.join(REPO, ".sentinel")
RESULTS_DIR = os.path.join(REPO, "eval", "results")
KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-sentinel-demo")
TF = os.environ.get("TRUEFORGE_URL", "http://localhost:8790")
MOCK_PORT = 9911
BANNER = "MOCK MODEL: plumbing check, not accuracy."
PAID_ENV = ("OPENAI_API_KEY", "TRUEFOUNDRY_API_KEY", "TRUEFOUNDRY_GATEWAY_URL", "DAYTONA_API_KEY")


def env_clean():
    """Environment for every child process: paid-provider credentials removed."""
    e = {k: v for k, v in os.environ.items() if k not in PAID_ENV}
    jdk = "/opt/homebrew/opt/openjdk@21"  # keg-only Homebrew JDK, as scripts/lib.sh does
    if os.path.exists(os.path.join(jdk, "bin", "java")):
        e["JAVA_HOME"] = jdk
        e["PATH"] = os.path.join(jdk, "bin") + os.pathsep + e.get("PATH", "")
    e["SENTINEL_REHEARSE"] = "1"
    e["SENTINEL_RUN_MODE"] = "rehearsal"
    return e


def kubectl(*args, check=False):
    r = subprocess.run(["kubectl", "--context", KUBE_CONTEXT, *args], capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def up(url):
    try:
        urllib.request.urlopen(url, timeout=3)
        return True
    except urllib.error.HTTPError:
        return True  # something answered
    except Exception:
        return False


def tf_get(path):
    with urllib.request.urlopen(f"{TF}/api/v1{path}", timeout=10) as r:
        return json.loads(r.read()).get("data")


def tf_post(path, body):
    req = urllib.request.Request(f"{TF}/api/v1{path}", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Keep TrueForge's own error text: it is the only way to see why a call was rejected.
        raise RuntimeError(f"POST {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from None


# --------------------------------------------------------------------------------------------
# Preflight: start only what is missing
# --------------------------------------------------------------------------------------------

class Stack:
    def __init__(self):
        self.procs = []  # processes this runner started, stopped at the end

    def start_bg(self, argv, logname, cwd=REPO):
        log = open(os.path.join(SENTINEL_DIR, logname), "a")
        p = subprocess.Popen(argv, cwd=cwd, env=env_clean(), stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def ensure(self):
        if not up(f"{TF}/api/v1/capabilities"):
            sys.exit(f"TrueForge is not reachable at {TF}. Start it: npx @truefoundry/trueforge@0.185.1")
        if not up("http://127.0.0.1:3001/health") or not up("http://127.0.0.1:3002/health"):
            print("==> starting Kubernetes MCP servers (scripts/start-k8s-mcp.sh)")
            subprocess.run([os.path.join(REPO, "scripts", "start-k8s-mcp.sh")], check=True, env=env_clean(),
                           cwd=REPO)
        if not up(f"http://127.0.0.1:{MOCK_PORT}/v1/models"):
            print(f"==> starting the scripted mock model on :{MOCK_PORT}")
            self.start_bg([sys.executable, os.path.join(REPO, "demo", "mock-llm", "mock_llm.py"),
                           "--scenario", "crash", "--port", str(MOCK_PORT)], "eval-mock.log")
            for _ in range(20):
                if up(f"http://127.0.0.1:{MOCK_PORT}/v1/models"):
                    break
                time.sleep(1)
        print("==> registering connectors and agent (rehearsal: no paid provider)")
        subprocess.run([os.path.join(REPO, "scripts", "setup-trueforge.sh")], check=True, env=env_clean(), cwd=REPO)

    def start_watcher(self):
        print("==> starting the Sentinel watcher (ui approval mode, 6s transient wait)")
        env = env_clean()
        env.update({"SENTINEL_KUBE_CONTEXT": KUBE_CONTEXT, "SENTINEL_APPROVAL_MODE": "ui",
                    "SENTINEL_TRANSIENT_WAIT_SECONDS": "6"})
        log_path = os.path.join(SENTINEL_DIR, "eval-watcher.log")
        open(log_path, "w").close()
        log = open(log_path, "a")
        p = subprocess.Popen(["mvn", "-q", "compile", "exec:java",
                              "-Dexec.mainClass=com.sentinel.detection.DetectionModule"],
                             cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(p)
        return log_path

    def stop(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    pass


# --------------------------------------------------------------------------------------------
# One fault
# --------------------------------------------------------------------------------------------

def read_from(path, offset):
    with open(path, errors="replace") as f:
        f.seek(offset)
        return f.read()


def approve_pending(session_id):
    """Answer the paused approval the same way the dashboard does. Returns the tool name asked for."""
    # Newer TrueForge: a paused turn has no required_actions. The pending request is a
    # tool.approval_required EVENT on the latest turn, same as the watcher reads it.
    turns = tf_get(f"/sessions/{session_id}/turns?limit=25") or []
    latest = sorted(turns, key=lambda t: t["created_at"])[-1]
    events = tf_get(f"/sessions/{session_id}/turns/{latest['id']}/events?limit=100") or []
    approval = next((e for e in events if e.get("type") == "tool.approval_required"), None)
    if not approval:
        return None
    ref = (approval.get("tool_calls") or [{}])[0]
    # The tool name is on the model message that issued the call.
    name = "unknown"
    for e in events:
        if e.get("type") == "model.message" and e.get("id") == ref.get("source_event_id"):
            for tc in e.get("tool_calls") or []:
                if tc.get("id") == ref.get("id"):
                    name = tc.get("function", {}).get("name") or name
                    if name == "call_tool":
                        name = json.loads(tc["function"].get("arguments") or "{}").get("tool_name", name)
    # Approvals are not new turns. The public turn-create endpoint accepts only user.message items;
    # a paused turn takes its decision on its own /events endpoint, as {"events": [...]}.
    tf_post(f"/sessions/{session_id}/turns/{latest['id']}/events", {"events": [{
        "type": "user.tool_approval", "thread_id": approval.get("thread_id", "main"),
        "tool_call_id": ref.get("id"), "approval": {"status": "allow"}}]})
    return name


def pods_ready(deployment):
    # Use the Deployment's own selector: pod labels do not always match the Deployment name
    # (broken-pod.yaml labels its pods app=sentinel-demo, for example).
    d = kubectl("get", "deployment", deployment, "-n", "default", "-o", "json")
    if d.returncode != 0:
        return None
    labels = json.loads(d.stdout).get("spec", {}).get("selector", {}).get("matchLabels", {})
    if not labels:
        return None
    selector = ",".join(f"{k}={v}" for k, v in labels.items())
    r = kubectl("get", "pods", "-n", "default", "-l", selector, "-o", "json")
    if r.returncode != 0:
        return None
    items = json.loads(r.stdout or '{"items": []}').get("items", [])
    if not items:
        return False
    ready = []
    for p in items:
        conds = {c["type"]: c["status"] for c in p.get("status", {}).get("conditions", [])}
        ready.append(conds.get("Ready") == "True")
    return all(ready)


def run_fault(fault, watcher_log, detect_timeout, run_timeout, settle):
    manifest = os.path.join(REPO, fault["manifest"])
    dep = fault["deployment"]
    rec = {"id": fault["id"], "deployment": dep, "mock_scripted": fault["mock_scripted"],
           "expected": {"detected": fault["expected_detected"], "outcome": fault["expected_outcome"],
                        "sandbox": fault["expected_sandbox"]},
           "detected": False, "pod": None, "session_id": None, "run_outcome": "none",
           "approval_requested": False, "tool_proposed": None, "approved": False,
           "pod_ready_after": None, "notes": []}

    # Clean slate for this fault only.
    kubectl("delete", "-f", manifest, "--ignore-not-found")
    kubectl("delete", "configmap", "fault-config", "-n", "default", "--ignore-not-found")
    kubectl("delete", "all", "--all", "-n", "sentinel-sandbox", "--ignore-not-found")
    time.sleep(3)

    offset = os.path.getsize(watcher_log)
    kubectl("apply", "-f", manifest, check=True)

    # Phase A: wait for detection.
    timeout = detect_timeout if fault["expected_detected"] else min(detect_timeout, 90)
    deadline = time.time() + timeout
    pod_re = re.compile(r"DETECTED: Pod failure - (" + re.escape(dep) + r"-\S+) \[")
    while time.time() < deadline:
        text = read_from(watcher_log, offset)
        m = pod_re.search(text)
        if m:
            rec["detected"], rec["pod"] = True, m.group(1)
            break
        time.sleep(2)
    if not rec["detected"]:
        rec["notes"].append(f"no DETECTED line for {dep} within {timeout}s")
        return rec

    # Phase B: follow the run. The watcher prints the session id when it starts the run.
    deadline = time.time() + run_timeout
    session = None
    while time.time() < deadline:
        text = read_from(watcher_log, offset)
        if session is None:
            s = re.search(r"TrueForge agent '[^']+' started for (?:default/)?" + re.escape(rec["pod"]) + r" \(session (\S+?)\)", text)
            if s:
                session = s.group(1)
                rec["session_id"] = session
        if session and "APPROVAL REQUIRED" in text and not rec["approval_requested"]:
            rec["approval_requested"] = True
            tool = approve_pending(session)
            rec["tool_proposed"] = tool
            rec["approved"] = tool is not None
            if tool is None:
                rec["notes"].append("approval line seen but no paused approval found in TrueForge")
        # The watcher prints the workload as namespace/pod ("default/<pod>"), so accept an optional prefix.
        pod_ref = r"(?:default/)?" + re.escape(rec["pod"])
        if re.search(r"Agent finished for " + pod_ref + r"\b", text):
            rec["run_outcome"] = "finished"
            break
        if re.search(r"Lost track of TrueForge run for " + pod_ref + r"\b", text) or \
           re.search(r"run for " + pod_ref + r" failed", text):
            rec["run_outcome"] = "failed"
            break
        time.sleep(2)
    else:
        rec["run_outcome"] = "timeout"
        rec["notes"].append(f"run did not finish within {run_timeout}s")

    if session is None:
        rec["notes"].append("no TrueForge session started (dedup or cooldown, or start failed)")

    # Phase C: ground truth from the cluster, not from the agent's narration.
    if rec["approved"]:
        time.sleep(settle)
        rec["pod_ready_after"] = pods_ready(dep)
    return rec


def judge(rec):
    exp = rec["expected"]
    det_ok = rec["detected"] == exp["detected"]
    if not rec["mock_scripted"] and exp["outcome"] == "approve" and not rec["approval_requested"]:
        status = "NOT EXERCISED (mock has no script for this fault)"
    else:
        want_approval = exp["outcome"] == "approve"
        out_ok = rec["approval_requested"] == want_approval
        status = "MATCH" if (det_ok and out_ok) else "MISMATCH"
    rec["detection_match"] = det_ok
    rec["status"] = status
    return rec


def write_results(recs, stamp):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    base = os.path.join(RESULTS_DIR, stamp)
    with open(base + ".json", "w") as f:
        json.dump({"banner": BANNER, "stamp": stamp, "faults": recs}, f, indent=2)
    lines = [f"# Fault-injection plumbing check - {stamp}", "", f"**{BANNER}**", "",
             "Expected values are what a correct agent should do with a real model; they are not "
             "measured. Only detection, proposal and cluster state are auto-scored. Sandbox fidelity "
             "is not scored.", "",
             "| fault | expected detect | detected | expected outcome | approval asked | run | pod Ready after | status |",
             "|---|---|---|---|---|---|---|---|"]
    for r in recs:
        ready = "-" if r["pod_ready_after"] is None else ("yes" if r["pod_ready_after"] else "no")
        lines.append(f"| {r['id']} | {r['expected']['detected']} | {r['detected']} | {r['expected']['outcome']} "
                     f"| {r['approval_requested']} | {r['run_outcome']} | {ready} | {r['status']} |")
    lines += ["", "## Notes per fault"]
    for r in recs:
        lines.append(f"- **{r['id']}**: " + ("; ".join(r["notes"]) or "no notes"))
    with open(base + ".md", "w") as f:
        f.write("\n".join(lines) + "\n")
    return base


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rehearse", action="store_true", required=True,
                    help="required: this runner only ever uses the scripted mock model")
    ap.add_argument("--only", default="", help="comma-separated fault ids")
    ap.add_argument("--detect-timeout", type=int, default=150)
    ap.add_argument("--run-timeout", type=int, default=480)
    ap.add_argument("--settle", type=int, default=45)
    a = ap.parse_args()

    if not KUBE_CONTEXT.startswith("kind-"):
        sys.exit(f"refusing: context {KUBE_CONTEXT} is not a kind context")
    if kubectl("cluster-info").returncode != 0:
        sys.exit(f"cannot reach cluster {KUBE_CONTEXT}; create it with: kind create cluster --name sentinel-demo")

    with open(os.path.join(REPO, "eval", "faults.json")) as f:
        faults = json.load(f)["faults"]
    if a.only:
        wanted = set(a.only.split(","))
        faults = [x for x in faults if x["id"] in wanted]

    stack = Stack()
    try:
        stack.ensure()
        watcher_log = stack.start_watcher()
        time.sleep(15)  # let the watcher subscribe before the first fault
        recs = []
        for fault in faults:
            print(f"==> fault {fault['id']}")
            rec = judge(run_fault(fault, watcher_log, a.detect_timeout, a.run_timeout, a.settle))
            print(f"    detected={rec['detected']} asked_approval={rec['approval_requested']} "
                  f"run={rec['run_outcome']} -> {rec['status']}")
            recs.append(rec)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = write_results(recs, stamp)
        print(f"\n{BANNER}\nwrote {base}.md and {base}.json")
    finally:
        # Remove the fault workloads this run created; leave TrueForge and the MCP servers as found.
        for fault in faults:
            kubectl("delete", "-f", os.path.join(REPO, fault["manifest"]), "--ignore-not-found")
        kubectl("delete", "configmap", "fault-config", "-n", "default", "--ignore-not-found")
        stack.stop()


if __name__ == "__main__":
    main()
