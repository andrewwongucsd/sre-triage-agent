.PHONY: install test demo eval eval-real gen-cases compare-judges compare-judges-ref clean

VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

test:
	$(PY) -m pytest tests/ -q

demo:            ## run the agent on one incident with the mock backend
	$(PY) -m sre_triage --incident checkout-db-pool --model mock

eval:            ## offline eval + regression gate (no API key needed)
	$(PY) evals/run.py --model mock --judge mock --validate-judge --gate 0.50

eval-real:       ## real benchmark with Claude (needs ANTHROPIC_API_KEY)
	$(PY) evals/run.py --model anthropic --judge anthropic --validate-judge --gate 0.75

gen-cases:       ## regenerate evals/cases.jsonl from the fixtures
	$(PY) scripts/gen_cases.py

compare-judges:  ## measure judge models against the human labels (needs ANTHROPIC_API_KEY)
	$(PY) scripts/compare_judges.py --models claude-opus-4-8,claude-sonnet-5,claude-haiku-4-5

compare-judges-ref:  ## same, against the 30 independent-model (Opus) reference labels
	$(PY) scripts/compare_judges.py --labels evals/reference_labels.jsonl --verdict-key verdict \
		--models claude-sonnet-5,claude-haiku-4-5

clean:
	rm -rf $(VENV) evals/results/*.json .pytest_cache **/__pycache__
