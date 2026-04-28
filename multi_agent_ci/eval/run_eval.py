#!/usr/bin/env python3
# eval/run_eval.py
# Evaluation harness for Multi-Agent CI System.
# Runs the full pipeline across held-out scenarios and measures reliability.
#
# Usage: python eval/run_eval.py [--dry-run] [--scenario tesla_public_large]

import sys
import os
import yaml
import json
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.WARNING)

from config import EVAL_SUCCESS_RATE, EVAL_RUNS_PER_SCENARIO
from agents import ui_agent_intake, run_pipeline
from oracles import run_all_oracles

EVAL_CONFIG_PATH = Path(__file__).parent / "ci_eval_config.yaml"


def load_config() -> dict:
    with open(EVAL_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def classify_failure(oracle_results: dict, state: dict, error: str) -> str:
    """Classify why a run failed into one of the taxonomy categories."""
    if error and ("exception" in error.lower() or "traceback" in error.lower()):
        return "pipeline_crash"
    if not oracle_results.get("overall_passed"):
        failing = oracle_results.get("failing_oracles", [])
        if "O2" in failing:
            return "fabrication"
        if "O1" in failing:
            return "incomplete_section"
        if state.get("retries", 0) >= 5:
            return "loop_nonconverge"
        return "silent_incorrect"
    return "pass"


def run_scenario(scenario: dict, run_number: int, dry_run: bool = False) -> dict:
    """Run a single scenario once and return results."""
    company = scenario["company"]
    run_id  = f"eval_{scenario['name']}_r{run_number}"

    print(f"  Run {run_number}: {company}...", end=" ", flush=True)
    start = time.time()

    if dry_run:
        # Return mock result for testing the harness itself
        time.sleep(0.1)
        return {
            "run_id":        run_id,
            "company":       company,
            "scenario":      scenario["name"],
            "run_number":    run_number,
            "passed":        True,
            "oracle_results":{"overall_passed": True, "O1":{"passed":True,"violations":[]},
                               "O2":{"passed":True,"violations":[]}, "O3":{"passed":True,"violations":[]},
                               "O4":{"passed":True,"violations":[]}, "O5":{"passed":True,"violations":[],"trace_rate":1.0}},
            "failure_class": "pass",
            "retries":       0,
            "duration_s":    0.1,
            "brief_length":  500,
        }

    try:
        state = ui_agent_intake(company, run_id=run_id)
        if state is None:
            return {
                "run_id": run_id, "company": company, "scenario": scenario["name"],
                "run_number": run_number, "passed": False,
                "oracle_results": {"overall_passed": False},
                "failure_class": "pipeline_crash",
                "error": "Company validation failed",
                "retries": 0, "duration_s": time.time() - start, "brief_length": 0,
            }

        state["_run_id"] = run_id  # type: ignore
        final = run_pipeline(state)

        oracle_results = run_all_oracles(final)
        duration = time.time() - start
        failure_class = classify_failure(oracle_results, final, final.get("error", ""))

        brief = final.get("brief", "")
        if oracle_results["overall_passed"]:
            print(f"PASS ({duration:.1f}s)")
        else:
            print(f"FAIL — {failure_class} ({duration:.1f}s)")

        return {
            "run_id":        run_id,
            "company":       company,
            "scenario":      scenario["name"],
            "run_number":    run_number,
            "passed":        oracle_results["overall_passed"],
            "oracle_results":oracle_results,
            "failure_class": failure_class,
            "retries":       final.get("retries", 0),
            "duration_s":    round(duration, 2),
            "brief_length":  len(brief),
            "error":         final.get("error", ""),
        }

    except Exception as e:
        duration = time.time() - start
        print(f"CRASH — {e} ({duration:.1f}s)")
        return {
            "run_id":        run_id,
            "company":       company,
            "scenario":      scenario["name"],
            "run_number":    run_number,
            "passed":        False,
            "oracle_results":{"overall_passed": False},
            "failure_class": "pipeline_crash",
            "error":         str(e),
            "retries":       0,
            "duration_s":    round(duration, 2),
            "brief_length":  0,
        }


def compute_metrics(results: list[dict]) -> dict:
    """Compute aggregate eval metrics from all run results."""
    total = len(results)
    passed = sum(1 for r in results if r["passed"])

    # Per-oracle stats
    oracle_pass = {f"O{i}": 0 for i in range(1, 6)}
    oracle_total = {f"O{i}": 0 for i in range(1, 6)}
    for r in results:
        o = r.get("oracle_results", {})
        for oracle in oracle_pass:
            if oracle in o:
                oracle_total[oracle] += 1
                if o[oracle].get("passed"):
                    oracle_pass[oracle] += 1

    # Failure taxonomy
    failure_counts: dict[str, int] = {}
    for r in results:
        fc = r.get("failure_class", "unknown")
        failure_counts[fc] = failure_counts.get(fc, 0) + 1

    # Retry stats
    retries = [r.get("retries", 0) for r in results]
    avg_retries = sum(retries) / len(retries) if retries else 0

    # Duration stats
    durations = [r.get("duration_s", 0) for r in results]
    avg_duration = sum(durations) / len(durations) if durations else 0

    return {
        "total_runs":      total,
        "passed":          passed,
        "failed":          total - passed,
        "success_rate":    round(passed / total, 3) if total else 0,
        "oracle_rates":    {o: round(oracle_pass[o] / oracle_total[o], 3) if oracle_total[o] else None
                            for o in oracle_pass},
        "failure_taxonomy":failure_counts,
        "avg_retries":     round(avg_retries, 2),
        "avg_duration_s":  round(avg_duration, 1),
        "target_rate":     EVAL_SUCCESS_RATE,
        "meets_threshold": (passed / total >= EVAL_SUCCESS_RATE) if total else False,
    }


def generate_report(metrics: dict, results: list[dict], config: dict) -> str:
    """Generate a markdown eval report."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %Human:%M:%S UTC").replace(" %Human", "")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    status = "PASS" if metrics["meets_threshold"] else "FAIL"
    status_emoji = "✅" if metrics["meets_threshold"] else "❌"

    lines = [
        f"# Eval Report — Multi-Agent CI System",
        f"Generated: {ts}",
        f"",
        f"## Summary",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Overall Status | {status_emoji} {status} |",
        f"| Success Rate | {metrics['success_rate']*100:.1f}% (target: {metrics['target_rate']*100:.0f}%) |",
        f"| Total Runs | {metrics['total_runs']} |",
        f"| Passed | {metrics['passed']} |",
        f"| Failed | {metrics['failed']} |",
        f"| Avg Retries | {metrics['avg_retries']} |",
        f"| Avg Duration | {metrics['avg_duration_s']}s |",
        f"",
        f"## Per-Oracle Pass Rates",
        f"| Oracle | Pass Rate | Description |",
        f"|--------|-----------|-------------|",
    ]

    oracle_descs = {
        "O1": "Six-Section Completeness",
        "O2": "No Data Fabrication",
        "O3": "Competitor Coverage",
        "O4": "Recency Check",
        "O5": "Anti-Hallucination Trace",
    }
    for oracle, rate in metrics["oracle_rates"].items():
        rate_str = f"{rate*100:.1f}%" if rate is not None else "N/A"
        lines.append(f"| {oracle} | {rate_str} | {oracle_descs.get(oracle, '')} |")

    lines += [
        "",
        "## Failure Taxonomy",
        "| Category | Count |",
        "|----------|-------|",
    ]
    for cat, count in sorted(metrics["failure_taxonomy"].items(), key=lambda x: -x[1]):
        lines.append(f"| {cat} | {count} |")

    lines += ["", "## Per-Scenario Results", ""]
    scenarios_seen = {}
    for r in results:
        sname = r["scenario"]
        if sname not in scenarios_seen:
            scenarios_seen[sname] = {"passed": 0, "total": 0}
        scenarios_seen[sname]["total"] += 1
        if r["passed"]:
            scenarios_seen[sname]["passed"] += 1

    for sname, counts in scenarios_seen.items():
        rate = counts["passed"] / counts["total"] * 100
        lines.append(f"### {sname}")
        lines.append(f"Pass rate: {rate:.0f}% ({counts['passed']}/{counts['total']})")
        lines.append("")

    lines += [
        "## Detailed Run Log",
        "| Run ID | Company | Passed | Retries | Duration | Failure Class |",
        "|--------|---------|--------|---------|----------|---------------|",
    ]
    for r in results:
        lines.append(
            f"| {r['run_id']} | {r['company']} | {'✅' if r['passed'] else '❌'} | "
            f"{r['retries']} | {r['duration_s']}s | {r.get('failure_class', 'N/A')} |"
        )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Run CI eval harness")
    parser.add_argument("--dry-run",  action="store_true", help="Mock pipeline calls (test harness only)")
    parser.add_argument("--scenario", type=str, default=None, help="Run only this scenario name")
    parser.add_argument("--runs",     type=int, default=None, help="Override runs_per_scenario")
    args = parser.parse_args()

    config   = load_config()
    scenarios = config["held_out_scenarios"]
    runs_each = args.runs or config.get("runs_per_scenario", EVAL_RUNS_PER_SCENARIO)

    if args.scenario:
        scenarios = [s for s in scenarios if s["name"] == args.scenario]
        if not scenarios:
            print(f"Scenario '{args.scenario}' not found in config")
            sys.exit(1)

    total_runs = len(scenarios) * runs_each
    print(f"\nMulti-Agent CI Eval Harness")
    print(f"{'='*50}")
    print(f"Scenarios:     {len(scenarios)}")
    print(f"Runs each:     {runs_each}")
    print(f"Total runs:    {total_runs}")
    print(f"Target rate:   {config['target_success_rate']*100:.0f}%")
    if args.dry_run:
        print(f"Mode:          DRY RUN (no real API calls)")
    print()

    all_results = []

    for scenario in scenarios:
        print(f"\n[Scenario] {scenario['name']} — {scenario['company']}")
        print(f"  {scenario.get('description', '')}")
        for run_num in range(1, runs_each + 1):
            result = run_scenario(scenario, run_num, dry_run=args.dry_run)
            all_results.append(result)
            # Small delay between runs to avoid rate limiting
            if not args.dry_run and run_num < runs_each:
                time.sleep(2)

    metrics = compute_metrics(all_results)
    report  = generate_report(metrics, all_results, config)

    # Save report
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = Path(__file__).parent / f"eval_report_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")

    # Save raw results
    results_path = Path(__file__).parent / f"eval_results_{timestamp}.json"
    results_path.write_text(json.dumps(all_results, indent=2, default=str), encoding="utf-8")

    print(f"\n{'='*50}")
    print(f"EVAL COMPLETE")
    print(f"{'='*50}")
    print(f"Success rate:  {metrics['success_rate']*100:.1f}% (target: {metrics['target_rate']*100:.0f}%)")
    print(f"Status:        {'PASS' if metrics['meets_threshold'] else 'FAIL'}")
    print(f"Report:        {report_path}")
    print(f"Results JSON:  {results_path}")
    print()
    print("Per-oracle rates:")
    for oracle, rate in metrics["oracle_rates"].items():
        if rate is not None:
            print(f"  {oracle}: {rate*100:.1f}%")

    return 0 if metrics["meets_threshold"] else 1


if __name__ == "__main__":
    sys.exit(main())
