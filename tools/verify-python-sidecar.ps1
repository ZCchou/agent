[CmdletBinding()]
param(
    [string]$RepositoryRoot,
    [string]$SidecarPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $RepositoryRoot) {
    $RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path

$bridgePath = Join-Path $RepositoryRoot "apps/agent-desktop/src-tauri/src/core_bridge.rs"
if (-not (Test-Path -LiteralPath $bridgePath -PathType Leaf)) {
    throw "Cannot find the Host compatibility contract: $bridgePath"
}

$bridge = Get-Content -LiteralPath $bridgePath -Raw
function Read-ContractValue {
    param([string]$Pattern, [string]$Label)
    $match = [regex]::Match($bridge, $Pattern)
    if (-not $match.Success) {
        throw "Cannot read $Label from $bridgePath"
    }
    return $match.Groups[1].Value
}

$expectedProtocol = [int](Read-ContractValue 'PROTOCOL_VERSION:\s*u64\s*=\s*(\d+)' "IPC protocol version")
if ($bridge -match 'EXPECTED_CORE_VERSION:\s*&str\s*=\s*env!\("CARGO_PKG_VERSION"\)') {
    $cargoPath = Join-Path $RepositoryRoot "apps/agent-desktop/src-tauri/Cargo.toml"
    $cargoVersion = [regex]::Match((Get-Content -LiteralPath $cargoPath -Raw), '(?m)^version\s*=\s*"([^"]+)"')
    if (-not $cargoVersion.Success) { throw "Cannot read Host Cargo package version." }
    $expectedCore = $cargoVersion.Groups[1].Value
} else {
    $expectedCore = Read-ContractValue 'EXPECTED_CORE_VERSION:\s*&str\s*=\s*"([^"]+)"' "Core version"
}
$expectedSchema = [int](Read-ContractValue 'EXPECTED_SCHEMA_VERSION:\s*u64\s*=\s*(\d+)' "Core schema version")
$expectedBuild = Read-ContractValue 'EXPECTED_BUILD_ID:\s*&str\s*=\s*"([^"]+)"' "Core build ID"

if (-not $SidecarPath) {
    $rustHost = (& rustc -Vv | Select-String '^host:\s+(.+)$').Matches.Groups[1].Value.Trim()
    if (-not $rustHost) {
        throw "Cannot detect the Rust host target."
    }
    $SidecarPath = Join-Path $RepositoryRoot "apps/agent-desktop/src-tauri/binaries/agent-core-$rustHost.exe"
}
$SidecarPath = (Resolve-Path -LiteralPath $SidecarPath).Path

$smokeData = Join-Path ([System.IO.Path]::GetTempPath()) ("agent-sidecar-verification-" + [guid]::NewGuid().ToString("N"))
$previousData = $env:AGENT_DATA_DIR
try {
    $env:AGENT_DATA_DIR = $smokeData
    $payload = @{ v = $expectedProtocol; id = "release-sidecar-check"; type = "ping"; params = @{} } | ConvertTo-Json -Compress
    $inputPath = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($inputPath, "$payload`r`n", (New-Object System.Text.UTF8Encoding($false)))
    $previousErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $commandLine = '"{0}" < "{1}"' -f $SidecarPath, $inputPath
        $output = @(& $env:ComSpec /d /s /c $commandLine 2>&1)
        $sidecarExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorPreference
        if ($inputPath -and [System.IO.File]::Exists($inputPath)) {
            [System.IO.File]::Delete($inputPath)
        }
    }
    if ($sidecarExitCode -ne 0) {
        throw "Sidecar exited with code $sidecarExitCode."
    }
} finally {
    $env:AGENT_DATA_DIR = $previousData
}

$responseLine = $output | Where-Object { $_.ToString().TrimStart().StartsWith('{') } | Select-Object -Last 1
if (-not $responseLine) {
    throw "Sidecar did not return a JSON response."
}
$response = $responseLine.ToString() | ConvertFrom-Json
if ($response.PSObject.Properties.Name -notcontains "result") {
    throw "Sidecar ping failed: $($responseLine.ToString())"
}
$actual = $response.result
if (-not $actual) {
    throw "Sidecar ping response has no result payload."
}

$mismatches = @()
if ([int]$response.v -ne $expectedProtocol) { $mismatches += "IPC $($response.v), expected $expectedProtocol" }
if ([string]$actual.core_version -ne $expectedCore) { $mismatches += "Core $($actual.core_version), expected $expectedCore" }
if ([int]$actual.protocol_version -ne $expectedProtocol) { $mismatches += "protocol $($actual.protocol_version), expected $expectedProtocol" }
if ([int]$actual.schema_version -ne $expectedSchema) { $mismatches += "schema $($actual.schema_version), expected $expectedSchema" }
if ([string]$actual.build_id -ne $expectedBuild) { $mismatches += "build $($actual.build_id), expected $expectedBuild" }
if ($mismatches.Count -gt 0) {
    throw "Sidecar compatibility mismatch: $($mismatches -join '; ')"
}

Write-Host "[OK] Sidecar compatibility verified"
Write-Host "     Core $expectedCore | IPC $expectedProtocol | schema $expectedSchema | $expectedBuild"
Write-Host "     $SidecarPath"
