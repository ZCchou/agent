param([string]$InstallDirectory)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
foreach ($module in @('Microsoft.PowerShell.Management', 'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Security')) {
    Import-Module (Join-Path $PSHOME ('Modules/' + $module)) -ErrorAction Stop
}
$names = @('agent-desktop.exe', 'agent_desktop_lib.dll', 'agent-core.exe')
$mutex = $null
$locked = $false
$lockFile = $null

function Safe-Path([string]$Root, [string]$Relative = '') {
    if ($Relative -and ($Relative -match '(^|[\\/])\.\.([\\/]|$)|:|^[/\\]')) { throw 'Unsafe relative path' }
    $path = [IO.Path]::GetFullPath((Join-Path $Root $(if ($Relative) { $Relative } else { '.' })))
    $boundary = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($path -ne $boundary -and -not $path.StartsWith($boundary + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Path escaped installation' }
    $ancestor = $path
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked installation path' }
        }
        $parent = [IO.Path]::GetDirectoryName($ancestor)
        if ($parent -eq $ancestor) { break }
        $ancestor = $parent
    }
    return $path
}

function Matches([string]$Path, $Record) {
    $null = Safe-Path ([IO.Path]::GetDirectoryName($Path)) ([IO.Path]::GetFileName($Path))
    return (Test-Path -LiteralPath $Path -PathType Leaf) -and
        (Get-Item -LiteralPath $Path).Length -eq [long]$Record.bytes -and
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $Record.sha256
}

function Assert-Idle([string]$Root) {
    $prefix = $Root.TrimEnd('\') + '\'
    foreach ($process in Get-Process -ErrorAction Stop) {
        if ($process.Path -and $process.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Close the application and its workers before updating. Busy PID $($process.Id)"
        }
    }
}

function Recover-Pending {
    if (-not (Test-Path -LiteralPath $journalPath)) { return }
    $pending = Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($pending.id -cnotmatch '^[a-f0-9]{32}$' -or $pending.version -cne $manifest.version) { throw 'Unexpected pending update' }
    $previous = Safe-Path $managed ($pending.id + '/previous')
    foreach ($name in $names) {
        if (-not (Matches (Safe-Path $previous $name) $manifest.base_files.$name)) { throw 'Backup integrity mismatch' }
        $active = Safe-Path $root $name
        if (-not (Matches $active $manifest.base_files.$name) -and -not (Matches $active $manifest.files.$name)) {
            throw 'Application changed outside the pending update; backup retained'
        }
    }
    foreach ($name in $names) {
        $active = Safe-Path $root $name
        if (-not (Matches $active $manifest.base_files.$name)) {
            $restore = Safe-Path $managed ($pending.id + '/restore-' + $name)
            Copy-Item -LiteralPath (Safe-Path $previous $name) -Destination $restore
            if (-not (Matches $restore $manifest.base_files.$name)) { throw 'Restore candidate integrity mismatch' }
            [IO.File]::Replace($restore, $active, [NullString]::Value)
        }
    }
    Remove-Item -LiteralPath $journalPath
    Write-Output 'RECOVERED_PREVIOUS_APPLICATION'
}

try {
    $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'update.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.application_id -notin @('io.github.zcchou.agent.ocrpreview', 'io.github.zcchou.agent.documentpreview')) { throw 'Unsupported update manifest' }
    foreach ($field in @('files', 'base_files')) {
        $entries = @($manifest.$field.PSObject.Properties)
        if ($entries.Count -ne 3) { throw 'Unexpected application files' }
        foreach ($entry in $entries) {
            if ($entry.Name -cnotin $names -or $entry.Value.sha256 -cnotmatch '^[a-f0-9]{64}$' -or [long]$entry.Value.bytes -le 0) { throw 'Invalid application identity' }
        }
    }
    if (-not $InstallDirectory) {
        Add-Type -AssemblyName System.Windows.Forms
        $picker = New-Object Windows.Forms.FolderBrowserDialog
        $picker.Description = 'Select the existing agent installation folder containing agent-desktop.exe'
        if ($picker.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { throw 'Folder selection cancelled' }
        $InstallDirectory = $picker.SelectedPath
        $picker.Dispose()
    }
    $root = Safe-Path $InstallDirectory
    if ($root.TrimEnd('\') -eq [IO.Path]::GetPathRoot($root).TrimEnd('\')) { throw 'Choose an application installation directory' }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $key = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))).Replace('-', '') }
    finally { $hash.Dispose() }
    $mutex = New-Object Threading.Mutex($false, ('Local\agent-offline-install-' + $key))
    try { $locked = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another installer is operating on this directory' }
    Assert-Idle $root
    $runtimeCount = 0
    foreach ($entry in $manifest.runtime_manifests.PSObject.Properties) {
        if ($entry.Name -cnotmatch '^(llm-runtime|rag-runtime|ocr-runtime)/runtime\.json$|^ocr-model-package/ocr-model\.json$') { throw 'Invalid runtime manifest path' }
        if (-not (Matches (Safe-Path $root $entry.Name) $entry.Value)) { throw 'Runtime identity mismatch; use the complete installer' }
        $runtimeCount++
    }
    if (-not $runtimeCount) { throw 'Missing runtime identities' }
    foreach ($name in $names) {
        if (-not (Matches (Safe-Path $PSScriptRoot ('payload/' + $name)) $manifest.files.$name)) { throw 'Update payload integrity mismatch' }
    }
    $runtimeState = Safe-Path $root '.managed-runtime'
    if ($manifest.application_id -eq 'io.github.zcchou.agent.ocrpreview' -and (Test-Path -LiteralPath $runtimeState)) {
        $runtimeOwner = Get-Content -LiteralPath (Safe-Path $runtimeState 'owner.txt') -Raw
        if ($runtimeOwner.Trim() -cne 'io.github.zcchou.agent.ocrpreview.offline-runtime.v1') { throw 'Unowned installed runtime state' }
        $managed = Safe-Path $runtimeState 'application-updates'
    } else {
        $managed = Safe-Path $root '.agent-app-update'
    }
    if (Test-Path -LiteralPath $managed) {
        if ((Get-Content -LiteralPath (Safe-Path $managed 'owner.txt') -Raw).Trim() -cne 'agent-preview-app-update-v1') { throw 'Unowned update directory' }
    } else {
        New-Item -ItemType Directory -Path $managed | Out-Null
        Set-Content -LiteralPath (Join-Path $managed 'owner.txt') -Value 'agent-preview-app-update-v1' -Encoding ASCII
    }
    $lockFile = [IO.File]::Open((Safe-Path $managed 'update.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $journalPath = Safe-Path $managed 'pending.json'
    Recover-Pending
    $alreadyUpdated = $true
    foreach ($name in $names) { if (-not (Matches (Safe-Path $root $name) $manifest.files.$name)) { $alreadyUpdated = $false } }
    if ($alreadyUpdated) { Write-Output 'ALREADY_UPDATED'; exit 0 }
    foreach ($name in $names) {
        if (-not (Matches (Safe-Path $root $name) $manifest.base_files.$name)) { throw 'Previous application version mismatch; select the matching delta' }
    }
    $bytes = 512MB
    foreach ($name in $names) { $bytes += [long]$manifest.files.$name.bytes + [long]$manifest.base_files.$name.bytes }
    $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($root))
    if ($drive.AvailableFreeSpace -lt $bytes) { throw 'Insufficient space for staging and backup' }
    $id = [guid]::NewGuid().ToString('N')
    $transaction = Safe-Path $managed $id
    $previous = Safe-Path $transaction 'previous'
    $candidate = Safe-Path $transaction 'candidate'
    New-Item -ItemType Directory -Path $previous, $candidate | Out-Null
    foreach ($name in $names) {
        Copy-Item -LiteralPath (Safe-Path $root $name) -Destination (Join-Path $previous $name)
        Copy-Item -LiteralPath (Safe-Path $PSScriptRoot ('payload/' + $name)) -Destination (Join-Path $candidate $name)
        if (-not (Matches (Join-Path $candidate $name) $manifest.files.$name) -or -not (Matches (Join-Path $previous $name) $manifest.base_files.$name)) { throw 'Staging integrity mismatch' }
    }
    @{ id = $id; version = $manifest.version } | ConvertTo-Json | Set-Content -LiteralPath ($journalPath + '.new') -Encoding UTF8
    [IO.File]::Move(($journalPath + '.new'), $journalPath)
    try {
        Assert-Idle $root
        foreach ($name in $names) { [IO.File]::Replace((Join-Path $candidate $name), (Safe-Path $root $name), [NullString]::Value) }
        foreach ($name in $names) { if (-not (Matches (Safe-Path $root $name) $manifest.files.$name)) { throw 'Published application integrity mismatch' } }
        [IO.File]::Move($journalPath, (Join-Path $transaction 'completed.json'))
        Write-Output "UPDATED $($manifest.version)"
    } catch {
        Recover-Pending
        throw
    }
} catch {
    [Console]::Error.WriteLine('PREVIEW_UPDATE_ERROR ' + $_.Exception.Message)
    exit 1
} finally {
    if ($lockFile) { $lockFile.Dispose() }
    if ($locked) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
