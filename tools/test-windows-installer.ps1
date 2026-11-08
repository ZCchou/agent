[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [string]$RepositoryRoot,
    [switch]$FullOffline,
    [string]$SmokeParent,
    [string]$Python = 'python'
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
foreach ($hive in @('HKCU:', 'HKLM:')) {
    $key = $hive + '\Software\Microsoft\Windows\CurrentVersion\Uninstall\agent'
    if (Test-Path -LiteralPath $key) {
        throw 'An existing agent installation is registered. Isolated smoke testing must preserve it. Use an isolated Windows account or VM.'
    }
}

if (-not $SmokeParent) { $SmokeParent = Join-Path $RepositoryRoot '.artifacts/installer-smoke' }
New-Item -ItemType Directory -Path $SmokeParent -Force | Out-Null
$tempRoot = [System.IO.Path]::GetFullPath($SmokeParent).TrimEnd('\')
$installerTemp = Join-Path $tempRoot ('installer-temp-' + $PID)
New-Item -ItemType Directory -Path $installerTemp -Force | Out-Null
function Invoke-SmokeInstaller([string]$Path, [string[]]$Arguments) {
    $previousTemp = $env:TEMP
    $previousTmp = $env:TMP
    try {
        $env:TEMP = $installerTemp
        $env:TMP = $installerTemp
        return Start-Process -FilePath $Path -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    } finally {
        $env:TEMP = $previousTemp
        $env:TMP = $previousTmp
    }
}
$installRoot = Join-Path $tempRoot "agent-installer-smoke-$PID"
$resolvedInstallRoot = [System.IO.Path]::GetFullPath($installRoot)
if (-not $resolvedInstallRoot.StartsWith("$tempRoot\", [StringComparison]::OrdinalIgnoreCase) -or
    [System.IO.Path]::GetFileName($resolvedInstallRoot) -notlike "agent-installer-smoke-*") {
    throw "Refusing to use an unsafe installer smoke-test directory: $resolvedInstallRoot"
}
if (Test-Path -LiteralPath $resolvedInstallRoot) {
    throw "Installer smoke-test directory already exists: $resolvedInstallRoot"
}
New-Item -ItemType Directory -Path $resolvedInstallRoot | Out-Null
Set-Content -LiteralPath (Join-Path $resolvedInstallRoot '.agent-installer-smoke-owned') -Value 'agent-installer-smoke-v1' -Encoding ASCII

Write-Host "[SMOKE] Installing into $resolvedInstallRoot"
$installProcess = Invoke-SmokeInstaller $InstallerPath @('/S', '/NS', "/D=$resolvedInstallRoot")
if ($installProcess.ExitCode -notin @(0, 3010)) {
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
if ($FullOffline) {
    & $Python (Join-Path $PSScriptRoot 'verify_installed_offline.py') $resolvedInstallRoot --data-directory (Join-Path $tempRoot ('parser-data-' + $PID))
    if ($LASTEXITCODE -ne 0) { throw 'Installed offline runtime/parser verification failed' }
}

Write-Host "[SMOKE] Uninstalling isolated package"
$uninstallProcess = Invoke-SmokeInstaller $uninstaller @('/S')
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
    $resolved = [IO.Path]::GetFullPath($resolvedInstallRoot)
    $marker = Join-Path $resolved '.agent-installer-smoke-owned'
    if (-not $resolved.StartsWith($tempRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
        (((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) -or
        -not (Test-Path -LiteralPath $marker) -or
        (Get-Content -LiteralPath $marker -Raw).Trim() -ne 'agent-installer-smoke-v1') {
        throw 'Refusing cleanup of an unowned or redirected smoke-test directory'
    }
    $remaining = @(Get-ChildItem -LiteralPath $resolved -Force)
    if ($remaining.Count -ne 1 -or $remaining[0].Name -ne '.agent-installer-smoke-owned') {
        throw 'Unexpected files remain after uninstall. Preserve the smoke-test directory for inspection.'
    }
    Remove-Item -LiteralPath $marker
    Remove-Item -LiteralPath $resolved
}
Write-Host "[OK] NSIS install, installed Sidecar handshake, and uninstall completed successfully."
