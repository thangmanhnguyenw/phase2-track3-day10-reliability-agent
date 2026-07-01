"""Simulate random user traffic with per-request randomized config and logging."""

from __future__ import annotations

import argparse
import copy
import json
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from reliability_lab.chaos import build_gateway
from reliability_lab.config import LabConfig, load_config
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


REGIONS = ("ap-southeast-1", "us-east-1", "eu-west-1", "ap-northeast-1", "sa-east-1")
DEVICE_TYPES = ("web", "mobile", "api", "cli")


@dataclass
class RequestProfile:
    query_id: str
    primary_fail_rate: float
    primary_latency_ms: int
    backup_fail_rate: float
    backup_latency_ms: int
    client_region: str
    device_type: str
    think_time_ms: float
    similarity_threshold: float
    ttl_seconds: int


@dataclass
class RequestPlan:
    row: dict[str, str]
    user_id: str
    profile: RequestProfile


@dataclass
class RequestLog:
    request_id: str
    user_id: str
    timestamp: float
    query_id: str
    query: str
    query_risk: str | None
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None
    success: bool
    cache_enabled: bool
    client_region: str
    device_type: str
    think_time_ms: float
    primary_fail_rate: float
    primary_latency_ms: int
    backup_fail_rate: float
    backup_latency_ms: int
    similarity_threshold: float


def load_queries_with_meta(path: str | Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def random_request_profile(rng: random.Random, config: LabConfig, query_id: str = "") -> RequestProfile:
    primary = next(p for p in config.providers if p.name == "primary")
    backup = next(p for p in config.providers if p.name == "backup")

    return RequestProfile(
        query_id=query_id,
        primary_fail_rate=round(
            _clamp(rng.uniform(primary.fail_rate * 0.6, primary.fail_rate * 1.4), 0.02, 0.45), 3
        ),
        primary_latency_ms=int(
            _clamp(rng.gauss(primary.base_latency_ms, primary.base_latency_ms * 0.15), 80, 450)
        ),
        backup_fail_rate=round(
            _clamp(rng.uniform(backup.fail_rate * 0.5, backup.fail_rate * 1.8), 0.01, 0.20), 3
        ),
        backup_latency_ms=int(
            _clamp(rng.gauss(backup.base_latency_ms, backup.base_latency_ms * 0.12), 100, 550)
        ),
        client_region=rng.choice(REGIONS),
        device_type=rng.choice(DEVICE_TYPES),
        think_time_ms=round(rng.uniform(0, 300), 1),
        similarity_threshold=round(
            _clamp(rng.gauss(config.cache.similarity_threshold, 0.02), 0.80, 0.98),
            2,
        ),
        ttl_seconds=int(_clamp(rng.gauss(config.cache.ttl_seconds, 60), 120, 900)),
    )


def generate_request_plans(
    rng: random.Random,
    query_rows: list[dict[str, str]],
    config: LabConfig,
    num_requests: int,
) -> list[RequestPlan]:
    plans: list[RequestPlan] = []
    for i in range(num_requests):
        row = rng.choice(query_rows)
        profile = random_request_profile(rng, config, row.get("id", f"q{i}"))
        plans.append(
            RequestPlan(
                row=row,
                user_id=f"user_{rng.randint(1000, 9999)}",
                profile=profile,
            )
        )
    return plans


def apply_profile_to_gateway(
    gateway_providers: list[FakeLLMProvider],
    cache: object | None,
    profile: RequestProfile,
) -> None:
    for provider in gateway_providers:
        if provider.name == "primary":
            provider.fail_rate = profile.primary_fail_rate
            provider.base_latency_ms = profile.primary_latency_ms
        elif provider.name == "backup":
            provider.fail_rate = profile.backup_fail_rate
            provider.base_latency_ms = profile.backup_latency_ms
    if cache is not None and hasattr(cache, "similarity_threshold"):
        cache.similarity_threshold = profile.similarity_threshold
    if cache is not None and hasattr(cache, "ttl_seconds"):
        cache.ttl_seconds = profile.ttl_seconds


def _metrics_snapshot(metrics: RunMetrics) -> dict[str, float | int | None]:
    report = metrics.to_report_dict()
    return {
        "total_requests": int(report["total_requests"]),
        "availability": float(report["availability"]),
        "error_rate": float(report["error_rate"]),
        "latency_p50_ms": float(report["latency_p50_ms"]),
        "latency_p95_ms": float(report["latency_p95_ms"]),
        "latency_p99_ms": float(report["latency_p99_ms"]),
        "cache_hit_rate": float(report["cache_hit_rate"]),
        "estimated_cost": float(report["estimated_cost"]),
        "estimated_cost_saved": float(report["estimated_cost_saved"]),
        "circuit_open_count": int(report["circuit_open_count"]),
        "fallback_success_rate": float(report["fallback_success_rate"]),
    }


def simulate_with_plans(
    config: LabConfig,
    plans: list[RequestPlan],
    *,
    cache_enabled: bool,
    per_request_random: bool = True,
    skip_think_time: bool = False,
) -> tuple[RunMetrics, list[RequestLog], dict]:
    run_config = copy.deepcopy(config)
    run_config.cache.enabled = cache_enabled
    gateway = build_gateway(run_config)

    metrics = RunMetrics()
    logs: list[RequestLog] = []
    route_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {}

    for i, plan in enumerate(plans):
        prompt = plan.row["query"]
        profile = plan.profile

        if per_request_random:
            apply_profile_to_gateway(gateway.providers, gateway.cache, profile)
            if not skip_think_time:
                time.sleep(profile.think_time_ms / 1000.0)

        ts = time.time()
        result = gateway.complete(prompt)
        success = result.route != "static_fallback"

        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms > 0:
            metrics.latencies_ms.append(result.latency_ms)

        route_counts[result.route] = route_counts.get(result.route, 0) + 1
        region_counts[profile.client_region] = region_counts.get(profile.client_region, 0) + 1
        if result.error:
            error_counts[result.error] = error_counts.get(result.error, 0) + 1

        logs.append(
            RequestLog(
                request_id=f"req_{i + 1:04d}",
                user_id=plan.user_id,
                timestamp=ts,
                query_id=profile.query_id,
                query=prompt[:80] + ("..." if len(prompt) > 80 else ""),
                query_risk=plan.row.get("expected_risk"),
                route=result.route,
                provider=result.provider,
                cache_hit=result.cache_hit,
                latency_ms=round(result.latency_ms, 2),
                estimated_cost=round(result.estimated_cost, 6),
                error=result.error,
                success=success,
                cache_enabled=cache_enabled,
                client_region=profile.client_region,
                device_type=profile.device_type,
                think_time_ms=profile.think_time_ms,
                primary_fail_rate=profile.primary_fail_rate,
                primary_latency_ms=profile.primary_latency_ms,
                backup_fail_rate=profile.backup_fail_rate,
                backup_latency_ms=profile.backup_latency_ms,
                similarity_threshold=profile.similarity_threshold,
            )
        )

    for breaker in gateway.breakers.values():
        metrics.circuit_open_count += sum(
            1 for t in breaker.transition_log if t["to"] == "open"
        )

    summary = {
        "route_distribution": route_counts,
        "error_distribution": error_counts,
        "region_distribution": region_counts,
        "unique_users": len({log.user_id for log in logs}),
        "cache_enabled": cache_enabled,
        "per_request_random": per_request_random,
    }
    return metrics, logs, summary


def run_cache_comparison(
    config_path: str,
    queries_path: str,
    num_requests: int,
    seed: int,
    per_request_random: bool = True,
) -> dict:
    """Run identical request sequence with cache ON vs OFF."""
    rng = random.Random(seed)
    config = load_config(config_path)
    query_rows = load_queries_with_meta(queries_path)
    plans = generate_request_plans(rng, query_rows, config, num_requests)

    metrics_no, logs_no, _ = simulate_with_plans(
        config, plans, cache_enabled=False, per_request_random=per_request_random, skip_think_time=True
    )

    metrics_yes, logs_yes, summary_yes = simulate_with_plans(
        config, plans, cache_enabled=True, per_request_random=per_request_random, skip_think_time=True
    )

    without = _metrics_snapshot(metrics_no)
    with_cache = _metrics_snapshot(metrics_yes)

    def delta(key: str, fmt: str = "num") -> float | str:
        a = without[key]
        b = with_cache[key]
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return "-"
        d = b - a
        if fmt == "pct":
            return f"{d * 100:+.1f}%"
        if fmt == "cost":
            return f"{d:+.6f}"
        return f"{d:+.2f}"

    comparison = {
        "without_cache": without,
        "with_cache": with_cache,
        "delta": {
            "latency_p50_ms": delta("latency_p50_ms"),
            "latency_p95_ms": delta("latency_p95_ms"),
            "latency_p99_ms": delta("latency_p99_ms"),
            "estimated_cost": delta("estimated_cost", "cost"),
            "cache_hit_rate": delta("cache_hit_rate", "pct"),
            "availability": delta("availability", "pct"),
        },
        "note": "Same request sequence (seed fixed); cache OFF run first, then cache ON.",
    }

    return {
        "comparison": comparison,
        "metrics": with_cache,
        "metrics_no_cache": without,
        "summary": {**summary_yes, "seed": seed, "config_path": config_path},
        "logs_with_cache": logs_yes,
        "logs_no_cache": logs_no,
    }


def simulate_users(
    config_path: str,
    queries_path: str,
    num_requests: int,
    seed: int,
    per_request_random: bool = True,
) -> tuple[RunMetrics, list[RequestLog], dict]:
    rng = random.Random(seed)
    config = load_config(config_path)
    query_rows = load_queries_with_meta(queries_path)
    plans = generate_request_plans(rng, query_rows, config, num_requests)
    metrics, logs, summary = simulate_with_plans(
        config, plans, cache_enabled=config.cache.enabled, per_request_random=per_request_random
    )
    summary["seed"] = seed
    summary["config_path"] = config_path
    return metrics, logs, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/simulation.yaml")
    parser.add_argument("--queries", default="data/sample_queries.jsonl")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-per-request-random", action="store_true")
    parser.add_argument("--no-cache-compare", action="store_true")
    parser.add_argument("--out-metrics", default="reports/simulation_metrics.json")
    parser.add_argument("--out-requests", default="reports/simulation_requests.json")
    args = parser.parse_args()

    per_request_random = not args.no_per_request_random
    out_metrics = Path(args.out_metrics)
    out_requests = Path(args.out_requests)
    out_metrics.parent.mkdir(parents=True, exist_ok=True)

    config_text = Path(args.config).read_text(encoding="utf-8")
    config_lines = [ln for ln in config_text.splitlines() if not ln.strip().startswith("#")]
    config_snapshot = yaml.safe_load("\n".join(config_lines))

    if not args.no_cache_compare:
        result = run_cache_comparison(
            args.config, args.queries, args.requests, args.seed, per_request_random
        )
        payload = {
            "metrics": result["metrics"],
            "metrics_no_cache": result["metrics_no_cache"],
            "cache_comparison": result["comparison"],
            "summary": result["summary"],
            "config_snapshot": config_snapshot,
        }
        logs = result["logs_with_cache"]
        print("cache comparison:")
        for key in ("latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"):
            w = result["comparison"]["without_cache"][key]
            c = result["comparison"]["with_cache"][key]
            d = result["comparison"]["delta"][key]
            print(f"  {key}: {w} -> {c} (delta {d})")
    else:
        metrics, logs, summary = simulate_users(
            args.config, args.queries, args.requests, args.seed, per_request_random
        )
        payload = {
            "metrics": metrics.to_report_dict(),
            "summary": summary,
            "config_snapshot": config_snapshot,
        }

    out_metrics.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    out_requests.write_text(
        json.dumps([asdict(log) for log in logs], indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    print(f"wrote {out_metrics}")
    print(f"wrote {out_requests} ({len(logs)} requests)")


if __name__ == "__main__":
    main()
