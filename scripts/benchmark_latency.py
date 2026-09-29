"""Measure complete AI requests versus gateway and Nebius round-trip time.

Requires local Noesis started with --trace-inference. Runs a short synthetic
preview with real Kimi calls. Writes only non-secret timings and provenance.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import statistics
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "local"


def combine_timings(responses: list[dict], gateway_rows: list[dict]) -> list[dict]:
    by_id = {row["response_id"]: row for row in gateway_rows
             if row.get("event") == "provider_success" and row.get("response_id")}
    results = []
    for response in responses:
        gateway = by_id.get(response["response_id"])
        if gateway is None:
            continue
        total = response["latency_ms"]
        remainder = total - gateway["elapsed_ms"]
        if remainder < -2:
            raise ValueError("Gateway span exceeds complete request time; timing correlation is invalid.")
        results.append({
            **{key: response[key] for key in ("agent_id", "request_id", "response_id", "model", "input_tokens", "output_tokens")},
            "source_media_time_s": response["media_time_s"],
            "observed_after_s": response["observed_after_s"],
            "total_ms": total,
            "nebius_round_trip_ms": gateway["upstream_ms"],
            "gateway_processing_ms": gateway["gateway_processing_ms"],
            "local_request_overhead_ms": round(max(0, remainder), 3),
        })
    return results


def summarize(rows: list[dict]) -> dict:
    def stats(items):
        return {"count": len(items), **{
            key: round(statistics.median(row[key] for row in items), 3)
            for key in ("total_ms", "nebius_round_trip_ms", "gateway_processing_ms", "local_request_overhead_ms")
        }} if items else {"count": 0}
    return {"all": stats(rows), "camera": stats([r for r in rows if r["agent_id"].startswith("camera-")]),
            "critic": stats([r for r in rows if r["agent_id"] == "critic"]),
            "director": stats([r for r in rows if r["agent_id"] == "director"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout-s", type=float, default=110)
    parser.add_argument("--label", default="", help="Optional safe suffix for the private report filename.")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 5 or not 10 <= args.timeout_s <= 180:
        parser.error("Use 1–5 rounds and a 10–180 second timeout.")
    if args.label and not re.fullmatch(r"[a-z0-9-]{1,48}", args.label):
        parser.error("Use a label of 1–48 lowercase letters, digits, or hyphens.")
    responses = {}
    report = {"profile": "kimi", "requested_rounds": args.rounds, "notes": [
        "Nebius round trip includes outbound network, provider queue, and inference; server compute is not separately observable.",
        "Local request overhead is the residual outside the gateway; it includes Flower model tasks only with the flower inference transport.",
        "Per-role component medians need not sum to the median total.",
        "Same role prompts and synthetic scenario across transports; live observations and generated reasons are not byte-identical.",
    ]}
    with httpx.Client(base_url="http://127.0.0.1:8765", timeout=15, trust_env=False) as client:
        def post(path, body=None):
            response = client.post(path, json=body or {})
            response.raise_for_status()
            return response.json()
        state = client.get("/api/state").json()
        if state["session"]["status"] in {"running", "paused"}:
            raise RuntimeError("Stop the active replay before benchmarking.")
        if state["flower"].get("deployment") != "local":
            raise RuntimeError("Start the local Flower launcher with --trace-inference first.")
        if state["flower"].get("status") != "connected":
            raise RuntimeError("Wait for the local six-role crew to connect before benchmarking.")
        report["inference_transport"] = state["flower"].get("inference_transport", "flower")
        post("/api/models/select", {"profile": "kimi"})
        started_session = False
        try:
            state = post("/api/session/start", {"input_mode": "synthetic", "output_mode": "preview"})
            started_session = True
            session_id = state["session"]["id"]
            report["run"] = state["flower"]["grid_run"]
            report["session_id"] = session_id
            start = time.monotonic()
            completed = set()
            while time.monotonic() - start < args.timeout_s:
                response = client.get("/api/state")
                response.raise_for_status()
                state = response.json()
                for row in state["flower"]["inference_results"]:
                    if row["session_id"] != session_id or row["response_id"] in responses:
                        continue
                    responses[row["response_id"]] = {
                        **{key: row[key] for key in ("agent_id", "request_id", "response_id", "model", "input_tokens", "output_tokens", "media_time_s", "latency_ms")},
                        "observed_after_s": round(time.monotonic() - start, 3),
                    }
                    if row["agent_id"] == "director":
                        completed.add(row["request_id"])
                        print(json.dumps({"completed_rounds": len(completed), "observed_after_s": responses[row["response_id"]]["observed_after_s"]}), flush=True)
                if len(completed) >= args.rounds:
                    break
                time.sleep(0.2)
            report["completed_rounds"] = len(completed)
            report["metrics"] = state["metrics"]
        finally:
            if started_session:
                post("/api/session/stop")
    gateway_rows = []
    path = RUNTIME / "gateway-timings.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                gateway_rows.append(json.loads(line))
            except ValueError:
                continue
    report["calls"] = combine_timings(list(responses.values()), gateway_rows)
    report["unmatched_calls"] = len(responses) - len(report["calls"])
    report["summary"] = summarize(report["calls"])
    director_times = sorted(row["observed_after_s"] for row in report["calls"] if row["agent_id"] == "director")
    report["first_decision_s"] = director_times[0] if director_times else None
    report["subsequent_decision_intervals_s"] = [round(right - left, 3) for left, right in zip(director_times, director_times[1:])]
    report["passed"] = report["completed_rounds"] == args.rounds and report["unmatched_calls"] == 0
    destination = RUNTIME / (f"latency-benchmark-{args.label}.json" if args.label else "latency-benchmark.json")
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "summary": report["summary"], "report": str(destination)}), flush=True)
    if not report["passed"]:
        raise SystemExit("Incomplete measurement. Check tracing is enabled and the crew is healthy.")


if __name__ == "__main__":
    main()
