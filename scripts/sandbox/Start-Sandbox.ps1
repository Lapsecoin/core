# Tries the test-signed Store package in Windows Sandbox: a clean, throwaway copy of
# Windows with no profile, Store login or tools, the nearest thing to a tester's fresh
# machine. Run it on your own PC (Windows 11 Pro or better). It downloads what it needs
# (the package, its certificate and sandbox-run.ps1, from the "sandbox-test" prerelease
# that the "Build the Microsoft Store package by hand" workflow keeps up to date):
#
#   .\Start-Sandbox.ps1            (15 minutes; -Minutes 30 for longer)
#
# Windows Sandbox opens and installs the package, starts it the way the Start menu does and
# watches it. It keeps running afterwards, so you can use the app like a tester would.
# Everything it learns is written to the "out" folder next to this script. Nothing is sent to
# GitHub (the sandbox switches crash reports off), and closing the Sandbox deletes the rest.

param([int] $Minutes = 15)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$base = "https://github.com/Lapsecoin/core/releases/download/sandbox-test"
foreach ($f in "lapsecoin-test-signed.msix", "lapsecoin-test.cer", "sandbox-run.ps1") {
    # fresh every time: the package is replaced whenever the workflow runs
    Write-Host "Downloading $f"
    Invoke-WebRequest "$base/$f" -OutFile (Join-Path $here $f) -UseBasicParsing
}
if (-not (Test-Path "$env:windir\System32\WindowsSandbox.exe")) {
    throw ("Windows Sandbox is not installed. As administrator run " +
           "Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All " +
           "and restart the PC. (It needs Windows 11 Pro, Enterprise or Education.)")
}

New-Item (Join-Path $here "out") -ItemType Directory -Force | Out-Null
$folder = [System.Security.SecurityElement]::Escape($here)
$config = Join-Path $here "lapsecoin.wsb"
@"
<Configuration>
  <Networking>Enable</Networking>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$folder</HostFolder>
      <SandboxFolder>C:\Lapse</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell -ExecutionPolicy Bypass -NoExit -File C:\Lapse\sandbox-run.ps1 -Minutes $Minutes</Command>
  </LogonCommand>
</Configuration>
"@ | Set-Content $config -Encoding UTF8

Write-Host "Starting Windows Sandbox. Results will be in $here\out"
Start-Process "$env:windir\System32\WindowsSandbox.exe" -ArgumentList "`"$config`""
