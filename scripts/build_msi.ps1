# Builds lapsecoin.msi from the PyInstaller folder with the WiX Toolset 3
# (heat, candle, light). Windows only; run by .github/workflows/release.yml.
#
#   scripts/build_msi.ps1 -Source dist\lapsecoin-win -Version 1.0.10 -Out lapsecoin.msi

param(
    [Parameter(Mandatory)] [string] $Source,
    [Parameter(Mandatory)] [string] $Version,
    [string] $Out = "lapsecoin.msi",
    [string] $Icon = "favicon.ico"
)

$ErrorActionPreference = "Stop"
$bin = (Get-Item "C:\Program Files (x86)\WiX Toolset v3*\bin" | Select-Object -First 1).FullName

# Windows Installer versions are three numbers, each within its own limit.
$parts = $Version.Split(".") + @("0", "0", "0")
$msiVersion = "$($parts[0]).$($parts[1]).$($parts[2])"

$work = Join-Path $PWD "msi-work"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item $work -ItemType Directory | Out-Null

# The marker the app looks for to know it was installed by this package.
$marker = Join-Path $work "installed-by-msi"
Set-Content -Path $marker -Value "msi" -NoNewline -Encoding ascii

& "$bin\heat.exe" dir $Source -cg AppFiles -dr INSTALLFOLDER -srd -sreg -gg -sfrag `
    -var var.SourceDir -out "$work\AppFiles.wxs"
if ($LASTEXITCODE) { throw "heat failed" }

& "$bin\candle.exe" -nologo -arch x64 `
    "-dVersion=$msiVersion" "-dSourceDir=$Source" "-dIcon=$Icon" "-dMarker=$marker" `
    -out "$work\" installer\lapsecoin.wxs "$work\AppFiles.wxs"
if ($LASTEXITCODE) { throw "candle failed" }

# ICE38/64/91 complain that per-user components lack registry key paths, which
# is how every harvested file set looks; none of them affects an install.
& "$bin\light.exe" -nologo -sval -sice:ICE38 -sice:ICE64 -sice:ICE91 `
    -out $Out "$work\lapsecoin.wixobj" "$work\AppFiles.wixobj"
if ($LASTEXITCODE) { throw "light failed" }

Write-Host "Built $Out ($msiVersion)"
