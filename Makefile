# db_deployer build & install
#
# Usage:
#   make              # build db_deployer.pyz
#   make install      # build + copy to /usr/local/bin/db_deployer (needs sudo)
#   make dev          # editable install into .venv for live-edit development
#   make check-deps   # verify psycopg2 is installable
#   make clean        # remove build artifacts
#   make distclean    # also remove the build venv

PYTHON      ?= python3
VENV        := .venv
VENV_BIN    := $(VENV)/bin
SHIV        := $(VENV_BIN)/shiv
PIP         := $(VENV_BIN)/pip
PYZ         := db_deployer.pyz
PREFIX      ?= /usr/local
BINDIR      := $(PREFIX)/bin
INSTALL_AS  := $(BINDIR)/db_deployer

SRC := pyproject.toml $(shell find src -name '*.py')

# On macOS with Homebrew, psycopg2 needs to find libpq and openssl@3.
# Both are keg-only, so we point the compiler and linker at them explicitly.
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
    HOMEBREW_PREFIX := $(shell brew --prefix 2>/dev/null)
    ifneq ($(HOMEBREW_PREFIX),)
        export LDFLAGS  += -L$(HOMEBREW_PREFIX)/opt/openssl@3/lib -L$(HOMEBREW_PREFIX)/opt/libpq/lib
        export CPPFLAGS += -I$(HOMEBREW_PREFIX)/opt/openssl@3/include -I$(HOMEBREW_PREFIX)/opt/libpq/include
    endif
endif

.PHONY: all build install uninstall clean distclean venv check-deps dev

all: build

build: $(PYZ)

$(PYZ): $(SRC) | $(SHIV) check-deps
	$(SHIV) -c db_deployer -o $(PYZ) --python "/usr/bin/env $(notdir $(PYTHON))" .

$(SHIV): | $(VENV)
	$(PIP) install --upgrade pip shiv

$(VENV):
	$(PYTHON) -m venv $(VENV)

venv: $(VENV)

check-deps: | $(VENV)
	@echo "Checking psycopg2 availability..."
	@$(PIP) install --dry-run psycopg2 >/dev/null 2>&1 \
		&& echo "✓ psycopg2 installable" \
		|| { echo "✗ psycopg2 cannot be installed."; \
		     echo "  Install libpq + pg_config (macOS: brew install libpq openssl@3)."; \
		     echo "  On macos: use Homebrew (eg: brew install libpq;brew link --force libpq)"; \
		     echo "            also make sure openssl is installed (brew install openssl)"; \
		     exit 1; }

dev: | $(VENV) check-deps
	$(PIP) install -e .
	@echo ""
	@echo "Editable install complete. Activate the venv and run db_deployer:"
	@echo "  source $(VENV)/bin/activate"
	@echo "  db_deployer --help"
	@echo ""
	@echo "Or invoke directly without activation:"
	@echo "  $(VENV_BIN)/db_deployer --help"
	@echo "  $(VENV_BIN)/python -m db_deployer --help"

install: $(PYZ)
	install -d $(BINDIR)
	install -m 0755 $(PYZ) $(INSTALL_AS)
	@echo "Installed: $(INSTALL_AS)"

uninstall:
	rm -f $(INSTALL_AS)
	@echo "Removed: $(INSTALL_AS)"

clean:
	rm -f $(PYZ)
	rm -rf build dist *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

distclean: clean
	rm -rf $(VENV)
