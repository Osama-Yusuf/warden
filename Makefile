# warden monorepo. Everyday commands, basically the package.json scripts of this repo.
#
#   make setup            install everything into .venv (uv workspace)
#   make dev              web dev server with auto-reload
#   make dev-desktop      desktop app with auto-restart on code/UI change
#   make web              run the web UI server
#   make cli              run the CLI            (make cli ARGS="docdb list-users")
#   make desktop          run the desktop app from source
#   make build            wheels for all packages → dist/
#   make build-desktop    standalone desktop bundle for THIS OS → dist/warden.app
#   make dmg              macOS installer image → dist/warden.dmg
#   make install-desktop  copy dist/warden.app into /Applications
#   make env              create .env from .env.example
#
# Cross-OS note: PyInstaller does not cross-compile, so build-desktop produces a
# bundle for the OS it runs on. CI (.github/workflows/build.yml) builds
# macOS arm64/x86_64 + Windows on tag push.

UV := $(shell command -v uv 2>/dev/null)
VENV := .venv
PY := $(VENV)/bin/python
ARGS ?=

.PHONY: setup dev dev-desktop web cli desktop build build-desktop dmg install-desktop env clean check-uv

check-uv:
ifndef UV
	$(error uv is required. install with: brew install uv)
endif

setup: check-uv
	uv sync
	@echo "\nDone. Entry points: $(VENV)/bin/warden, warden-web, warden-desktop"

$(PY):
	@$(MAKE) setup

dev: $(PY)
	$(PY) -u tools/dev.py --target web $(ARGS)

dev-desktop: $(PY)
	$(PY) -u tools/dev.py --target desktop $(ARGS)

web: $(PY)
	$(VENV)/bin/warden-web $(ARGS)

cli: $(PY)
	@$(VENV)/bin/warden $(ARGS)

desktop: $(PY)
	$(VENV)/bin/warden-desktop

build: check-uv
	uv build --all-packages --out-dir dist
	@ls -1 dist/*.whl

build-desktop: $(PY)
	uv pip install pyinstaller
	$(VENV)/bin/pyinstaller --noconfirm --clean --windowed --name warden \
		--icon packages/desktop/assets/warden.icns \
		--add-data "packages/web/src/warden_web/static:warden_web/static" \
		packages/desktop/src/warden_desktop/main.py
	@rm -f warden.spec
	@echo "\nBundle: dist/warden.app (macOS) / dist/warden/ (other OS)"

dmg:
	@test -d dist/warden.app || $(MAKE) build-desktop
	rm -f dist/warden.dmg
	@STAGE=$$(mktemp -d /tmp/warden-dmg.XXXXXX); \
	cp -R dist/warden.app $$STAGE/; \
	ln -sf /Applications $$STAGE/Applications; \
	for i in 1 2 3 4 5; do \
		hdiutil create -volname warden -srcfolder $$STAGE -ov -format UDZO dist/warden.dmg && break; \
		echo "hdiutil busy - retrying ($$i)"; sleep 3; \
	done; \
	rm -rf $$STAGE
	@test -f dist/warden.dmg
	@echo "\nInstaller: dist/warden.dmg (open it, drag warden into Applications)"

install-desktop:
	@test -d dist/warden.app || $(MAKE) build-desktop
	rm -rf /Applications/warden.app
	cp -R dist/warden.app /Applications/
	@echo "Installed: /Applications/warden.app"

env:
	@test -f .env || (cp .env.example .env && echo "Created .env, fill it in")
	@test ! -f .env || echo ".env already exists"

clean:
	rm -rf $(VENV) dist build *.spec
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
