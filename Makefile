.PHONY: setup check check-all lint format test test-explorer test-swarm test-silent-storm test-tools runtime-check browser-setup browser-check locks build package-check identity-sync identity-check clean
.NOTPARALLEL: check check-all test

UV = uv
RUN = $(UV) run --locked --all-packages --all-extras --group dev
MEMBER_RUN = run --locked --all-packages --all-extras --group dev
BROWSER_RUN = $(UV) --directory evidence-explorer run --locked --all-packages --all-extras --group dev --group browser
BROWSER_INSTALL_FLAGS ?=
CODE_PATHS = evidence-explorer/src evidence-explorer/tests \
	swarm-detector/src swarm-detector/tests swarm-detector/scripts swarm-detector/gunicorn.conf.py \
	operation-silent-storm/src operation-silent-storm/tests tools

setup:
	$(UV) sync --locked --all-packages --all-extras --group dev

check: locks identity-check lint test test-tools runtime-check build package-check

check-all: check browser-check

identity-sync:
	$(RUN) python tools/sync_traced_identity.py

identity-check:
	$(RUN) python tools/sync_traced_identity.py --check

lint:
	$(RUN) ruff check $(CODE_PATHS)
	$(RUN) ruff format --check $(CODE_PATHS)

test: test-explorer test-swarm test-silent-storm

test-explorer:
	$(UV) --directory evidence-explorer $(MEMBER_RUN) pytest

test-swarm:
	$(UV) --directory swarm-detector $(MEMBER_RUN) pytest

test-silent-storm:
	$(UV) --directory operation-silent-storm $(MEMBER_RUN) pytest

test-tools:
	$(RUN) python -m unittest discover -s tools/tests

runtime-check:
	$(RUN) python tools/check_runtime.py --report dist/runtime-check.json

browser-setup:
	$(UV) --directory evidence-explorer sync --locked --all-packages --all-extras --group dev --group browser
	$(BROWSER_RUN) python -m playwright install $(BROWSER_INSTALL_FLAGS) chromium

browser-check:
	$(BROWSER_RUN) python ../tools/check_browser.py --report ../dist/browser-check.json

locks:
	$(UV) lock --check

format:
	$(RUN) ruff check --select I --fix $(CODE_PATHS)
	$(RUN) ruff format $(CODE_PATHS)

build: locks
	$(UV) build --all-packages --out-dir dist

package-check:
	$(RUN) python tools/check_distributions.py --install

clean:
	rm -rf dist build
