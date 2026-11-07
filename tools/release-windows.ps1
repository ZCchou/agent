[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipDependencyInstall,
    [switch]$AllowDirty,
    [switch]$InstallerSmokeTest,
    [ValidateRange(1, 5)]
    [int]$BundleAttempts = 3,
    [string]$OutputDirectory,
    [string]$ReleaseTag,
    [ValidateSet("preview", "stable")]
    [string]$UpdateChannel = "preview",
    [string]$ReleaseNotesFile,
    [string]$RagRuntimeDirectory,
    [ValidateSet("cpu", "cuda")]
    [string]$RagRuntimeDevice = "cuda"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktop = Join-Path $root "apps/agent-desktop"
$tauri = Join-Path $desktop "src-tauri"
$core = Join-Path $root "services/agent-core"
$sidecarBuilder = Join-Path $PSScriptRoot "build-python-sidecar.bat"
$sidecarVerifier = Join-Path $PSScriptRoot "verify-python-sidecar.ps1"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command is not available on PATH: $Name"
    }
}

function Invoke-Native {
    param(
        [string]$Label,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    Write-Step $Label
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }
}

function Read-RegexValue {
    param([string]$Path, [string]$Pattern, [string]$Label)
    $text = Get-Content -LiteralPath $Path -Raw
    $match = [regex]::Match($text, $Pattern)
    if (-not $match.Success) {
        throw "Cannot read $Label from $Path"
    }
    return $match.Groups[1].Value
}

if ($env:OS -ne "Windows_NT") {
    throw "The Windows release pipeline must run on Windows."
}
foreach ($command in @("git", "node", "npm.cmd", "rustc", "cargo", "conda")) {
    Require-Command $command
}

if (-not $AllowDirty) {
    $dirty = @(git -C $root status --porcelain=v1 --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect the Git worktree." }
    if ($dirty.Count -gt 0) {
        throw "Release builds require a clean worktree. Commit the changes or pass -AllowDirty for a local verification build."
    }
}
$initialCommit = (git -C $root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Cannot capture the initial source commit." }
$officialBuild = -not ($SkipTests -or $AllowDirty)
if ($officialBuild -and -not $InstallerSmokeTest) {
    throw "Official packaging requires -InstallerSmokeTest. Use the full authoritative release gates."
}
if ($officialBuild -and [string]::IsNullOrWhiteSpace($env:TAURI_SIGNING_PRIVATE_KEY)) {
    throw "Official packaging requires the TAURI_SIGNING_PRIVATE_KEY build environment secret."
}

$package = Get-Content -LiteralPath (Join-Path $desktop "package.json") -Raw | ConvertFrom-Json
$appVersion = [string]$package.version
if (-not $ReleaseNotesFile) {
    $versionNotes = Join-Path $root "docs/releases/$appVersion.md"
    if (Test-Path -LiteralPath $versionNotes -PathType Leaf) { $ReleaseNotesFile = $versionNotes }
}
$cargoVersion = Read-RegexValue (Join-Path $tauri "Cargo.toml") '(?m)^version\s*=\s*"([^"]+)"' "Cargo package version"
$corePackageVersion = Read-RegexValue (Join-Path $core "pyproject.toml") '(?m)^version\s*=\s*"([^"]+)"' "Python package version"
$coreRuntimeVersion = Read-RegexValue (Join-Path $core "src/agent/protocol.py") '(?m)^CORE_VERSION\s*=\s*"([^"]+)"' "Core runtime version"
$tauriConfig = Get-Content -LiteralPath (Join-Path $tauri "tauri.conf.json") -Raw | ConvertFrom-Json

$versionMismatches = @()
if ($cargoVersion -ne $appVersion) { $versionMismatches += "Cargo=$cargoVersion" }
if ($corePackageVersion -ne $appVersion) { $versionMismatches += "Python package=$corePackageVersion" }
if ($coreRuntimeVersion -ne $appVersion) { $versionMismatches += "Core runtime=$coreRuntimeVersion" }
if ([string]$tauriConfig.version -ne "../package.json") { $versionMismatches += "Tauri version source=$($tauriConfig.version)" }
if ($versionMismatches.Count -gt 0) {
    throw "Version contract mismatch. Desktop=$appVersion; $($versionMismatches -join '; ')"
}
if (-not $ReleaseTag) {
    $ReleaseTag = if ($env:GITHUB_REF_TYPE -eq "tag") { $env:GITHUB_REF_NAME } elseif ($UpdateChannel -eq "stable") { "v$appVersion" } else { "v$appVersion-preview.1" }
}
$expectedTagPattern = if ($UpdateChannel -eq "stable") { "^v$([regex]::Escape($appVersion))$" } else { "^v$([regex]::Escape($appVersion))-preview\.[1-9]\d*$" }
if ($ReleaseTag -notmatch $expectedTagPattern -or ($env:GITHUB_REF_TYPE -eq "tag" -and $env:GITHUB_REF_NAME -ne $ReleaseTag)) {
    throw "Release tag, application version and update channel disagree."
}
if ($tauriConfig.bundle.createUpdaterArtifacts -ne $true) {
    throw "The authoritative Tauri configuration must enable bundle.createUpdaterArtifacts."
}

if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $root ".artifacts/releases/$ReleaseTag/windows-x86_64"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory = Join-Path $root $OutputDirectory
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
if ((Test-Path -LiteralPath $OutputDirectory) -and @(Get-ChildItem -LiteralPath $OutputDirectory -Force).Count -gt 0) {
    throw "Release output must be empty. Existing release artifacts are never overwritten."
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

Write-Host "agent Windows release $ReleaseTag ($UpdateChannel)" -ForegroundColor Green
Write-Host "Repository: $root"
Write-Host "Artifacts:  $OutputDirectory"

if (-not $SkipDependencyInstall) {
    Invoke-Native "Install locked Node dependencies" "npm.cmd" @("ci") $desktop
}

$sidecarArguments = @()
if ($SkipTests) { $sidecarArguments += "--skip-tests" }
Invoke-Native "Build and verify the Python Core sidecar" $sidecarBuilder $sidecarArguments $root

if (-not $RagRuntimeDirectory) {
    $RagRuntimeDirectory = Join-Path $root ".artifacts/rag-runtime-$RagRuntimeDevice/rag-runtime"
}
if (-not (Test-Path -LiteralPath (Join-Path $RagRuntimeDirectory "runtime.json"))) {
    & (Join-Path $PSScriptRoot "build-rag-runtime.ps1") -Device $RagRuntimeDevice -OutputDirectory $RagRuntimeDirectory
    if ($LASTEXITCODE -ne 0) { throw "$RagRuntimeDevice inference runtime build failed." }
}
$stageRuntime = Join-Path $tauri "binaries/rag-runtime"
Invoke-Native "Verify and stage local inference runtime" "conda" @("run", "--no-capture-output", "-n", "agent", "python", (Join-Path $PSScriptRoot "rag_runtime_bundle.py"), $RagRuntimeDirectory, "--destination", $stageRuntime, "--repository", $root, "--required-device", $RagRuntimeDevice) $root

if (-not $SkipTests) {
    Invoke-Native "Verify context retention and negative mutations" "conda" @("run", "--no-capture-output", "-n", "agent", "python", "evals/context/run_context_eval.py", "--verify-mutations") $core
    Invoke-Native "Run frontend unit, UI, and production build gates" "npm.cmd" @("run", "check") $desktop
    Invoke-Native "Check Rust formatting" "cargo" @("fmt", "--all", "--", "--check") $tauri
    Invoke-Native "Run Rust tests" "cargo" @("test", "--locked") $tauri
    Invoke-Native "Run strict Rust Clippy" "cargo" @("clippy", "--locked", "--all-targets", "--all-features", "--", "-D", "warnings") $tauri
}

& $sidecarVerifier -RepositoryRoot $root

$buildArguments = @("run", "tauri", "--", "build", "--bundles", "nsis")
$signingConfigPath = $null
$thumbprint = [string]$env:AGENT_WINDOWS_CERTIFICATE_THUMBPRINT
if ($thumbprint) {
    $timestampUrl = if ($env:AGENT_WINDOWS_TIMESTAMP_URL) { $env:AGENT_WINDOWS_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
    $useTsp = $env:AGENT_WINDOWS_TIMESTAMP_TSP -eq "1"
    $signingConfigPath = Join-Path $root ".artifacts/tauri-signing-config.json"
    $signingDirectory = Split-Path $signingConfigPath -Parent
    New-Item -ItemType Directory -Force -Path $signingDirectory | Out-Null
    @{
        bundle = @{
            windows = @{
                certificateThumbprint = $thumbprint
                digestAlgorithm = "sha256"
                timestampUrl = $timestampUrl
                tsp = $useTsp
            }
        }
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $signingConfigPath -Encoding UTF8
    $buildArguments += @("--config", $signingConfigPath)
    Write-Host "[SIGN] Authenticode signing enabled through a certificate thumbprint supplied by the environment."
} else {
    Write-Warning "No Authenticode certificate was supplied. Windows will identify the publisher as unknown; updater signing is a separate requirement."
}
if (-not $officialBuild -and [string]::IsNullOrWhiteSpace($env:TAURI_SIGNING_PRIVATE_KEY)) {
    # Explicit local verification may run without the official updater key.
    # Override only generated bundler configuration, never tracked source.
    $localConfigPath = Join-Path $root ".artifacts/tauri-local-bundle-config.json"
    @{ bundle = @{ createUpdaterArtifacts = $false } } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $localConfigPath -Encoding UTF8
    $buildArguments += @("--config", $localConfigPath)
}

# Append runner arguments after all Tauri options, including signing config.
$buildArguments += @("--", "--locked")

$built = $false
$bundleStartedAt = [DateTime]::UtcNow
for ($attempt = 1; $attempt -le $BundleAttempts; $attempt++) {
    try {
        Invoke-Native "Build NSIS installer (attempt $attempt/$BundleAttempts)" "npm.cmd" $buildArguments $desktop
        $built = $true
        break
    } catch {
        if ($attempt -ge $BundleAttempts) { throw }
        Write-Warning "Bundling attempt $attempt failed. Tauri tool downloads are cached; retrying the same immutable build."
        Start-Sleep -Seconds ([Math]::Min(5 * $attempt, 15))
    }
}
if (-not $built) { throw "NSIS bundle was not produced." }

$sourceArguments = @("run", "--no-capture-output", "-n", "agent", "python", (Join-Path $PSScriptRoot "release_windows.py"), "normalize-source", "--root", $root, "--initial-commit", $initialCommit)
if ($AllowDirty) { $sourceArguments += "--allow-dirty" }
Invoke-Native "Verify immutable source and normalize only canonical-identical Cargo line endings" "conda" $sourceArguments $root

$installers = @(Get-ChildItem -LiteralPath (Join-Path $tauri "target/release/bundle/nsis") -Filter "*_${appVersion}_x64-setup.exe" -File |
    Where-Object { $_.LastWriteTimeUtc -ge $bundleStartedAt })
if ($installers.Count -ne 1) { throw "Expected exactly one fresh NSIS installer for version $appVersion." }
$installer = $installers[0]
$desktopExe = Get-Item -LiteralPath (Join-Path $tauri "target/release/agent-desktop.exe")
$sidecarExe = Get-Item -LiteralPath (Join-Path $tauri "target/release/agent-core.exe")
if (-not $installer -or $installer.Length -le 0) { throw "Tauri completed without a valid NSIS installer." }

$releaseFiles = @(
    @{ Source = $installer.FullName; Name = "agent-$appVersion-windows-x86_64-setup.exe" },
    @{ Source = $desktopExe.FullName; Name = "agent-desktop.exe" },
    @{ Source = $sidecarExe.FullName; Name = "agent-core.exe" }
)
$installerSignature = "$($installer.FullName).sig"
$updaterSigned = (Test-Path -LiteralPath $installerSignature -PathType Leaf) -and ((Get-Item -LiteralPath $installerSignature).LastWriteTimeUtc -ge $bundleStartedAt)
if ($officialBuild -and -not $updaterSigned) { throw "Tauri did not produce the required updater signature." }
if ($updaterSigned) {
    $releaseFiles += @{ Source = $installerSignature; Name = "agent-$appVersion-windows-x86_64-setup.exe.sig" }
}
foreach ($file in $releaseFiles) {
    Copy-Item -LiteralPath $file.Source -Destination (Join-Path $OutputDirectory $file.Name) -Force
}
Invoke-Native "Include verified inference runtime in portable release" "conda" @("run", "--no-capture-output", "-n", "agent", "python", (Join-Path $PSScriptRoot "rag_runtime_bundle.py"), $stageRuntime, "--destination", (Join-Path $OutputDirectory "rag-runtime"), "--repository", $root, "--required-device", $RagRuntimeDevice) $root

if ($InstallerSmokeTest) {
    & (Join-Path $PSScriptRoot "test-windows-installer.ps1") `
        -InstallerPath (Join-Path $OutputDirectory "agent-$appVersion-windows-x86_64-setup.exe") `
        -RepositoryRoot $root
}

$commit = (git -C $root rev-parse HEAD).Trim()
if ($commit -ne $initialCommit) { throw "Release HEAD drifted during packaging." }
$isDirty = @(git -C $root status --porcelain=v1 --untracked-files=all).Count -gt 0
if ($isDirty -and -not $AllowDirty) { throw "Unexpected source changes after packaging." }
$manifestFiles = foreach ($file in $releaseFiles) {
    $path = Join-Path $OutputDirectory $file.Name
    $item = Get-Item -LiteralPath $path
    [ordered]@{
        name = $item.Name
        bytes = $item.Length
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$manifest = [ordered]@{
    product = "agent"
    version = $appVersion
    platform = "windows-x86_64"
    git_commit = $commit
    dirty_worktree = $isDirty
    core_version = $coreRuntimeVersion
    ipc_version = [int](Read-RegexValue (Join-Path $core "src/agent/protocol.py") '(?m)^PROTOCOL_VERSION\s*=\s*(\d+)' "IPC version")
    schema_version = [int](Read-RegexValue (Join-Path $core "src/agent/storage.py") '(?m)^SCHEMA_VERSION\s*=\s*(\d+)' "schema version")
    build_id = Read-RegexValue (Join-Path $core "src/agent/protocol.py") '(?m)^BUILD_ID\s*=\s*"([^"]+)"' "build ID"
    rag_runtime = @{
        manifest = "rag-runtime/runtime.json"
        manifest_sha256 = (Get-FileHash -LiteralPath (Join-Path $OutputDirectory "rag-runtime/runtime.json") -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    signed = [bool]$thumbprint
    updater_signed = [bool]$updaterSigned
    release_tag = $ReleaseTag
    update_channel = $UpdateChannel
    build_gates = @{ tests_passed = -not [bool]$SkipTests; installer_smoke_passed = [bool]$InstallerSmokeTest }
    built_at_utc = [DateTime]::UtcNow.ToString("o")
    files = @($manifestFiles)
}
if ($env:GITHUB_RUN_ID) {
    $manifest.build_provenance = @{ repository = $env:GITHUB_REPOSITORY; workflow_run_id = $env:GITHUB_RUN_ID; commit = $commit }
}
$manifestPath = Join-Path $OutputDirectory "release-manifest.json"
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

$checksumFiles = @($releaseFiles | ForEach-Object { Join-Path $OutputDirectory $_.Name }) + $manifestPath
$checksumLines = foreach ($path in $checksumFiles) {
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([System.IO.Path]::GetFileName($path))"
}
$checksumLines | Set-Content -LiteralPath (Join-Path $OutputDirectory "SHA256SUMS.txt") -Encoding ASCII

$expectedArtifactNames = @($releaseFiles | ForEach-Object { $_.Name }) + @("release-manifest.json", "SHA256SUMS.txt")
$unexpectedArtifacts = @(Get-ChildItem -LiteralPath $OutputDirectory -File | Where-Object { $_.Name -notin $expectedArtifactNames })
if ($unexpectedArtifacts.Count -gt 0) {
    throw "The release directory contains unexpected files: $($unexpectedArtifacts.Name -join ', '). Use an empty output directory."
}

if ($thumbprint) {
    foreach ($file in $releaseFiles | Where-Object { $_.Name -like "*.exe" }) {
        $signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $OutputDirectory $file.Name)
        if ($signature.Status -ne "Valid") {
            throw "Authenticode validation failed for $($file.Name): $($signature.Status)"
        }
    }
}

if ($officialBuild) {
    $publicArguments = @("run", "--no-capture-output", "-n", "agent", "python", (Join-Path $PSScriptRoot "release_windows.py"), "prepare-public", "--directory", $OutputDirectory, "--config", (Join-Path $tauri "tauri.conf.json"))
    if ($ReleaseNotesFile) { $publicArguments += @("--notes-file", (Resolve-Path -LiteralPath $ReleaseNotesFile).Path) }
    Invoke-Native "Verify updater signature and prepare installer-only public distribution" "conda" $publicArguments $root
} elseif ($updaterSigned) {
    Invoke-Native "Verify local updater signature" "node" @((Join-Path $PSScriptRoot "verify-updater-signature.cjs"), (Join-Path $OutputDirectory "agent-$appVersion-windows-x86_64-setup.exe"), (Join-Path $OutputDirectory "agent-$appVersion-windows-x86_64-setup.exe.sig"), (Join-Path $tauri "tauri.conf.json")) $root
}

Write-Host ""
Write-Host "Release artifacts are ready:" -ForegroundColor Green
Get-ChildItem -LiteralPath $OutputDirectory -File | ForEach-Object {
    Write-Host ("  {0,-55} {1,12:N0} bytes" -f $_.Name, $_.Length)
}
