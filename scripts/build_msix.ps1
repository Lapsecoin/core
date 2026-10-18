# Builds lapsecoin.msix, the Microsoft Store build, from the folder
# scripts/build_windows.ps1 makes (Nuitka). Windows only; run by
# .github/workflows/release.yml. The package is not signed: the Store signs it.
#
#   scripts/build_msix.ps1 -Source dist\lapsecoin-win -Version 1.1.4 `
#       -Name <identity name> -Publisher <CN=...> -PublisherDisplayName <name> `
#       -DisplayName <reserved app name> -Out lapsecoin.msix
#
# The identity values and the display name (the app name reserved for the
# product) come from Partner Center and must match it exactly. The folder
# holds no installed-by-msi marker, so the program finds out it is packaged
# from Windows itself and keeps its data in %USERPROFILE%\LapseCoin (see
# src/datadir.py).

param(
    [Parameter(Mandatory)] [string] $Source,
    [Parameter(Mandatory)] [string] $Version,
    [Parameter(Mandatory)] [string] $Name,
    [Parameter(Mandatory)] [string] $Publisher,
    [Parameter(Mandatory)] [string] $PublisherDisplayName,
    [Parameter(Mandatory)] [string] $DisplayName,
    [string] $Out = "lapsecoin.msix",
    [string] $Logo = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Not a version: '$Version'" }
if (-not (Test-Path (Join-Path $Source "lapsecoin.exe"))) { throw "No lapsecoin.exe in $Source" }
if (Test-Path (Join-Path $Source "installed-by-msi")) { throw "$Source holds the MSI marker, which must not be in the package" }
if ($Publisher -notmatch '^CN=') { throw "The publisher should look like CN=..., copy it from Partner Center" }

# The Store wants a fourth number of 0.
$msixVersion = "$Version.0"

$makeappx = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\makeappx.exe" |
    Sort-Object { [version]$_.Directory.Parent.Name } -Descending | Select-Object -First 1
if (-not $makeappx) { throw "makeappx.exe not found, the Windows SDK is missing" }

$work = Join-Path $PWD "msix-work"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item $Source $work -Recurse

function Esc($s) { [System.Security.SecurityElement]::Escape($s) }
$manifest = Get-Content (Join-Path $PSScriptRoot "AppxManifest.template.xml") -Raw
$manifest = $manifest.Replace("{{NAME}}", (Esc $Name)).Replace("{{PUBLISHER}}", (Esc $Publisher)).
    Replace("{{PUBLISHER_DISPLAY_NAME}}", (Esc $PublisherDisplayName)).Replace("{{VERSION}}", $msixVersion).
    Replace("{{DISPLAY_NAME}}", (Esc $DisplayName))
[System.IO.File]::WriteAllText((Join-Path $work "AppxManifest.xml"), $manifest, (New-Object System.Text.UTF8Encoding($false)))

# Tile and store logos, scaled from the app's own logo (the copy bundled in
# the program folder unless -Logo says otherwise), so no image files are kept
# in the repository for them.
if (-not $Logo) { $Logo = Join-Path $Source "lapsecoin.png" }
if (-not (Test-Path $Logo)) { throw "Logo not found: $Logo" }
$assets = Join-Path $work "Assets"
New-Item $assets -ItemType Directory | Out-Null
$src = [System.Drawing.Image]::FromFile((Resolve-Path $Logo))
foreach ($a in @(@("Square44x44Logo", 44), @("Square150x150Logo", 150), @("StoreLogo", 50))) {
    $bmp = New-Object System.Drawing.Bitmap $a[1], $a[1]
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.DrawImage($src, 0, 0, $a[1], $a[1])
    $g.Dispose()
    $bmp.Save((Join-Path $assets "$($a[0]).png"), [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
}
$src.Dispose()

Remove-Item $Out -ErrorAction SilentlyContinue
& $makeappx.FullName pack /d $work /p $Out /o
if ($LASTEXITCODE) { throw "makeappx failed" }
Write-Host "Built $Out ($msixVersion)"
