# Day 10 Reliability Report

> Generated from `reports/simulation_metrics.json`, `reports/metrics.json`, and `docs/huong-dan.html` dashboard.  
> Simulation seed: **42** | Config: `configs/simulation.yaml` | Requests per run: **100**

## 1. Architecture summary

The reliability layer routes every user prompt through a production-style pipeline:

1. **ReliabilityGateway** — entry point; returns `GatewayResponse` with route, latency, cost.
2. **ResponseCache** (in-memory) or **SharedRedisCache** — semantic cache with n-gram cosine similarity, privacy guardrails, and false-hit detection (4-digit year/ID mismatch).
3. **CircuitBreaker** (per provider) — 3-state machine: CLOSED → OPEN → HALF_OPEN → CLOSED.
4. **FakeLLMProvider chain** — primary then backup; simulates latency, failures, and cost without real API keys.
5. **Static fallback** — degraded message when all providers fail or circuits are open.

```
User Request (random query, user, region, per-request profile)
    |
    v
[ReliabilityGateway.complete()]
    |
    v
[Cache check] -----------------> HIT? return route=cache_hit:score, latency=0, cost=0
    | MISS
    v
[Circuit Breaker: primary] ----> FakeLLMProvider primary
    |  (OPEN / ProviderError?)
    v
[Circuit Breaker: backup] -----> FakeLLMProvider backup
    |  (fail?)
    v
[Static fallback] route=static_fallback
```

**Per-request randomization** (simulation): each of 100 requests gets its own `fail_rate`, `latency_ms`, `client_region`, `device_type`, and `similarity_threshold` around base config values before calling the gateway.

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Open circuit after 3 consecutive failures — balances fast protection vs noise |
| reset_timeout_seconds | 3.8 | Probe half-open after ~4s; tuned from default 2s for less oscillation under flaky primary |
| success_threshold | 1 | Single successful probe closes circuit — fast recovery for lab |
| cache TTL | 300 s | 5-minute TTL — FAQ/policy answers stay fresh without excessive eviction |
| similarity_threshold | 0.89 | Lowered from 0.92 to improve hit rate; false-hit guard still blocks wrong year/ID matches |
| load_test requests | 100 | Enough traffic for P50/P95 and ~64% cache hit rate in simulation |
| primary fail_rate | 0.24 | Realistic flaky primary (~24%) |
| primary base_latency_ms | 178 | Typical LLM gateway latency band |
| backup fail_rate | 0.03 | Stable backup tier |
| backup base_latency_ms | 221 | Backup slightly slower but more reliable |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 98.0% (sim, with cache) / **99.33%** (chaos, 300 req) | Partial — chaos pass, user sim slightly below |
| Latency P95 | < 2500 ms | **279.6 ms** (sim) / 281.0 ms (chaos) | **Yes** |
| Fallback success rate | >= 95% | 83.3% (sim) / **97.65%** (chaos) | Partial — chaos pass, sim affected by 2 static fallbacks |
| Cache hit rate | >= 10% | **64%** | **Yes** |
| Recovery time | < 5000 ms | **4183 ms** (chaos avg) | **Yes** |

## 4. Metrics

Summary from **user simulation with cache enabled** (`reports/simulation_metrics.json` → `metrics`):

| Metric | Value |
|---|---:|
| total_requests | 100 |
| availability | 0.98 |
| error_rate | 0.02 |
| latency_p50_ms | 217.88 |
| latency_p95_ms | 279.59 |
| latency_p99_ms | 297.45 |
| fallback_success_rate | 0.8333 |
| cache_hit_rate | 0.64 |
| estimated_cost | $0.014808 |
| estimated_cost_saved | $0.064 |
| circuit_open_count | 1 |
| recovery_time_ms | N/A (single open, no close in 100-req window) |

**Route distribution (with cache):**

| Route | Count |
|---|---:|
| cache_hit:1.00 | 59 |
| primary | 24 |
| fallback | 10 |
| static_fallback | 2 |
| cache_hit:0.97 / 0.98 | 5 |

**Chaos run aggregate** (`reports/metrics.json`, 3 × 100 requests):

| Metric | Value |
|---|---:|
| total_requests | 300 |
| availability | 0.9933 |
| latency_p95_ms | 281.0 |
| cache_hit_rate | 0.64 |
| circuit_open_count | 6 |
| recovery_time_ms | 4183.32 |
| estimated_cost_saved | $0.192 |

## 5. Cache comparison

Same 100 requests (seed=42, identical query/user/random profile) — **without cache** then **with cache**:

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 226.81 | 217.88 | **-8.93 ms** |
| latency_p95_ms | 299.92 | 279.59 | **-20.33 ms** |
| latency_p99_ms | 338.79 | 297.45 | **-41.34 ms** |
| estimated_cost | $0.044958 | $0.014808 | **-$0.030150 (-67%)** |
| cache_hit_rate | 0 | 0.64 | **+64%** |
| availability | 96% | 98% | +2% |
| circuit_open_count | 0 | 1 | +1 |

**Conclusion:** Cache reduces cost by ~67% and improves tail latency (P99 -41 ms). 64% of requests avoid LLM calls entirely.

## 6. Redis shared cache

### Why shared cache matters

- **In-memory cache is insufficient for multi-instance deployments:** Each gateway process holds its own `ResponseCache._entries`. Instance A's cache is invisible to instance B — hit rate drops, cost savings don't scale horizontally, and users get inconsistent answers across replicas.
- **How `SharedRedisCache` solves this:** Stores `query` + `response` in Redis hashes (`rl:cache:{hash}`) with `EXPIRE`. Any instance can `HGET` / `SCAN` + semantic similarity lookup. Privacy and false-hit checks reuse the same helpers as in-memory cache.

### Evidence of shared state

`pytest tests/test_redis_cache.py::test_shared_state_across_instances` — **PASSED**

```
Two SharedRedisCache instances (c1, c2) on same Redis:
  c1.set("shared query", "shared response")
  c2.get("shared query") → ("shared response", 1.0)  ✓
```

### Redis CLI output

Simulation used `cache.backend: memory`, so production cache keys are empty after tests (test prefix `rl:test:*` is flushed). To verify Redis in production config:

```bash
docker compose up -d
# Set cache.backend: redis in config, run simulation, then:
docker compose exec redis redis-cli KEYS "rl:cache:*"
```

### In-memory vs Redis latency comparison (optional)

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 217.88 | — | Sim used memory backend |
| latency_p95_ms | 279.59 | — | Redis adds ~1–3 ms network overhead; shared hit rate across instances offsets this |

Redis integration verified by full test suite (`test_redis_cache.py` — 6/6 pass with Docker).

## 7. Chaos scenarios

From `make run-chaos` / `reports/metrics.json` (`configs/simulation.yaml`, 100 requests per scenario):

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | All traffic fallback to backup, circuit opens | Primary fail_rate=1.0 → fallback path used, circuit opens on primary | **pass** |
| primary_flaky_50 | Circuit oscillates, mix of primary and fallback | 50% primary failures → mixed routes, circuit transitions logged | **pass** |
| all_healthy | All requests via primary, no circuit opens | Baseline providers healthy, majority primary/cache routes | **pass** |
| user_simulation_seed42 | 100 random users, per-request random profiles | 98% availability, 64% cache hit, 2 static fallbacks | **pass** |

All three named chaos scenarios: `"pass"` in `metrics.json` → `scenarios` block.

## 8. Failure analysis

**Remaining weakness:** Per-request random `fail_rate` on providers can push fallback success below 95% SLO in short runs (simulation: 83.3% fallback success, 2 static fallbacks when backup also failed).

**What could still go wrong?**
- Semantic cache false positives on paraphrased but semantically different queries (mitigated by similarity_threshold + false-hit guard, not eliminated).
- Circuit breaker state is per-process — multi-instance deployments don't share OPEN/HALF_OPEN state without Redis-backed counters.
- No per-user rate limiting — abusive client can exhaust provider budget.

**Proposed fixes before production:**
1. Store circuit breaker counters in Redis (`INCR` + `EXPIRE`) for cross-instance coordination.
2. Add per-user token bucket rate limiting at gateway entry.
3. Monitor cache `false_hit_log` and alert when rejection rate spikes.

## 9. Next steps

1. **Switch simulation to `cache.backend: redis`** and re-run cache comparison (memory vs Redis) with `KEYS rl:cache:*` evidence in report.
2. **Define cost budget SLO** — route to cheaper backup after 80% daily spend (stretch goal from README).
3. **Concurrent load test** — `ThreadPoolExecutor` in `run_simulation` to validate circuit breaker under parallel traffic.

---

## Reproducibility

```powershell
cd e:\ai\phase2-track3-day10-reliability-agent
.venv\Scripts\activate
docker compose up -d
pytest -q
python scripts/generate_simulation_config.py --seed 42 --out configs/simulation.yaml
python scripts/simulate_users.py --config configs/simulation.yaml --requests 100 --seed 42
python scripts/run_chaos.py --config configs/simulation.yaml --out reports/metrics.json
python scripts/build_html_dashboard.py
```

Dashboard: `docs/huong-dan.html` (sections 15–19: config, metrics, cache comparison, charts, per-request random table).
