[CmdletBinding()]
param(
    [ValidateSet('Verify', 'Preflight', 'Install', 'Commit', 'Recover', 'Uninstall')][string]$Action = 'Verify',
    [string]$PayloadDirectory,
    [string]$InstallDirectory,
    [string]$ManifestSha256,
    [switch]$RunPrerequisites,
    [switch]$IncludeOcr,
    [switch]$Airworthiness,
    [switch]$DeferCommit,
    [string]$TransactionToken,
    [int]$InstallerProcessId = 0,
    [string]$ApplicationManifestPath,
    [string]$ApplicationManifestSha256,
    [long]$RequiredBytes = 0,
    [ValidateSet('', 'DuringCopy', 'AfterCopy', 'AfterFirstActivation', 'InterruptAfterFirstActivation', 'InsufficientSpace')][string]$Fault = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# A parent PowerShell 7 process can pass its module path to Windows PowerShell.
# Load only the operating-system modules used by this embedded installer.
foreach ($module in @('Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Security')) {
    Import-Module (Join-Path $PSHOME ('Modules/' + $module)) -ErrorAction Stop
}
$components = @('llm-runtime', 'rag-runtime')
$appFiles = @('agent-desktop.exe', 'agent.exe', 'agent-core.exe', 'uninstall.exe')
$owner = 'io.github.zcchou.agent.offline-runtime.v1'
if ($IncludeOcr) {
    $components += @('ocr-runtime', 'ocr-model-package')
    $appFiles += 'agent_desktop_lib.dll'
    $owner = 'io.github.zcchou.agent.ocrpreview.offline-runtime.v1'
}
if ($Airworthiness) {
    $components = @('llm-runtime', 'rag-runtime', 'ocr-runtime', 'ocr-model-package', 'office-runtime')
    $appFiles += @('agent_desktop_lib.dll', 'agent-updater.exe', 'application.json')
    $owner = 'io.github.zcchou.agent.airworthiness.offline-runtime.v1'
}
$restartRequired = $false
if ($TransactionToken) { $TransactionToken = ([guid]::Parse($TransactionToken)).ToString('N') }

function Assert-Unlinked([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked path refused: $cursor" }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}

function Get-SafePath([string]$Root, [string]$Relative, [bool]$InspectLinks = $true) {
    if (-not $Relative -or $Relative.Contains('\') -or $Relative.Contains(':') -or $Relative.StartsWith('/')) {
        throw 'Unsafe relative payload path'
    }
    foreach ($part in $Relative.Split('/')) {
        if (-not $part -or $part -in @('.', '..') -or $part -match '[<>"|?*\x00-\x1f]' -or
            $part -match '[. ]$' -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$') {
            throw 'Unsafe Windows payload path'
        }
    }
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $resolved = [IO.Path]::GetFullPath((Join-Path $base $Relative))
    if (-not $resolved.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Escaping payload path' }
    if ($InspectLinks) { Assert-Unlinked $resolved }
    return $resolved
}

function Get-Tree([string]$Root) {
    Assert-Unlinked $Root
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push([IO.Path]::GetFullPath($Root))
    while ($pending.Count) {
        foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked entry refused: $($item.FullName)" }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) } else { $item }
        }
    }
}

function Read-Manifest([string]$Root, [string]$Digest) {
    if ($Digest -cnotmatch '^[a-f0-9]{64}$') { throw 'Missing embedded release manifest SHA256' }
    $path = Get-SafePath $Root 'payload-manifest.json'
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Digest) {
        throw 'Payload manifest differs from this installer release'
    }
    $manifest = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.release_id -cnotmatch '^[A-Za-z0-9._-]{1,100}$') {
        throw 'Invalid payload manifest identity'
    }
    return $manifest
}

function Read-ApplicationManifest([string]$Path, [string]$Digest) {
    Assert-Unlinked $Path
    if ($Digest -cnotmatch '^[a-f0-9]{64}$' -or
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Digest) {
        throw 'Application identity manifest differs from this installer'
    }
    $record = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    $allowed = @('agent-desktop.exe', 'agent-core.exe')
    if ($IncludeOcr) { $allowed += 'agent_desktop_lib.dll' }
    if ($Airworthiness) { $allowed += @('agent_desktop_lib.dll', 'agent-updater.exe', 'application.json') }
    if ($record.schema_version -ne 1 -or @($record.files.PSObject.Properties).Count -lt 2) { throw 'Invalid application identity manifest' }
    foreach ($entry in $record.files.PSObject.Properties) {
        if ($entry.Name -notin $allowed -or $entry.Value.bytes -le 0 -or $entry.Value.sha256 -cnotmatch '^[a-f0-9]{64}$') {
            throw 'Invalid application identity entry'
        }
    }
    $required = @('agent-desktop.exe', 'agent-core.exe')
    if ($Airworthiness) { $required += @('agent_desktop_lib.dll', 'agent-updater.exe', 'application.json') }
    foreach ($name in $required) {
        $entry = $record.files.PSObject.Properties[$name]
        if (-not $entry -or $entry.Value.bytes -le 0 -or $entry.Value.sha256 -cnotmatch '^[a-f0-9]{64}$') {
            throw 'Missing pinned Host or Core identity'
        }
    }
    return $record
}

function Verify-Application([string]$Root, $Record) {
    foreach ($entry in $Record.files.PSObject.Properties) {
        $path = Get-SafePath $Root $entry.Name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-Item -LiteralPath $path).Length -ne $entry.Value.bytes -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.Value.sha256) {
            throw "Installed application identity mismatch: $($entry.Name)"
        }
    }
    $uninstaller = Get-SafePath $Root 'uninstall.exe'
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf) -or (Get-Item -LiteralPath $uninstaller).Length -le 0) {
        throw 'Installed uninstaller is missing or empty'
    }
}

function Verify-Tree([string]$Root, $Manifest, [string[]]$OnlyComponents = @()) {
    $expected = @{}
    foreach ($property in $Manifest.files.PSObject.Properties) {
        $name = $property.Name
        # Get-Tree rejects links before entering any child directory. Inventory
        # names need lexical checks here, without repeating every ancestor stat.
        $null = Get-SafePath $Root $name $false
        if ($expected.ContainsKey($name)) { throw 'Case-colliding payload inventory' }
        if ($OnlyComponents.Count -and $name.Split('/')[0] -notin $OnlyComponents) { continue }
        if ($property.Value.sha256 -cnotmatch '^[a-f0-9]{64}$' -or $property.Value.bytes -lt 0) { throw 'Invalid payload hash record' }
        $expected[$name] = $property.Value
    }
    $observed = 0
    $roots = @($Root)
    if ($OnlyComponents.Count) { $roots = @($OnlyComponents | ForEach-Object { Get-SafePath $Root $_ }) }
    foreach ($treeRoot in $roots) {
        foreach ($item in Get-Tree $treeRoot) {
            $name = $item.FullName.Substring([IO.Path]::GetFullPath($Root).TrimEnd('\').Length + 1).Replace('\', '/')
            if (-not $OnlyComponents.Count -and $name -eq 'payload-manifest.json') { continue }
            if (-not $expected.ContainsKey($name)) { throw "Unlisted payload file: $name" }
            $record = $expected[$name]
            if ($item.Length -ne $record.bytes -or
                (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -cne $record.sha256) {
                throw "Payload hash or size mismatch: $name"
            }
            $observed++
        }
    }
    if ($observed -ne $expected.Count) { throw 'Missing payload files' }
}

function Assert-Idle([string]$Root) {
    $prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    foreach ($process in Get-Process -ErrorAction Stop) {
        if ($process.Path -and ([IO.Path]::GetFullPath($process.Path)).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Close the application and its managed workers before upgrading. Busy PID $($process.Id)"
        }
    }
}

function Write-State([string]$Path, $State) {
    $temporary = $Path + '.new'
    $State | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $temporary -Encoding UTF8
    if ([IO.File]::Exists($Path)) {
        [IO.File]::Replace($temporary, $Path, ($Path + '.previous'))
    } else {
        [IO.File]::Move($temporary, $Path)
    }
}

function Assert-Owned([string]$Root) {
    Assert-Unlinked $Root
    $marker = Join-Path $Root 'owner.txt'
    if (-not (Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker -Raw).Trim() -ne $owner) {
        throw 'Unowned managed runtime directory'
    }
}

function Assert-PreviousRuntimes([string]$Root, [string]$Managed) {
    $activeRecord = Join-Path $Managed 'active.json'
    if (Test-Path -LiteralPath $activeRecord) {
        $previous = Get-Content -LiteralPath $activeRecord -Raw -Encoding UTF8 | ConvertFrom-Json
        Verify-Tree $Root $previous $components
        return
    }
    foreach ($name in $components) {
        $component = Get-SafePath $Root $name
        if (-not (Test-Path -LiteralPath $component)) { continue }
        # A legacy bundle can be adopted only through its complete hash inventory.
        $manifestPath = Get-SafePath $component 'runtime.json'
        $legacy = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($legacy.schema_version -ne 1 -or $legacy.protocol_version -ne 1) { throw 'Unsupported previous runtime inventory' }
        $inventories = @($legacy.files)
        if ($name -eq 'llm-runtime') {
            foreach ($engine in $legacy.engines.PSObject.Properties) { $inventories += $engine.Value.files }
        }
        $files = @{}
        foreach ($inventory in $inventories) {
            foreach ($property in $inventory.PSObject.Properties) {
                $path = Get-SafePath $component $property.Name
                $files[$name + '/' + $property.Name] = @{ bytes = (Get-Item -LiteralPath $path).Length; sha256 = $property.Value }
            }
        }
        $files[$name + '/runtime.json'] = @{ bytes = (Get-Item -LiteralPath $manifestPath).Length
            sha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() }
        Verify-Tree $Root ([PSCustomObject]@{ files = [PSCustomObject]$files }) @($name)
    }
}

function Assert-TransactionAccess($Journal, [switch]$Commit, [switch]$AllowOrphan) {
    $ownerProcess = $null
    if ($Journal.PSObject.Properties['owner_pid']) {
        $ownerProcess = Get-Process -Id $Journal.owner_pid -ErrorAction SilentlyContinue
        if ($ownerProcess -and $ownerProcess.StartTime.ToUniversalTime().Ticks -ne $Journal.owner_started_ticks) { $ownerProcess = $null }
    }
    $sameToken = $TransactionToken -and $TransactionToken -ceq $Journal.id
    $sameOwner = $ownerProcess -and $InstallerProcessId -eq $Journal.owner_pid
    if ($Commit -and (-not $sameToken -or -not $sameOwner)) { throw 'Commit transaction identity does not match the initiating live installer' }
    if ($ownerProcess -and -not ($sameToken -and $sameOwner) -and $Journal.owner_pid -ne $PID) {
        throw 'Another live installer owns the active transaction'
    }
    if (-not $ownerProcess -and $Journal.PSObject.Properties['deferred'] -and $Journal.deferred -and -not $sameToken -and -not $AllowOrphan) {
        throw 'Recovery transaction identity differs from the initiating installer'
    }
}

function Recover-Transaction([string]$Root, [string]$Managed, [switch]$AllowOrphan) {
    $journalPath = Join-Path $Managed 'transaction.json'
    if (-not (Test-Path -LiteralPath $journalPath)) { return }
    $journal = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
    if ($journal.id -cnotmatch '^[a-f0-9]{32}$') { throw 'Invalid transaction journal' }
    Assert-TransactionAccess $journal -AllowOrphan:$AllowOrphan
    $transaction = Get-SafePath $Managed ('versions/' + $journal.id)
    Assert-Owned $transaction
    # A committed activation is retained, including after an interrupted final cleanup.
    if ($journal.phase -eq 'committed') {
        Remove-Item -LiteralPath $journalPath
        return
    }
    foreach ($name in $components) {
        $active = Get-SafePath $Root $name
        $backup = Get-SafePath $transaction ('previous/' + $name)
        $candidate = Get-SafePath $transaction ('candidate/' + $name)
        $quarantine = Get-SafePath $transaction ('failed/' + $name)
        if (Test-Path -LiteralPath $backup) {
            if (Test-Path -LiteralPath $active) { Move-Item -LiteralPath $active -Destination $quarantine }
            Move-Item -LiteralPath $backup -Destination $active
        } elseif ($name -notin $journal.preexisting -and -not (Test-Path -LiteralPath $candidate) -and (Test-Path -LiteralPath $active)) {
            Move-Item -LiteralPath $active -Destination $quarantine
        }
    }
    $appRecordPath = Get-SafePath $transaction 'previous-app.json'
    if (Test-Path -LiteralPath $appRecordPath) {
        $appRecords = Get-Content -LiteralPath $appRecordPath -Raw | ConvertFrom-Json
        foreach ($name in $appFiles) {
            $activeApp = Get-SafePath $Root $name
            $property = $appRecords.PSObject.Properties[$name]
            if ($property) {
                $backupApp = Get-SafePath $transaction ('previous-app/' + $name)
                if ((Get-FileHash -LiteralPath $backupApp -Algorithm SHA256).Hash.ToLowerInvariant() -cne $property.Value) {
                    throw 'Previous application backup integrity mismatch'
                }
                if ((Test-Path -LiteralPath $activeApp) -and
                    (Get-FileHash -LiteralPath $activeApp -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $property.Value) { continue }
                if (Test-Path -LiteralPath $activeApp) {
                    Move-Item -LiteralPath $activeApp -Destination (Get-SafePath $transaction ('failed-app/' + $name + '.' + [guid]::NewGuid().ToString('N')))
                }
                Copy-Item -LiteralPath $backupApp -Destination $activeApp
            } elseif (Test-Path -LiteralPath $activeApp) {
                Move-Item -LiteralPath $activeApp -Destination (Get-SafePath $transaction ('failed-app/' + $name + '.' + [guid]::NewGuid().ToString('N')))
            }
        }
    }
    $oldActive = Join-Path $transaction 'previous-active.json'
    $activeRecord = Join-Path $Managed 'active.json'
    if (Test-Path -LiteralPath $oldActive) {
        Copy-Item -LiteralPath $oldActive -Destination $activeRecord -Force
    } elseif (Test-Path -LiteralPath $activeRecord) {
        Remove-Item -LiteralPath $activeRecord
    }
    Remove-Item -LiteralPath $journalPath
}

function Install-Prerequisites([string]$Root, $Manifest) {
    foreach ($name in @('vc_redist.x64.exe', 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe')) {
        $path = Get-SafePath $Root ('prerequisites/' + $name)
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
            throw "Microsoft prerequisite signature invalid: $name"
        }
        if ((Get-Item -LiteralPath $path).VersionInfo.FileVersion -ne $Manifest.prerequisites.$name.file_version) {
            throw "Microsoft prerequisite file version differs from the installer inventory: $name"
        }
        if ($name -like 'MicrosoftEdge*') {
            $webviewId = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
            $clientKeys = @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$webviewId",
                "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$webviewId",
                "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$webviewId")
            $present = $false
            foreach ($key in $clientKeys) {
                if (Test-Path -LiteralPath $key) {
                    $version = Get-ItemPropertyValue -LiteralPath $key -Name 'pv' -ErrorAction SilentlyContinue
                    if ($version -and $version -match '^\d+\.\d+\.\d+\.\d+$' -and $version -ne '0.0.0.0') { $present = $true }
                }
            }
            if ($present) { Write-Output 'WEBVIEW2_ALREADY_INSTALLED'; continue }
        }
        $arguments = @('/install', '/quiet', '/norestart')
        if ($name -like 'MicrosoftEdge*') { $arguments = @('/silent', '/install') }
        $process = Start-Process -FilePath $path -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
        $allowed = @(0, 1638, 3010)
        if ($name -like 'MicrosoftEdge*') { $allowed = @(0, 3010) }
        if ($process.ExitCode -notin $allowed) { throw "Offline prerequisite failed: $name exit $($process.ExitCode)" }
        if ($process.ExitCode -eq 3010) { $script:restartRequired = $true; Write-Output 'RESTART_REQUIRED' }
    }
}

$installationMutex = $null
$hasInstallationMutex = $false
try {
    if ($Action -eq 'Verify') {
        $manifest = Read-Manifest $PayloadDirectory $ManifestSha256
        Verify-Tree $PayloadDirectory $manifest
        Write-Output "VERIFIED $($manifest.release_id)"
        exit 0
    }
    $installRoot = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\')
    if (-not $InstallDirectory -or $installRoot -eq [IO.Path]::GetPathRoot($installRoot).TrimEnd('\')) { throw 'Unsafe application directory' }
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $mutexHash = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($installRoot.ToLowerInvariant()))).Replace('-', '') }
    finally { $hasher.Dispose() }
    $installationMutex = New-Object Threading.Mutex($false, ('Local\agent-offline-install-' + $mutexHash))
    try { $hasInstallationMutex = $installationMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $hasInstallationMutex = $true }
    if (-not $hasInstallationMutex) { throw 'Another installer is operating on this application directory' }
    Assert-Unlinked $installRoot
    Assert-Idle $installRoot
    if ($Action -eq 'Preflight') {
        if ($RequiredBytes -le 0 -or $RequiredBytes -gt 1TB) { throw 'Invalid embedded payload space requirement' }
        Assert-Unlinked $PayloadDirectory
        $payloadDrive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($PayloadDirectory)))
        $targetDrive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($installRoot))
        $targetBytes = $RequiredBytes + 1GB
        if ($payloadDrive.Name -eq $targetDrive.Name) { $targetBytes += $RequiredBytes }
        if ($targetDrive.AvailableFreeSpace -lt $targetBytes -or $payloadDrive.AvailableFreeSpace -lt ($RequiredBytes + 1GB)) {
            throw 'Insufficient free space for embedded extraction, verified installation and rollback reserve'
        }
        Write-Output 'EMBEDDED_SPACE_VERIFIED'
        exit 0
    }
    $managed = Get-SafePath $installRoot '.managed-runtime'
    if (Test-Path -LiteralPath $managed) { Assert-Owned $managed }
    if ($Action -eq 'Commit') {
        Assert-Owned $managed
        $journalPath = Get-SafePath $managed 'transaction.json'
        $journal = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
        if ($journal.id -cnotmatch '^[a-f0-9]{32}$' -or $journal.phase -ne 'activated') { throw 'No verified activation is pending commit' }
        Assert-TransactionAccess $journal -Commit
        $transaction = Get-SafePath $managed ('versions/' + $journal.id)
        Assert-Owned $transaction
        $manifest = Read-Manifest $transaction $ManifestSha256
        if ($journal.application_manifest_sha256 -cne $ApplicationManifestSha256) { throw 'Application identity does not match this transaction' }
        $application = Read-ApplicationManifest (Get-SafePath $transaction 'application-manifest.json') $ApplicationManifestSha256
        Verify-Application $installRoot $application
        Verify-Tree $installRoot $manifest $components
        Write-State (Join-Path $managed 'active.json') $manifest
        $journal.phase = 'committed'
        Write-State $journalPath $journal
        Remove-Item -LiteralPath $journalPath
        Write-Output "COMMITTED $($manifest.release_id)"
        exit 0
    }
    if ($Action -in @('Recover', 'Uninstall')) {
        Assert-Owned $managed
        Recover-Transaction $installRoot $managed -AllowOrphan:($Action -eq 'Uninstall')
        if ($Action -eq 'Uninstall') {
            # Only files recorded in the active owned inventory can be removed.
            $activeRecord = Get-Content -LiteralPath (Join-Path $managed 'active.json') -Raw | ConvertFrom-Json
            Verify-Tree $installRoot $activeRecord $components
            foreach ($name in $components) {
                $path = Get-SafePath $installRoot $name
                $null = @(Get-Tree $path)
                Remove-Item -LiteralPath $path -Recurse -Force
            }
            Assert-Owned $managed
            $null = @(Get-Tree $managed)
            Remove-Item -LiteralPath $managed -Recurse -Force
        }
        exit 0
    }
    $manifest = Read-Manifest $PayloadDirectory $ManifestSha256
    if ($DeferCommit) {
        if ($TransactionToken -cnotmatch '^[a-f0-9]{32}$' -or $InstallerProcessId -le 0) { throw 'Deferred activation requires an initiating installer identity' }
        $initiator = Get-Process -Id $InstallerProcessId -ErrorAction Stop
        $application = Read-ApplicationManifest $ApplicationManifestPath $ApplicationManifestSha256
    } else { $initiator = Get-Process -Id $PID }
    $existingJournal = Join-Path $managed 'transaction.json'
    if (Test-Path -LiteralPath $existingJournal) {
        Assert-TransactionAccess (Get-Content -LiteralPath $existingJournal -Raw | ConvertFrom-Json) -AllowOrphan
    }
    Verify-Tree $PayloadDirectory $manifest
    $bytes = [long]0
    foreach ($property in $manifest.files.PSObject.Properties) { $bytes += [long]$property.Value.bytes }
    $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($installRoot))
    if ($Fault -eq 'InsufficientSpace' -or $drive.AvailableFreeSpace -lt ($bytes + 1GB)) { throw 'Insufficient disk space for verified staging and rollback reserve' }
    if ($RunPrerequisites) { Install-Prerequisites $PayloadDirectory $manifest }
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath $managed)) {
        New-Item -ItemType Directory -Path $managed | Out-Null
        Set-Content -LiteralPath (Join-Path $managed 'owner.txt') -Value $owner -Encoding ASCII
    }
    $lock = [IO.File]::Open((Join-Path $managed 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        Recover-Transaction $installRoot $managed -AllowOrphan
        Assert-PreviousRuntimes $installRoot $managed
        $id = [guid]::NewGuid().ToString('N')
        if ($DeferCommit) { $id = $TransactionToken }
        $transaction = Get-SafePath $managed ('versions/' + $id)
        foreach ($name in @('candidate', 'previous', 'failed', 'previous-app', 'failed-app')) {
            New-Item -ItemType Directory -Path (Join-Path $transaction $name) -Force | Out-Null
        }
        Set-Content -LiteralPath (Join-Path $transaction 'owner.txt') -Value $owner -Encoding ASCII
        Copy-Item -LiteralPath (Get-SafePath $PayloadDirectory 'payload-manifest.json') -Destination (Join-Path $transaction 'payload-manifest.json')
        if ($DeferCommit) {
            Copy-Item -LiteralPath $ApplicationManifestPath -Destination (Join-Path $transaction 'application-manifest.json')
        }
        $appRecords = [ordered]@{}
        foreach ($name in $appFiles) {
            $activeApp = Get-SafePath $installRoot $name
            if (Test-Path -LiteralPath $activeApp) {
                Copy-Item -LiteralPath $activeApp -Destination (Join-Path $transaction ('previous-app/' + $name))
                $appRecords[$name] = (Get-FileHash -LiteralPath $activeApp -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
        Write-State (Join-Path $transaction 'previous-app.json') $appRecords
        if (Test-Path -LiteralPath (Join-Path $managed 'active.json')) {
            Copy-Item -LiteralPath (Join-Path $managed 'active.json') -Destination (Join-Path $transaction 'previous-active.json')
        }
        $preexisting = @($components | Where-Object { Test-Path -LiteralPath (Join-Path $installRoot $_) })
        $journalPath = Join-Path $managed 'transaction.json'
        $journal = @{ id = $id; phase = 'copying'; preexisting = $preexisting
            owner_pid = $initiator.Id; owner_started_ticks = $initiator.StartTime.ToUniversalTime().Ticks; deferred = [bool]$DeferCommit
            application_manifest_sha256 = $ApplicationManifestSha256 }
        Write-State $journalPath $journal
        try {
            foreach ($name in $components) {
                Copy-Item -LiteralPath (Get-SafePath $PayloadDirectory $name) -Destination (Join-Path $transaction 'candidate') -Recurse
                if ($Fault -eq 'DuringCopy') { throw 'Injected partial copy failure' }
            }
            Verify-Tree (Join-Path $transaction 'candidate') $manifest $components
            if ($Fault -eq 'AfterCopy') { throw 'Injected copy failure' }
            $journal.phase = 'activating'
            Write-State $journalPath $journal
            foreach ($name in $components) {
                $active = Get-SafePath $installRoot $name
                if (Test-Path -LiteralPath $active) {
                    $null = @(Get-Tree $active)
                    Move-Item -LiteralPath $active -Destination (Join-Path $transaction ('previous/' + $name))
                }
                Move-Item -LiteralPath (Join-Path $transaction ('candidate/' + $name)) -Destination $active
                if ($name -eq $components[0] -and $Fault -eq 'AfterFirstActivation') { throw 'Injected activation failure' }
                if ($name -eq $components[0] -and $Fault -eq 'InterruptAfterFirstActivation') { [Environment]::Exit(97) }
            }
            Verify-Tree $installRoot $manifest $components
            if ($DeferCommit) {
                $journal.phase = 'activated'
                Write-State $journalPath $journal
                Write-Output "ACTIVATED_PENDING_APP_COPY $($manifest.release_id)"
            } else {
                Write-State (Join-Path $managed 'active.json') $manifest
                $journal.phase = 'committed'
                Write-State $journalPath $journal
                Remove-Item -LiteralPath $journalPath
                Write-Output "INSTALLED $($manifest.release_id)"
            }
        } catch {
            Recover-Transaction $installRoot $managed
            throw
        }
    } finally { $lock.Dispose() }
    if ($restartRequired) { exit 3010 }
    exit 0
} catch {
    [Console]::Error.WriteLine('OFFLINE_INSTALL_ERROR ' + $_.Exception.Message)
    exit 1
} finally {
    if ($hasInstallationMutex) { $installationMutex.ReleaseMutex() }
    if ($installationMutex) { $installationMutex.Dispose() }
}
