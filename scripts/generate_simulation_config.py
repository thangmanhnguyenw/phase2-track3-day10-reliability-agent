"""Generate a production-realistic config with randomized tunable parameters."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import yaml


def generate_config(seed: int | None = None) -> dict:
    rng = random.Random(seed)

    primary_latency = rng.randint(150, 280)
    backup_latency = rng.randint(primary_latency + 40, primary_latency + 150)
    primary_fail = round(rng.uniform(0.12, 0.28), 2)
    backup_fail = round(rng.uniform(0.02, 0.08), 2)
    primary_cost = round(rng.uniform(0.008, 0.015), 4)
    backup_cost = round(primary_cost * rng.uniform(0.45, 0.75), 4)

    return {
        "providers": [
            {
                "name": "primary",
                "fail_rate": primary_fail,
                "base_latency_ms": primary_latency,
                "cost_per_1k_tokens": primary_cost,
            },
            {
                "name": "backup",
                "fail_rate": backup_fail,
                "base_latency_ms": backup_latency,
                "cost_per_1k_tokens": backup_cost,
            },
        ],
        "circuit_breaker": {
            "failure_threshold": rng.randint(3, 6),
            "reset_timeout_seconds": round(rng.uniform(2.0, 5.0), 1),
            "success_threshold": rng.randint(1, 3),
        },
        "cache": {
            "enabled": True,
            "backend": "memory",
            "ttl_seconds": rng.choice([300, 450, 600, 900]),
            "similarity_threshold": round(rng.uniform(0.88, 0.95), 2),
            "redis_url": "redis://localhost:6379/0",
        },
        "load_test": {"requests": 100},
        "scenarios": [
            {
                "name": "primary_timeout_100",
                "description": "Primary provider fails 100% — all traffic should fallback",
                "provider_overrides": {"primary": 1.0},
            },
            {
                "name": "primary_flaky_50",
                "description": "Primary provider fails 50% — circuit should oscillate",
                "provider_overrides": {"primary": 0.5},
            },
            {
                "name": "all_healthy",
                "description": "Baseline — both providers healthy",
                "provider_overrides": {},
            },
        ],
        "_meta": {
            "seed": seed,
            "description": "Auto-generated realistic production-like parameters",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="configs/simulation.yaml")
    args = parser.parse_args()

    config = generate_config(args.seed)
    meta = config.pop("_meta")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = (
        f"# Auto-generated simulation config (seed={meta['seed']})\n"
        f"# {meta['description']}\n"
    )
    out_path.write_text(header + yaml.dump(config, default_flow_style=False, sort_keys=False))
    print(f"wrote {out_path} (seed={meta['seed']})")


if __name__ == "__main__":
    main()
