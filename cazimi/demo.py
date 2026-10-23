"""A fully synthetic offline demonstration of the persistent research loop."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path

from .runtime import ResearchRuntime, stamp, utc_now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cazimi synthetic offline demonstration; makes no market or model requests")
    parser.add_argument("--workspace", type=Path, default=Path(".cazimi/demo"))
    args = parser.parse_args(argv)
    root = args.workspace.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        parser.error("Choose an empty demonstration workspace; existing files are preserved.")
    root.mkdir(parents=True, exist_ok=True)
    now = utc_now() - timedelta(seconds=3)
    feed = root / "synthetic-feed.json"

    def write_feed(value: float, when) -> None:
        feed.write_text(json.dumps({"observations": [
            {"id": "margin", "ticker": "DEMO", "kind": "metric", "as_of": stamp(when),
             "metric": "operating_margin", "value": value, "unit": "fraction", "period": "FY2026",
             "citation": "https://example.invalid/synthetic-margin", "title": "Fictional demonstration margin"},
            {"id": "price", "ticker": "DEMO", "kind": "price", "as_of": stamp(when),
             "value": 100, "unit": "USD", "citation": "https://example.invalid/synthetic-price",
             "title": "Fictional demonstration quote"}]}), encoding="utf-8")

    source = {"id": "illustrative-source", "kind": "json", "location": str(feed),
              "category": "portfolio_models_trackers", "quality": "user", "request_cost": 0}
    watch = {"id": "demo-thesis", "ticker": "DEMO", "thesis": "Fictional margins support a hypothetical valuation.",
             "horizon_days": 1, "pillars": [{"id": "margin", "claim": "Fictional operating margin remains healthy.",
                 "metric": "operating_margin", "unit": "fraction", "period": "FY2026", "source_ids": ["illustrative-source"],
                 "confirm": {"op": "gte", "value": .3}, "warning": {"op": "lt", "value": .25},
                 "kill": {"op": "lt", "value": .15}, "threshold_origin": "draft"}],
             "valuation": {"currency": "USD", "as_of": stamp(now), "scenarios": [
                 {"name": "bull", "probability": .2, "price": 140},
                 {"name": "base", "probability": .6, "price": 120},
                 {"name": "bear", "probability": .2, "price": 70}],
                 "hurdle_return": .1, "max_loss_fraction": .35}}
    write_feed(.35, now)
    runtime = ResearchRuntime(root)
    try:
        runtime.add_source(source)
        runtime.add_watch(watch)
        runtime.approve("demo-thesis")
        runtime.schedule(60, now=now)
        first = runtime.run(now=now, force=False)
        unchanged = runtime.run(now=now + timedelta(seconds=1))
        later = now + timedelta(seconds=2)
        write_feed(.1, later)
        adverse = runtime.run(now=later)
        runtime.pause()
        errors = runtime.verify() + runtime.verify_research()
        print("SYNTHETIC OFFLINE DEMONSTRATION: fictional security, assertions, rules, prices and simulated clock.")
        print("No market data, model requests, external notifications, or trades occurred.")
        print("Only fictional monitoring rules were approved. Source quality remains 'user'.")
        print("Due scheduled run:", first["reports"]["demo-thesis"]["security_readiness"])
        print("Unchanged poll:", len(unchanged["changed_theses"]), "changed theses")
        print("Adverse fictional metric:", adverse["reports"]["demo-thesis"]["company_thesis_status"],
              "/", adverse["reports"]["demo-thesis"]["position_action"])
        print("Integrity:", "verified" if not errors else "; ".join(errors))
        print("The demonstration schedule is paused. Inspect with:")
        print(f'python -m cazimi --workspace "{root}" -c /reports -c /alerts -c /jobs')
        return 1 if errors else 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
