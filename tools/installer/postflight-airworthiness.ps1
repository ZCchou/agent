[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InstallDirectory,
    [Parameter(Mandatory)][string]$ManifestPath,
    [Parameter(Mandatory)][string]$HealthDirectory
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
foreach ($module in @('Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Security')) {
    Import-Module (Join-Path $PSHOME ('Modules/' + $module)) -ErrorAction Stop
}
try {
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($property in $manifest.prerequisites.PSObject.Properties) {
        $name = $property.Name
        if ($name -notin @('vc_redist.x64.exe', 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe')) { throw 'Unexpected prerequisite' }
        $path = Join-Path $InstallDirectory ('prerequisites/' + $name)
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw "Invalid Microsoft prerequisite signature: $name" }
        if ((Get-Item -LiteralPath $path).VersionInfo.FileVersion -ne $property.Value.file_version) { throw "Prerequisite file version mismatch: $name" }
        $arguments = @('/install', '/quiet', '/norestart')
        if ($name -like 'MicrosoftEdge*') {
            $webviewId = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
            $present = $false
            foreach ($key in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$webviewId", "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$webviewId", "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$webviewId")) {
                if (Test-Path -LiteralPath $key) {
                    $version = Get-ItemPropertyValue -LiteralPath $key -Name pv -ErrorAction SilentlyContinue
                    if ($version -match '^\d+\.\d+\.\d+\.\d+$' -and $version -ne '0.0.0.0') { $present = $true }
                }
            }
            if ($present) { Write-Output 'WEBVIEW2_ALREADY_INSTALLED'; continue }
            $arguments = @('/silent', '/install')
        }
        Write-Output "PREREQUISITE $name"
        $child = Start-Process -FilePath $path -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
        $allowed = @(0, 3010)
        if ($name -eq 'vc_redist.x64.exe') { $allowed += 1638 }
        if ($child.ExitCode -notin $allowed) { throw "Prerequisite failed: $name exit $($child.ExitCode)" }
        if ($child.ExitCode -eq 3010) { Write-Output 'RESTART_REQUIRED' }
    }

    # Installer health must never open or migrate the user's real Core database.
    $application = Get-Content -LiteralPath (Join-Path $InstallDirectory 'application.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($application.application_id -cne 'io.github.zcchou.agent.airworthiness') { throw 'Wrong application health identity' }
    $contract = $application.core_contract
    if (Test-Path -LiteralPath $HealthDirectory) { throw 'Health directory must be new and isolated' }
    New-Item -ItemType Directory -Path $HealthDirectory | Out-Null
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = Join-Path $InstallDirectory 'agent-core.exe'
    $start.WorkingDirectory = $InstallDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables['AGENT_DATA_DIR'] = $HealthDirectory
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    if (-not $process.Start()) { throw 'Core health process failed to start' }
    try {
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        $request = @{ v = $contract.protocol_version; id = 'installer-health'; type = 'ping'; params = @{} } | ConvertTo-Json -Compress
        $process.StandardInput.WriteLine($request)
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(60000)) { $process.Kill(); $process.WaitForExit(); throw 'Isolated Core health timed out' }
        $output = $outputTask.GetAwaiter().GetResult()
        $errors = $errorTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw "Isolated Core health failed: $errors" }
        $line = @($output -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith('{') })[-1]
        $response = $line | ConvertFrom-Json
        if ($response.id -cne 'installer-health' -or $response.v -ne $contract.protocol_version -or
            $response.result.core_version -cne $application.version -or
            $response.result.protocol_version -ne $contract.protocol_version -or
            $response.result.schema_version -ne $contract.schema_version -or
            $response.result.build_id -cne $contract.build_id) { throw 'Isolated Core health contract mismatch' }
    } finally { $process.Dispose() }
    Write-Output 'ISOLATED_CORE_HEALTH_OK'
    exit 0
} catch {
    [Console]::Error.WriteLine('POSTFLIGHT_ERROR ' + $_.Exception.Message)
    exit 1
}
