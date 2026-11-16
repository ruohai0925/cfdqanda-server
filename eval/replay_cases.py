#!/usr/bin/env python3
"""replay_cases.py — re-run real user cases against a Foam-Agent build.

For judging an upstream upgrade (or a model) on the requests that actually
failed in production, under production's resource envelope: same prompt the
worker sent (prompt.txt from Storage, platform notes included), 5 GB memory,
the user's chosen time limit, single core.

Runs Foam-Agent through its official CLI (src/main.py), in the worker image,
one container per (case, model). Records the verdict from three independent
sources, because no single one is trustworthy on its own:
  * process / container exit  (OOM kill, wall-clock timeout, exit status)
  * Foam-Agent's own <workflow_end> line
  * the OpenFOAM solver log  (ends with "End" only on a clean run)

Usage:
  python3 eval/replay_cases.py --cases-dir RUNS/replay --fa-dir FA_CHECKOUT \
      --lane gpt --cases 783 790 785 [--image cfdqanda-worker:fa-next]
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from supabase_rest import load_env  # noqa: E402

LANES = {
    # default platform model, ChatGPT/Codex subscription
    "gpt":  {"FOAMAGENT_MODEL_PROVIDER": "openai-codex", "FOAMAGENT_MODEL_VERSION": "gpt-5.6-sol"},
    # subscription Claude through the local claude-bridge (OpenAI-compatible)
    "opus": {"FOAMAGENT_MODEL_PROVIDER": "openai", "FOAMAGENT_MODEL_VERSION": "claude-opus-5-5"},
}
UTILITY_LOGS = {"blockMesh", "snappyHexMesh", "checkMesh", "setFields", "decomposePar",
                "reconstructPar", "surfaceFeatures", "surfaceFeatureExtract", "topoSet",
                "createPatch", "extrudeMesh", "transformPoints", "renumberMesh", "mapFields",
                "foamToVTK", "postProcess", "gmshToFoam", "fluentMeshToFoam", "refineMesh"}


def solver_outcome(output: Path):
    """Same rule as worker._detect_solver_outcome: trust only the solver log."""
    logs = [p for p in output.rglob("log.*")
            if p.is_file() and p.name[4:] not in UTILITY_LOGS]
    done = None
    for p in sorted(logs):
        tail = p.read_text(errors="replace")[-65536:]
        m = re.search(r"-->\s*FOAM FATAL (?:IO )?ERROR:?\s*(.{0,300})", tail, re.S)
        if m or "FOAM FATAL" in tail:
            msg = " ".join((m.group(1) if m else "").split()).split("From function")[0].strip()
            return {"status": "fatal", "solver": p.name[4:], "detail": msg[:240]}
        if tail.rstrip().endswith("End"):
            done = p.name[4:]
    if done:
        return {"status": "completed", "solver": done}
    if logs:
        return {"status": "incomplete", "solver": sorted(logs)[-1].name[4:]}
    return {"status": "none"}


def verdict(r):
    """One-word outcome for the summary table."""
    if r["oom_killed"]:
        return "OOM"
    if r["timed_out"]:
        return "TIMEOUT"
    so = r["solver"]["status"]
    if r["workflow_end"].startswith("Workflow completed successfully") and so == "completed":
        return "PASS"
    if "visualization failed" in r["workflow_end"] and so == "completed":
        return "PASS (no viz)"
    if so == "completed":
        return "SOLVED, FA says fail"
    return "FAIL"


def run_one(case_id, lane, args):
    case = Path(args.cases_dir) / str(case_id)
    meta = {m["id"]: m for m in json.load(open(Path(args.cases_dir) / "manifest.json"))}[case_id]
    tag = f"{case_id}-{lane}"
    work = case / lane
    out = work / "output"
    subprocess.run(["docker", "run", "--rm", "-v", f"{case}:/c", "alpine", "rm", "-rf", f"/c/{lane}"],
                   check=False, capture_output=True)
    work.mkdir(parents=True, exist_ok=True)

    fa_mount = "/home/openfoam/Foam-Agent"
    rel = lambda p: f"{fa_mount}/{Path(p).resolve().relative_to(Path(args.fa_dir).resolve())}"
    cli = ["python", "src/main.py", "--prompt_path", rel(case / "prompt.txt"),
           "--output_dir", rel(out)]
    mesh = next((p for p in case.iterdir() if p.suffix in (".msh", ".unv", ".cas")), None)
    if mesh:
        cli += ["--custom_mesh_path", rel(mesh)]
    timeout_s = int(meta["timeout_min"]) * 60

    env = dict(LANES[lane])
    docker = ["docker", "run", "--name", f"replay-{tag}", "--memory", args.memory,
              "--memory-swap", args.memory, "--entrypoint", "bash",
              "-v", f"{Path(args.fa_dir).resolve()}:{fa_mount}",
              "-v", f"{os.path.expanduser('~/.codex')}:/home/openfoam/.codex:ro",
              "-v", f"{args.hf_cache}:/home/openfoam/.cache/huggingface",
              "-w", fa_mount]
    if lane == "opus":
        env["OPENAI_API_BASE"] = os.environ["CLAUDE_BRIDGE_URL"]
        env["OPENAI_API_KEY"] = os.environ["CLAUDE_BRIDGE_TOKEN"]
        docker += ["--network", args.network]
    for k, v in env.items():
        docker += ["-e", f"{k}={v}"]
    inner = ("source /opt/conda/etc/profile.d/conda.sh && conda activate FoamAgent && "
             "source /opt/openfoam10/etc/bashrc 2>/dev/null; "
             f"timeout --signal=TERM --kill-after=30 {timeout_s} "
             + " ".join(f"'{a}'" for a in cli)
             + f" > {rel(work / 'simulation.log')} 2>&1; echo $? > {rel(work / 'rc')}")
    docker += [args.image, "-lc", inner]

    t0 = time.time()
    subprocess.run(["docker", "rm", "-f", f"replay-{tag}"], capture_output=True)
    proc = subprocess.run(docker, capture_output=True, text=True)
    elapsed = time.time() - t0
    insp = subprocess.run(["docker", "inspect", f"replay-{tag}", "--format",
                           "{{.State.OOMKilled}} {{.State.ExitCode}}"],
                          capture_output=True, text=True).stdout.split()
    subprocess.run(["docker", "rm", "-f", f"replay-{tag}"], capture_output=True)

    log = (work / "simulation.log").read_text(errors="replace") if (work / "simulation.log").exists() else ""
    rc_file = work / "rc"
    inner_rc = int(rc_file.read_text().strip()) if rc_file.exists() and rc_file.read_text().strip() else None
    end = re.findall(r"<workflow_end>(.*?)</workflow_end>", log)
    err = re.findall(r"<workflow_error>(.*?)</workflow_error>", log, re.S)
    tok = re.search(r"Total tokens:\s*(\d+)", log)
    calls = re.search(r"Total calls:\s*(\d+)|Number of calls:\s*(\d+)|Total LLM calls:\s*(\d+)", log)
    r = {
        "case": case_id, "lane": lane, "elapsed_min": round(elapsed / 60, 1),
        "oom_killed": bool(insp and insp[0] == "true"),
        "container_exit": int(insp[1]) if len(insp) > 1 else proc.returncode,
        "cli_exit": inner_rc,
        "timed_out": inner_rc == 124,
        "workflow_end": end[-1] if end else "",
        "workflow_error": " ".join(err[-1].split())[:300] if err else "",
        "solver": solver_outcome(out) if out.exists() else {"status": "none"},
        "total_tokens": int(tok.group(1)) if tok else None,
        "llm_calls": next((int(g) for g in calls.groups() if g), None) if calls else None,
        "report": (out / ".foamagent" / "workflow_report.html").exists(),
        "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    r["verdict"] = verdict(r)
    (work / "result.json").write_text(json.dumps(r, indent=1, ensure_ascii=False))
    print(json.dumps(r, ensure_ascii=False), flush=True)
    return r


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-dir", required=True)
    ap.add_argument("--fa-dir", required=True, help="Foam-Agent checkout to test (bind-mounted)")
    ap.add_argument("--lane", choices=LANES, required=True)
    ap.add_argument("--cases", type=int, nargs="+", required=True)
    ap.add_argument("--image", default="cfdqanda-worker:fa-next")
    ap.add_argument("--memory", default="5g", help="production worker limit")
    ap.add_argument("--hf-cache", default="/mnt/bigdata/hf-cache")
    ap.add_argument("--network", default="cfdqanda-server_default",
                    help="docker network that can reach claude-bridge (opus lane)")
    args = ap.parse_args()
    for cid in args.cases:
        run_one(cid, args.lane, args)


if __name__ == "__main__":
    main()
