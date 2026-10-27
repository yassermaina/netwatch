# Installs the data files (desktop entry, metainfo, icons) that pip/pyproject cannot place.
# The Python package itself is installed by the distribution's Python build tooling
# (%pyproject_install on Fedora, pybuild on Debian) or by `make install` below.
PREFIX ?= /usr
DESTDIR ?=
DATADIR = $(DESTDIR)$(PREFIX)/share
PYTHON ?= python3
APP_ID = io.github.netwatch.NetWatch

.PHONY: all test install install-data install-python run deb rpm update clean

all:
	@echo "Targets: test, run, install, install-data, deb, rpm, update"

test:
	@$(PYTHON) -c "import pytest" 2>/dev/null || { \
		echo "pytest is not installed for $(PYTHON)."; \
		echo "  Fedora:        sudo dnf install python3-pytest"; \
		echo "  Debian/Ubuntu: sudo apt install python3-pytest"; \
		echo "  (or use a venv created with --system-site-packages so GTK bindings stay visible)"; \
		exit 1; }
	@$(PYTHON) -c "import gi" 2>/dev/null || echo "note: PyGObject not found for $(PYTHON); GUI tests will be skipped."
	$(PYTHON) -m pytest -q

run:
	./bin/netwatch

install-data:
	install -Dm755 packaging/netwatch-update $(DESTDIR)$(PREFIX)/bin/netwatch-update
	install -Dm644 data/$(APP_ID).desktop $(DATADIR)/applications/$(APP_ID).desktop
	install -Dm644 data/$(APP_ID).metainfo.xml $(DATADIR)/metainfo/$(APP_ID).metainfo.xml
	install -Dm644 data/icons/hicolor/scalable/apps/$(APP_ID).svg $(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	for f in data/icons/hicolor/symbolic/apps/*.svg; do \
		install -Dm644 $$f $(DATADIR)/icons/hicolor/symbolic/apps/$$(basename $$f); done

install-python:
	$(PYTHON) -m pip install --no-deps --no-build-isolation --root "$(or $(DESTDIR),/)" --prefix $(PREFIX) .

install: install-python install-data

deb:
	./packaging/build-deb.sh

rpm:
	./packaging/build-rpm.sh

update:
	./update.sh

clean:
	rm -rf build dist *.egg-info .pytest_cache
	find . -name __pycache__ -prune -exec rm -rf {} +
