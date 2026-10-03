UV ?= uv
SITE ?= https://usegitai.com/
OUTPUT ?= output

.PHONY: help sync lint format typecheck test check clone serve down clean

help:
	@echo "sync      install dependencies"
	@echo "lint      ruff check"
	@echo "format    ruff format"
	@echo "typecheck ty"
	@echo "test      pytest"
	@echo "check     format + lint + typecheck + test"
	@echo "clone     clone $(SITE) into ./$(OUTPUT)"
	@echo "serve     serve ./$(OUTPUT) with nginx on :8080"

sync:
	$(UV) sync

lint:
	$(UV) run ruff check .

format:
	$(UV) run ruff format .

typecheck:
	$(UV) run ty check

test:
	$(UV) run pytest

check: format lint typecheck test

clone:
	$(UV) run sitecloner "$(SITE)" -o "$(OUTPUT)"

serve:
	docker compose up nginx

down:
	docker compose down

clean:
	rm -rf "$(OUTPUT)"