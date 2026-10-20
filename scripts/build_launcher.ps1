# Compiles launcher/Launch.cs into lapsecoin-launch.exe, the program the shortcuts and
# the Store package start (see launcher/Launch.cs). Windows only.
#
#   scripts/build_launcher.ps1 -OutDir dist\lapsecoin-win
#
# It uses the C# 5 compiler that ships with .NET Framework 4, which is part of
# Windows 10 and 11, so the launcher needs nothing installed to run.

param(
    [Parameter(Mandatory)] [string] $OutDir,
    [string] $Icon = "favicon.ico"
)

$ErrorActionPreference = "Stop"
$csc = Join-Path $env:windir "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { throw "csc.exe not found at $csc" }

$source = Join-Path $PSScriptRoot "..\launcher\Launch.cs"
$out = Join-Path $OutDir "lapsecoin-launch.exe"
New-Item $OutDir -ItemType Directory -Force | Out-Null

[string[]]$iconArgs = if (Test-Path $Icon) { "/win32icon:$Icon" } else { @() }
& $csc /nologo /target:winexe /platform:x64 /optimize+ /warnaserror+ "/out:$out" @iconArgs `
    /reference:System.dll /reference:System.Core.dll $source
if ($LASTEXITCODE) { throw "csc failed" }
Write-Host "Built $out ($((Get-Item $out).Length) bytes)"
