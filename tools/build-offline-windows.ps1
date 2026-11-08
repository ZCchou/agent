<# Unsigned LOCAL VERIFICATION build. Public releases retain release-windows.ps1 gates. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$CoreExecutable,
    [Parameter(Mandatory)][string]$RagRuntimeDirectory,
    [Parameter(Mandatory)][string]$LlmRuntimeDirectory,
    [Parameter(Mandatory)][string]$PrerequisiteDirectory,
    [Parameter(Mandatory)][string]$OutputDirectory,
    [string]$Python = 'python',
    [switch]$AllowDirty,
    [switch]$SkipTests,
    [switch]$PrepareOnly,
    [switch]$ResumePrepared,
    [switch]$InstallerSmokeTest
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$desktop = Join-Path $repository 'apps/agent-desktop'
$tauri = Join-Path $desktop 'src-tauri'
$output = [IO.Path]::GetFullPath($OutputDirectory)
if ($env:OS -ne 'Windows_NT') { throw 'Windows x64 build host required' }
if (-not $AllowDirty -or -not $SkipTests) {
    throw 'This isolated unsigned LOCAL VERIFICATION path requires explicit -AllowDirty -SkipTests. Official builds use release-windows.ps1 and its clean/test/signing gates.'
}
function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit $LASTEXITCODE" }
}
function Update-OfflineApplicationManifest([string]$TauriRoot, [string]$CoreSha256, [string]$Destination) {
    $applicationFiles = [ordered]@{}
    foreach ($name in @('agent-desktop.exe', 'agent-core.exe')) {
        $path = Join-Path $TauriRoot ('target/release/' + $name)
        $applicationFiles[$name] = [ordered]@{ bytes = (Get-Item -LiteralPath $path).Length
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    if ($applicationFiles['agent-core.exe'].sha256 -ne $CoreSha256) { throw 'Built sidecar differs from the frozen Core' }
    [ordered]@{ schema_version = 1; files = $applicationFiles } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Destination -Encoding UTF8
}
$initialCommit = (& git -C $repository rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot capture source revision' }
$version = (Get-Content -LiteralPath (Join-Path $desktop 'package.json') -Raw | ConvertFrom-Json).version
$releaseId = 'agent-' + $version + '-local-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
$config = Join-Path $output 'build/tauri-offline.json'
$preparedPath = Join-Path $output 'build/prepared.json'
$payload = Join-Path $output 'payload'
$applicationManifest = Join-Path $output 'application-manifest.json'
if (-not $ResumePrepared) {
    if (Test-Path -LiteralPath $output) { throw 'Use a new offline output directory' }
    New-Item -ItemType Directory -Path (Join-Path $output 'build') -Force | Out-Null
    $runtimeLock = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'offline-runtime.lock.json') -Raw | ConvertFrom-Json
    foreach ($name in @('vc_redist.x64.exe', 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe')) {
        $path = Join-Path $PrerequisiteDirectory $name
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
            throw "Invalid Microsoft signature for $name"
        }
        if ((Get-Item -LiteralPath $path).VersionInfo.FileVersion -ne $runtimeLock.prerequisites.$name.file_version) {
            throw "Microsoft prerequisite file version differs from the fixed lock: $name"
        }
    }
    Invoke-Checked $Python @((Join-Path $PSScriptRoot 'offline_delivery.py'), 'prepare-payload', '--llm', $LlmRuntimeDirectory,
        '--rag', $RagRuntimeDirectory, '--prereqs', $PrerequisiteDirectory, '--destination', $payload, '--release-id', $releaseId)
    $manifestPath = Join-Path $payload 'payload-manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $total = [long]0
    $largest = [long]0
    foreach ($property in $manifest.files.PSObject.Properties) {
        $total += [long]$property.Value.bytes
        $largest = [Math]::Max($largest, [long]$property.Value.bytes)
    }
    # A large total warns about capacity. Attempt the real embedded installer
    # unless a single source file already exceeds NSIS's representable bound.
    $external = $largest -ge (2GB - 64MB)
    $hook = Join-Path $output 'build/offline-hooks.nsh'
    @{ schema_version = 1; files = @{
        'agent-desktop.exe' = @{ bytes = 1; sha256 = ('0' * 64) }
        'agent-core.exe' = @{ bytes = (Get-Item -LiteralPath $CoreExecutable).Length
            sha256 = (Get-FileHash -LiteralPath $CoreExecutable -Algorithm SHA256).Hash.ToLowerInvariant() }
    }} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $applicationManifest -Encoding UTF8
    $hookArgs = @((Join-Path $PSScriptRoot 'offline_delivery.py'), 'installer-hook', '--manifest', $manifestPath,
        '--destination', $hook, '--application-manifest', $applicationManifest)
    if (-not $external) { $hookArgs += '--embedded' }
    Invoke-Checked $Python $hookArgs
    $template = Join-Path $output 'build/offline-installer.nsi'
    $templateRecord = & $Python (Join-Path $PSScriptRoot 'offline_delivery.py') installer-template --destination $template
    if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare the pinned offline installer template' }
    $templateRecord = $templateRecord | ConvertFrom-Json
    Invoke-Checked $Python @((Join-Path $PSScriptRoot 'offline_delivery.py'), 'source-identity', '--destination', (Join-Path $output 'source-identity.json'))
    @{
        bundle = @{
            resources = $null
            createUpdaterArtifacts = $false
            windows = @{
                webviewInstallMode = @{ type = 'skip' }
                nsis = @{ installerHooks = $hook; template = $template; compression = 'lzma' }
            }
        }
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $config -Encoding UTF8
    $prepared = [ordered]@{
        release_id = $releaseId; version = $version; git_commit = $initialCommit
        core_sha256 = (Get-FileHash -LiteralPath $CoreExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
        payload_manifest_sha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        hook_sha256 = (Get-FileHash -LiteralPath $hook -Algorithm SHA256).Hash.ToLowerInvariant()
        application_manifest_sha256 = (Get-FileHash -LiteralPath $applicationManifest -Algorithm SHA256).Hash.ToLowerInvariant()
        verifier_sha256 = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'installer/install-offline-runtime.ps1') -Algorithm SHA256).Hash.ToLowerInvariant()
        installer_template = $templateRecord
        source_identity_sha256 = (Get-FileHash -LiteralPath (Join-Path $output 'source-identity.json') -Algorithm SHA256).Hash.ToLowerInvariant()
        external_payload = $external; payload_bytes = $total; largest_payload_file = $largest
        capacity_warning = $(if ($total -ge (2GB - 64MB)) { 'Uncompressed total exceeds2GiB. Actual embedded NSIS attempt required.' } else { $null })
        fallback_reason = $(if ($external) { 'NSIS 2 GiB executable/data-block limit, conservative uncompressed input bound exceeded. Complete offline ZIP selected before unsupported bundling. https://nsis.sourceforge.io/Features' } else { $null })
    }
    $prepared | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $preparedPath -Encoding UTF8
    Copy-Item -LiteralPath (Join-Path $repository 'docs/offline-windows-operations.zh-CN.md') -Destination (Join-Path $output '离线安装与迁移说明.md')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'offline-runtime.lock.json') -Destination (Join-Path $output 'runtime-provenance.lock.json')
}
$prepared = Get-Content -LiteralPath $preparedPath -Raw | ConvertFrom-Json
if ($prepared.git_commit -ne $initialCommit -or $prepared.version -ne $version -or
    $prepared.core_sha256 -ne (Get-FileHash -LiteralPath $CoreExecutable -Algorithm SHA256).Hash.ToLowerInvariant() -or
    $prepared.payload_manifest_sha256 -ne (Get-FileHash -LiteralPath (Join-Path $payload 'payload-manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant() -or
    $prepared.hook_sha256 -ne (Get-FileHash -LiteralPath (Join-Path $output 'build/offline-hooks.nsh') -Algorithm SHA256).Hash.ToLowerInvariant() -or
    $prepared.application_manifest_sha256 -ne (Get-FileHash -LiteralPath $applicationManifest -Algorithm SHA256).Hash.ToLowerInvariant() -or
    $prepared.installer_template.patched_sha256 -ne (Get-FileHash -LiteralPath (Join-Path $output 'build/offline-installer.nsi') -Algorithm SHA256).Hash.ToLowerInvariant() -or
    $prepared.verifier_sha256 -ne (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'installer/install-offline-runtime.ps1') -Algorithm SHA256).Hash.ToLowerInvariant()) {
    throw 'Prepared Core, installer, payload or source identity changed. Prepare a new release.'
}
if ($PrepareOnly) {
    Write-Output "PREPARED LOCAL VERIFICATION $preparedPath"
    return
}
Invoke-Checked $Python @((Join-Path $PSScriptRoot 'offline_delivery.py'), 'source-identity', '--destination', (Join-Path $output 'source-identity.json'), '--verify')
Invoke-Checked $Python @((Join-Path $PSScriptRoot 'offline_delivery.py'), 'verify-payload', $payload)
$hostTriple = (& rustc -Vv | Select-String '^host:\s+(.+)$').Matches.Groups[1].Value.Trim()
if ($hostTriple -ne 'x86_64-pc-windows-msvc') { throw 'Only Windows x64 MSVC is supported' }
Copy-Item -LiteralPath $CoreExecutable -Destination (Join-Path $tauri "binaries/agent-core-$hostTriple.exe") -Force
& (Join-Path $PSScriptRoot 'verify-python-sidecar.ps1') -RepositoryRoot $repository
$started = [DateTime]::UtcNow
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$env:TEMP = Join-Path $output 'build/temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
Push-Location $desktop
try {
    # Cargo and compression run serially. The caller must stop resident model workers first.
    Invoke-Checked 'npm.cmd' @('run', 'tauri', '--', 'build', '--no-bundle', '--no-sign', '--config', $config, '--', '--locked', '-j1')
    Update-OfflineApplicationManifest $tauri $prepared.core_sha256 $applicationManifest
    $hookArguments = @((Join-Path $PSScriptRoot 'offline_delivery.py'), 'installer-hook',
        '--manifest', (Join-Path $payload 'payload-manifest.json'), '--destination', (Join-Path $output 'build/offline-hooks.nsh'),
        '--application-manifest', $applicationManifest)
    if (-not $prepared.external_payload) { $hookArguments += '--embedded' }
    Invoke-Checked $Python $hookArguments
    $prepared.application_manifest_sha256 = (Get-FileHash -LiteralPath $applicationManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $prepared.hook_sha256 = (Get-FileHash -LiteralPath (Join-Path $output 'build/offline-hooks.nsh') -Algorithm SHA256).Hash.ToLowerInvariant()
    $prepared | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $preparedPath -Encoding UTF8
    $bundleArguments = @('run', 'tauri', '--', 'bundle', '--bundles', 'nsis', '--no-sign', '--ci', '--config', $config)
    $bundleLog = Join-Path $output $(if ($prepared.external_payload) { 'build/nsis-external-attempt.log' } else { 'build/nsis-embedded-attempt.log' })
    & npm.cmd @bundleArguments 2>&1 | Tee-Object -FilePath $bundleLog | Out-Host
    $bundleExit = $LASTEXITCODE
    # Tauri patches the Host bundle-type marker in place before invoking NSIS.
    # Rebind the installer to those actual bytes, including when bundling fails.
    Update-OfflineApplicationManifest $tauri $prepared.core_sha256 $applicationManifest
    $bundledApplicationHash = (Get-FileHash -LiteralPath $applicationManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $applicationChanged = $bundledApplicationHash -ne $prepared.application_manifest_sha256
    Invoke-Checked $Python $hookArguments
    $prepared.application_manifest_sha256 = $bundledApplicationHash
    $prepared.hook_sha256 = (Get-FileHash -LiteralPath (Join-Path $output 'build/offline-hooks.nsh') -Algorithm SHA256).Hash.ToLowerInvariant()
    $prepared | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $preparedPath -Encoding UTF8
    if ($bundleExit -eq 0 -and $applicationChanged) {
        $bundleLog = Join-Path $output 'build/nsis-identity-rebind.log'
        & npm.cmd @bundleArguments 2>&1 | Tee-Object -FilePath $bundleLog | Out-Host
        $bundleExit = $LASTEXITCODE
    }
    if ($bundleExit -ne 0 -and -not $prepared.external_payload) {
        # Keep the actual compiler failure, then rebuild the same app as a
        # small installer consuming the complete adjacent verified payload.
        $prepared.external_payload = $true
        $prepared.fallback_reason = "Actual full embedded NSIS build failed with exit $bundleExit. See build/$([IO.Path]::GetFileName($bundleLog))."
        $prepared | Add-Member -NotePropertyName embedded_attempt -NotePropertyValue @{
            exit_code = $bundleExit
            log_name = 'build/' + [IO.Path]::GetFileName($bundleLog)
            log_sha256 = (Get-FileHash -LiteralPath $bundleLog -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        Invoke-Checked $Python @((Join-Path $PSScriptRoot 'offline_delivery.py'), 'installer-hook',
            '--manifest', (Join-Path $payload 'payload-manifest.json'), '--destination', (Join-Path $output 'build/offline-hooks.nsh'),
            '--application-manifest', $applicationManifest)
        $prepared.hook_sha256 = (Get-FileHash -LiteralPath (Join-Path $output 'build/offline-hooks.nsh') -Algorithm SHA256).Hash.ToLowerInvariant()
        $prepared | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $preparedPath -Encoding UTF8
        & npm.cmd @bundleArguments 2>&1 | Tee-Object -FilePath (Join-Path $output 'build/nsis-external-attempt.log') | Out-Host
        $bundleExit = $LASTEXITCODE
    }
    if ($bundleExit -ne 0) { throw "NSIS bundle failed with exit $bundleExit. Build logs were retained." }
    $expectedApplication = Get-Content -LiteralPath $applicationManifest -Raw | ConvertFrom-Json
    foreach ($name in @('agent-desktop.exe', 'agent-core.exe')) {
        $path = Join-Path $tauri ('target/release/' + $name)
        if ((Get-Item -LiteralPath $path).Length -ne $expectedApplication.files.$name.bytes -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedApplication.files.$name.sha256) {
            throw 'Bundling changed application identity after the installer manifest was anchored'
        }
    }
} finally {
    Pop-Location
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
}
$installers = @(Get-ChildItem -LiteralPath (Join-Path $tauri 'target/release/bundle/nsis') -Filter '*_x64-setup.exe' |
    Where-Object { $_.LastWriteTimeUtc -ge $started })
if ($installers.Count -ne 1) { throw 'Expected exactly one fresh installer' }
$installer = Join-Path $output "agent-$version-windows-x86_64-offline-local-setup.exe"
Copy-Item -LiteralPath $installers[0].FullName -Destination $installer
$smokePassed = $false
if ($InstallerSmokeTest) {
    & (Join-Path $PSScriptRoot 'test-windows-installer.ps1') -InstallerPath $installer -RepositoryRoot $repository -FullOffline
    $smokePassed = $true
}
$signature = Get-AuthenticodeSignature -LiteralPath $installer
$dirty = @(& git -C $repository status --porcelain=v1 --untracked-files=all).Count -gt 0
$protocolSource = Get-Content -LiteralPath (Join-Path $repository 'services/agent-core/src/agent/protocol.py') -Raw
$storageSource = Get-Content -LiteralPath (Join-Path $repository 'services/agent-core/src/agent/storage.py') -Raw
$appSignatures = @{}
foreach ($name in @('agent-desktop.exe', 'agent-core.exe')) {
    $path = Join-Path $tauri ('target/release/' + $name)
    $appSignatures[$name] = @{
        status = [string](Get-AuthenticodeSignature -LiteralPath $path).Status
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        bytes = (Get-Item -LiteralPath $path).Length
    }
}
$release = [ordered]@{
    product = 'agent'; version = $version; platform = 'windows-x86_64'; purpose = 'UNSIGNED LOCAL VERIFICATION'
    core_version = $version
    ipc_version = [int]([regex]::Match($protocolSource, '(?m)^PROTOCOL_VERSION\s*=\s*(\d+)').Groups[1].Value)
    schema_version = [int]([regex]::Match($storageSource, '(?m)^SCHEMA_VERSION\s*=\s*(\d+)').Groups[1].Value)
    build_id = [regex]::Match($protocolSource, '(?m)^BUILD_ID\s*=\s*"([^"]+)"').Groups[1].Value
    git_commit = $initialCommit; dirty_worktree = $dirty; allow_dirty = [bool]$AllowDirty
    skip_tests = [bool]$SkipTests; signed = ($signature.Status -eq 'Valid'); authenticode_status = [string]$signature.Status
    updater_signed = $false; public_updater_enabled = $false
    delivery_mode = $(if ($prepared.external_payload) { 'full-offline-zip' } else { 'single-exe' })
    application_authenticode = $appSignatures
    build_gates = @{ tests_passed = $false; installer_smoke_passed = $smokePassed; clean_windows_vm_passed = $false }
    built_at_utc = [DateTime]::UtcNow.ToString('o'); build = $prepared
    installer = @{ name = [IO.Path]::GetFileName($installer); bytes = (Get-Item -LiteralPath $installer).Length
        sha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() }
}
$release | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $output 'release-manifest.json') -Encoding UTF8
$finalizeArguments = @((Join-Path $PSScriptRoot 'offline_delivery.py'), 'archive', $output)
if (-not $prepared.external_payload) { $finalizeArguments += '--checksums-only' }
Invoke-Checked $Python $finalizeArguments
Write-Output "BUILT UNSIGNED LOCAL VERIFICATION $output"
