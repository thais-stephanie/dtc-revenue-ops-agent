.PHONY: brief evals test seed dump
brief:  ; PYTHONPATH=src python3 -m revenue_agent.daily
evals:  ; python3 evals/runner.py
test:   ; python3 -m unittest discover -s tests
seed:   ; python3 scripts/seed_demo.py
dump:   ; python3 scripts/seed_demo.py --dump build/seed.sql
