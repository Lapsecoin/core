# Installs cmake, git, and a C++ compiler -- the only real prerequisite
# for running from source. liboqs itself needs none of this documented
# separately: liboqs-python builds and installs it automatically on first
# `import oqs` using exactly these three tools, so once they're on PATH
# there's nothing else to set up.
#
# Unlike the Linux/macOS script, this can't be fully verified against a
# real Windows machine from this repo's own tooling -- test it before
# relying on it. The C++ compiler step in particular (Visual Studio Build
# Tools) is a multi-GB install with no single-line equivalent to apt/brew;
# this uses winget's override flags to select the C++ workload silently,
# matching what .github/workflows/release.yml's own Windows job installs,
# but that CI job runs on a runner image that already ships Visual Studio,
# so it never actually exercises this particular install path itself.

$ErrorActionPreference = "Stop"

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

$haveCmake = Test-Command "cmake"
$haveGit = Test-Command "git"
$haveCompiler = Test-Command "cl"

if ($haveCmake -and $haveGit -and $haveCompiler) {
    Write-Host "cmake, git, and a C++ compiler are already on PATH. Nothing to do."
    exit 0
}

if (-not (Test-Command "winget")) {
    Write-Error "winget not found (needs Windows 10 1809+ or Windows 11 with App Installer). Install cmake, git, and Visual Studio Build Tools (C++ workload) yourself, then re-run."
    exit 1
}

if (-not $haveCmake) {
    winget install --id Kitware.CMake -e --silent
}
if (-not $haveGit) {
    winget install --id Git.Git -e --silent
}
if (-not $haveCompiler) {
    Write-Host "Installing Visual Studio Build Tools (C++ workload) -- this is a large download and may take a while."
    winget install --id Microsoft.VisualStudio.2022.BuildTools -e --silent --override "--quiet --add Microsoft.VisualStudio.Workload.VCTools"
}

Write-Host "Done. Open a new terminal so PATH updates take effect, then liboqs will build itself automatically the first time you run lapsecoin."
