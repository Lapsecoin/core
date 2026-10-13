# Compiles the Windows program and the small bridge with Nuitka (MSVC).
# Run by .github/workflows/release.yml; also usable on a Windows machine with
# Python, the requirements, Visual Studio Build Tools and liboqs installed.
#
#   scripts/build_windows.ps1 -Version 1.0.10
#
# Produces  dist\lapsecoin-win\   the program as a folder (packaged by the MSI)
#           dist-bridge\lapsecoin.exe   the stand-in for the old single-file exe
#
# Why Nuitka and not PyInstaller: PyInstaller's bootloader and self-extracting
# layout are what antivirus engines match on. Nuitka turns the code into a
# native program with no such bootloader.

param(
    [Parameter(Mandatory)] [string] $Version,
    [string] $Liboqs = "C:\liboqs\install"
)

$ErrorActionPreference = "Stop"

# Windows resource versions are four numbers.
$parts = $Version.Split(".") + @("0", "0", "0", "0")
$fileVersion = ($parts[0..3] -join ".")
$jobs = [Environment]::ProcessorCount

$identity = @(
    "--company-name=LapseCoin",
    "--product-name=LapseCoin",
    "--file-version=$fileVersion",
    "--product-version=$fileVersion",
    "--windows-icon-from-ico=favicon.ico"
)
$common = @("--assume-yes-for-downloads", "--msvc=latest", "--jobs=$jobs",
            "--nofollow-import-to=pytest", "--nofollow-import-to=unittest")

# Native libraries that Python wheels place beside their extension module and
# that dependency scanning can miss (chiavdf is a single top-level .pyd).
$extra = @()
foreach ($sp in (python -c "import site; print('\n'.join(site.getsitepackages()))")) {
    foreach ($pat in "mpir*.dll", "chiavdf*.dll") {
        foreach ($f in Get-ChildItem (Join-Path $sp $pat) -ErrorAction SilentlyContinue) {
            $extra += "--include-data-files=$($f.FullName)=$($f.Name)"
        }
    }
}

# oqs looks for an install prefix with the library under bin\.
$oqs = Join-Path $Liboqs "bin"
$oqsDll = @("oqs.dll", "liboqs.dll") | ForEach-Object { Join-Path $oqs $_ } |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $oqsDll) { throw "no oqs.dll in $oqs" }

$env:PYTHONPATH = "src"

Remove-Item build-nuitka, dist\lapsecoin-win -Recurse -Force -ErrorAction SilentlyContinue
python -m nuitka --mode=standalone @common @identity `
    "--file-description=LapseCoin node and wallet" `
    --windows-console-mode=disable `
    --enable-plugin=tk-inter `
    --output-dir=build-nuitka --output-filename=lapsecoin `
    --include-data-files=VERSION=VERSION `
    --include-data-files=src/bip39_english.txt=bip39_english.txt `
    --include-data-files=docs/whitepaper.md=docs/whitepaper.md `
    --include-data-files=lapsecoin.svg=lapsecoin.svg `
    --include-data-files=lapsecoin.png=lapsecoin.png `
    --include-data-files=favicon.ico=favicon.ico `
    --include-data-dir=templates_html=templates_html `
    --include-data-dir=vendor=vendor `
    --include-data-files="$oqsDll=_oqs/bin/$(Split-Path $oqsDll -Leaf)" `
    --include-package-data=limits `
    @extra `
    main.py
if ($LASTEXITCODE) { throw "nuitka failed for the program" }
New-Item dist -ItemType Directory -Force | Out-Null
Move-Item build-nuitka\main.dist dist\lapsecoin-win

# The bridge is standard library only, as one file, because old copies of the
# program download a single lapsecoin.exe.
Remove-Item dist-bridge -Recurse -Force -ErrorAction SilentlyContinue
python -m nuitka --mode=onefile @common @identity `
    "--file-description=LapseCoin installer launcher" `
    --windows-console-mode=disable `
    --output-dir=dist-bridge --output-filename=lapsecoin.exe `
    --include-data-files=VERSION=VERSION `
    lapsecoin_bridge.py
if ($LASTEXITCODE) { throw "nuitka failed for the bridge" }
Write-Host "Built dist\lapsecoin-win and dist-bridge\lapsecoin.exe"
