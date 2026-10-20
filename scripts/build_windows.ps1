# Compiles the Windows program with Nuitka (MSVC).
# Run by .github/workflows/release.yml; also usable on a Windows machine with
# Python, the requirements, Visual Studio Build Tools and liboqs installed.
#
#   scripts/build_windows.ps1 -Version 1.0.10
#
# Produces  dist\lapsecoin-win\   the program as a folder (packaged by the MSI)
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
    "--copyright=MIT License. https://github.com/Lapsecoin/core",
    "--file-version=$fileVersion",
    "--product-version=$fileVersion",
    "--windows-icon-from-ico=favicon.ico"
)
# --lto=no: no link-time optimisation pass over ~900 files, which the
# program's own Python does not need (the heavy maths is in native libraries).
# pygments is only an optional import of other packages; nothing here uses it.
$common = @("--assume-yes-for-downloads", "--msvc=latest", "--jobs=$jobs", "--lto=no",
            "--nofollow-import-to=pytest", "--nofollow-import-to=unittest",
            "--nofollow-import-to=pygments")

# oqs looks for an install prefix with the library under bin\.
$oqs = Join-Path $Liboqs "bin"
$oqsDll = @("oqs.dll", "liboqs.dll") | ForEach-Object { Join-Path $oqs $_ } |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $oqsDll) { throw "no oqs.dll in $oqs" }
# The HD wallet's seeded Falcon key generation, built beside it (release.yml).
$fsDll = Join-Path $oqs "falcon_seeded.dll"
if (-not (Test-Path $fsDll)) { throw "no falcon_seeded.dll in $oqs" }

$env:PYTHONPATH = "src"

# The Store page of this app, for the update banner of a Store-installed copy.
# Empty without the MSIX_STORE_ID repo variable (passed in as an environment
# variable by release.yml).
$storeUrl = if ($env:MSIX_STORE_ID) { "https://apps.microsoft.com/detail/" + $env:MSIX_STORE_ID.Trim().ToLower() } else { "" }
Set-Content -Path STORE_URL -Value $storeUrl -NoNewline -Encoding ascii

# Where a critical crash report is filed (src/crashlog.py): the repository (for
# example Lapsecoin/core) and a fine-grained token that can only write issues in it, from
# the repository the build runs in (github.repository) and the CRASH_REPORT_TOKEN secret. The token ends
# up readable inside the program, so it must never be able to do more than that.
# Without both, nothing is sent.
$crashRepo = if ($env:CRASH_REPORT_REPO) { $env:CRASH_REPORT_REPO.Trim() } else { "" }
$crashToken = if ($env:CRASH_REPORT_TOKEN) { $env:CRASH_REPORT_TOKEN.Trim() } else { "" }
$crashReport = if ($crashRepo -and $crashToken) { @{ repo = $crashRepo; token = $crashToken } | ConvertTo-Json -Compress } else { "" }
Set-Content -Path CRASH_REPORT -Value $crashReport -NoNewline -Encoding ascii

Remove-Item build-nuitka, dist\lapsecoin-win -Recurse -Force -ErrorAction SilentlyContinue
python -m nuitka --mode=standalone @common @identity `
    "--file-description=LapseCoin node and wallet" `
    --windows-console-mode=disable `
    --enable-plugin=tk-inter `
    --output-dir=build-nuitka --output-filename=lapsecoin `
    --include-data-files=VERSION=VERSION `
    --include-data-files=STORE_URL=STORE_URL `
    --include-data-files=CRASH_REPORT=CRASH_REPORT `
    --include-data-files=src/bip39_english.txt=bip39_english.txt `
    --include-data-files=docs/whitepaper.md=docs/whitepaper.md `
    --include-data-files=lapsecoin.svg=lapsecoin.svg `
    --include-data-files=lapsecoin.png=lapsecoin.png `
    --include-data-files=favicon.ico=favicon.ico `
    --include-data-dir=templates_html=templates_html `
    --include-data-dir=vendor=vendor `
    --include-data-files="$oqsDll=_oqs/bin/$(Split-Path $oqsDll -Leaf)" `
    --include-data-files="$fsDll=_oqs/bin/falcon_seeded.dll" `
    --include-package-data=limits `
    main.py
if ($LASTEXITCODE) { throw "nuitka failed for the program" }
New-Item dist -ItemType Directory -Force | Out-Null
Move-Item build-nuitka\main.dist dist\lapsecoin-win

# Native libraries that Python wheels place beside their extension module.
# Nuitka finds the ones the extension links to (mpir.dll), but not the
# CPU-specific variants that mpir loads by name at run time, so any of them
# still missing are copied in here, after the build. Putting them on the
# command line instead makes Nuitka stop on the ones it already found.
foreach ($sp in (python -c "import site; print('\n'.join(site.getsitepackages()))")) {
    foreach ($pat in "mpir*.dll", "chiavdf*.dll") {
        foreach ($f in Get-ChildItem (Join-Path $sp $pat) -ErrorAction SilentlyContinue) {
            $dest = Join-Path dist\lapsecoin-win $f.Name
            if (-not (Test-Path $dest)) {
                Copy-Item $f.FullName $dest
                Write-Host "added $($f.Name)"
            }
        }
    }
}

Write-Host "Built dist\lapsecoin-win"
