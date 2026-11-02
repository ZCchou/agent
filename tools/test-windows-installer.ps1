[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [string]$RepositoryRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "The installer smoke test must run on Windows."
}
if (-not $RepositoryRoot) {
    $RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path

$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
$installRoot = Join-Path $tempRoot "agent-installer-smoke-$PID"
$resolvedInstallRoot = [System.IO.Path]::GetFullPath($installRoot)
if (-not $resolvedInstallRoot.StartsWith("$tempRoot\", [StringComparison]::OrdinalIgnoreCase) -or
    [System.IO.Path]::GetFileName($resolvedInstallRoot) -notlike "agent-installer-smoke-*") {
    throw "Refusing to use an unsafe installer smoke-test directory: $resolvedInstallRoot"
}
if (Test-Path -LiteralPath $resolvedInstallRoot) {
    throw "Installer smoke-test directory already exists: $resolvedInstallRoot"
}

Write-Host "[SMOKE] Installing into $resolvedInstallRoot"
$installProcess = Start-Process -FilePath $InstallerPath -ArgumentList @("/S", "/D=$resolvedInstallRoot") -Wait -PassThru -WindowStyle Hidden
if ($installProcess.ExitCode -ne 0) {
    throw "NSIS installer exited with code $($installProcess.ExitCode)."
}

$desktopExe = Join-Path $resolvedInstallRoot "agent.exe"
if (-not (Test-Path -LiteralPath $desktopExe -PathType Leaf)) {
    $desktopExe = Join-Path $resolvedInstallRoot "agent-desktop.exe"
}
$sidecarExe = Join-Path $resolvedInstallRoot "agent-core.exe"
$uninstaller = Join-Path $resolvedInstallRoot "uninstall.exe"
foreach ($required in @($desktopExe, $sidecarExe, $uninstaller)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Installed package is missing: $required"
    }
}

& (Join-Path $PSScriptRoot "verify-python-sidecar.ps1") -RepositoryRoot $RepositoryRoot -SidecarPath $sidecarExe

Write-Host "[SMOKE] Uninstalling isolated package"
$uninstallProcess = Start-Process -FilePath $uninstaller -ArgumentList @("/S") -Wait -PassThru -WindowStyle Hidden
if ($uninstallProcess.ExitCode -ne 0) {
    throw "NSIS uninstaller exited with code $($uninstallProcess.ExitCode)."
}

$deadline = [DateTime]::UtcNow.AddSeconds(20)
while ((Test-Path -LiteralPath $desktopExe -PathType Leaf) -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 250
}
if (Test-Path -LiteralPath $desktopExe -PathType Leaf) {
    throw "The uninstaller did not remove the installed desktop executable."
}

if ([System.IO.Directory]::Exists($resolvedInstallRoot)) {
    [System.IO.Directory]::Delete($resolvedInstallRoot, $true)
}
Write-Host "[OK] NSIS install, installed Sidecar handshake, and uninstall completed successfully."
