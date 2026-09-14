.PHONY: install test lint demo serve clean verify

PY ?= python

install:
	$(PY) -m pip install -e ".[all]"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check reposplit tests examples

demo:
	$(PY) -m reposplit run examples/shop_monolith --out out --provider mock --yes

serve:
	$(PY) -m reposplit serve --port 8765

verify:
	$(PY) -m reposplit verify out/reports/migration_passport.json

clean:
	rm -rf out .reposplit .pytest_cache .ruff_cache
