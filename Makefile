.PHONY: test lint typecheck run-chaos report clean docker-up docker-down sim-config simulate dashboard

test:
	pytest -q

lint:
	ruff check src tests scripts

typecheck:
	mypy src

run-chaos:
	python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json

report:
	python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md

docker-up:
	docker compose up -d

docker-down:
	docker compose down

sim-config:
	python scripts/generate_simulation_config.py --seed 42 --out configs/simulation.yaml

simulate:
	python scripts/simulate_users.py --config configs/simulation.yaml --requests 100 --seed 42

dashboard: simulate
	python scripts/build_html_dashboard.py

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache reports/metrics.json reports/final_report.md reports/simulation_*.json
