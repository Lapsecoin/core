# LapseCoin build targets
#
# make linux   -- regenerate icons, build onedir via PyInstaller,
#                 then wrap into dist/lapsecoin (AppImage, no extension)
#                 requires python3-tk (for the desktop GUI; falls back to
#                 console at runtime if missing, but PyInstaller needs it
#                 present at build time to bundle it in the first place)
# make windows -- build dist/lapsecoin-win/ with Nuitka (packaged by the MSI); needs Visual Studio Build Tools
# make icons   -- regenerate favicon.ico and lapsecoin.png from lapsecoin.svg
#                 (Linux only; requires libcairo2-dev + pip install cairosvg Pillow)
#                 Run before make linux or make windows. Output is git-ignored.
# make clean   -- remove build artifacts
#
# PyInstaller must build on the target platform. Cross-compilation is not supported.
# VDF_ITERATIONS in params.py must be calibrated on the target hardware before
# building for mainnet (see vdf.py docstring).
# appimagetool must be on PATH for make linux:
#   wget -q https://github.com/AppImage/AppImageKit/releases/latest/download/appimagetool-x86_64.AppImage
#   chmod +x appimagetool-x86_64.AppImage && sudo mv appimagetool-x86_64.AppImage /usr/local/bin/appimagetool
#
# Requirements: pip install pyinstaller cairosvg Pillow

SPEC    = lapsecoin.spec
DIST    = dist
BUILD   = build
APPDIR  = AppDir

.PHONY: linux windows icons native test clean

# The HD wallet's seeded Falcon key generation (needs a C compiler). The Linux
# build bundles it; running from a checkout builds it the first time it is
# needed if a compiler is there, or run this once.
native:
	$(MAKE) -C native/falcon_seeded

linux: icons native
	pyinstaller --clean --noconfirm $(SPEC)
	@which appimagetool > /dev/null 2>&1 || (echo "appimagetool not found. See Makefile header." && exit 1)
	rm -rf $(APPDIR)
	mkdir -p $(APPDIR)/usr/bin
	cp -r $(DIST)/lapsecoin-onedir/* $(APPDIR)/usr/bin/
	cp lapsecoin.png $(APPDIR)/lapsecoin.png
	printf '[Desktop Entry]\nName=LapseCoin\nExec=lapsecoin\nIcon=lapsecoin\nType=Application\nCategories=Network;Finance;\n' > $(APPDIR)/lapsecoin.desktop
	printf '#!/bin/sh\nexec "$$APPDIR/usr/bin/lapsecoin" "$$@"\n' > $(APPDIR)/AppRun
	chmod +x $(APPDIR)/AppRun
	ARCH=x86_64 appimagetool --appimage-extract-and-run $(APPDIR) $(DIST)/lapsecoin
	@echo "Built: $(DIST)/lapsecoin"

windows:
	powershell -Command "& scripts/build_windows.ps1 -Version (Get-Content VERSION).Trim()"
	@echo "Built: $(DIST)/lapsecoin-win/lapsecoin.exe"

icons:
	python3 -c "\
import cairosvg, base64, io; \
from PIL import Image; \
imgs=[Image.open(io.BytesIO(cairosvg.svg2png(url='lapsecoin.svg',output_width=s,output_height=s))).convert('RGBA') for s in [16,32,48]]; \
imgs[0].save('favicon.ico',format='ICO',sizes=[(16,16),(32,32),(48,48)],append_images=imgs[1:]); \
open('lapsecoin.png','wb').write(cairosvg.svg2png(url='lapsecoin.svg',output_width=512,output_height=512)); \
print('icons regenerated')"

test:
	python3 -m pytest tests/ -q

clean:
	$(MAKE) -C native/falcon_seeded clean
	rm -rf $(DIST) $(BUILD) $(APPDIR) __pycache__ lapsecoin_chain.db lapsecoin_chain.db-shm lapsecoin_chain.db-wal
	find . -name "*.pyc" -delete
