.PHONY: install up down index data ingest ask serve test lint fmt typecheck eval check clean

PY ?= python

install:            ## create venv and install everything
	$(PY) -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,ingestion,observability,eval]"

up:                 ## start OpenSearch + Redis locally
	docker compose up -d opensearch redis

down:
	docker compose down

index:              ## create search pipeline + index
	$(PY) -m insurance_rag init-index

data:               ## download free public docs + generate synthetic claim notes
	$(PY) -m insurance_rag download
	$(PY) -m insurance_rag generate-claims

ingest:             ## parse, redact, chunk, embed, index
	$(PY) -m insurance_rag ingest --redactor regex

ask:                ## make ask Q="How long do I have to file proof of loss?"
	$(PY) -m insurance_rag ask "$(Q)"

serve:
	uvicorn insurance_rag.api.main:create_app --factory --reload --port 8080

test:
	pytest

lint:
	ruff check . && ruff format --check .

fmt:
	ruff check --fix . && ruff format .

typecheck:
	mypy src

eval:               ## run the quality gate against whatever is indexed
	$(PY) -m insurance_rag.evaluation.run --scorer ragas

check: lint typecheck test   ## what CI runs before anything touches AWS

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache build *.egg-info
