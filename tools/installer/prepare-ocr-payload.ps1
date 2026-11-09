[CmdletBinding()]
param(
    [ValidateSet('Prepare','Cleanup')][string]$Action = 'Prepare',
    [string]$ArchiveDirectory,
    [string]$PartsManifestPath,
    [string]$PayloadManifestPath,
    [Parameter(Mandatory)][string]$InstallDirectory
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility')
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security')
Add-Type -AssemblyName System.IO.Compression.FileSystem
$owner = 'io.github.zcchou.agent.ocrpreview.payload.v1'
$install = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\')
if ($install -eq [IO.Path]::GetPathRoot($install).TrimEnd('\')) { throw 'Unsafe installation root' }
$stage = Join-Path $install '.ocr-preview-payload'
$payload = Join-Path $stage 'payload'
$marker = Join-Path $stage 'owner.txt'

function Assert-Plain([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked payload path refused' }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}
function Clean-OwnedStage {
    Assert-Plain $stage
    if (-not (Test-Path -LiteralPath $stage)) { return }
    if (-not (Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker -Raw).Trim() -cne $owner) { throw 'Unowned extraction directory' }
    $target = [IO.Path]::GetFullPath($stage)
    if (-not $target.StartsWith($install + '\', [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($target) -ne '.ocr-preview-payload') { throw 'Unsafe cleanup target' }
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push($stage)
    while ($pending.Count) {
        foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked extraction entry refused' }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
    Remove-Item -LiteralPath $target -Recurse -Force
}

try {
    Assert-Plain $install
    if ($Action -eq 'Cleanup') { Clean-OwnedStage; exit 0 }
    Assert-Plain $ArchiveDirectory
    $parts = Get-Content -LiteralPath $PartsManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $manifest = Get-Content -LiteralPath $PayloadManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($parts.schema_version -ne 1 -or $manifest.schema_version -ne 1 -or @($parts.archives).Count -lt 1 -or @($parts.archives).Count -gt 64) { throw 'Invalid offline archive manifest' }
    $expected = @{}
    $bytes = [long]0
    foreach ($property in $manifest.files.PSObject.Properties) {
        if ($expected.ContainsKey($property.Name) -or $property.Value.bytes -lt 0 -or $property.Value.sha256 -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid payload identity' }
        $expected[$property.Name] = $property.Value
        $bytes += [long]$property.Value.bytes
    }
    if ($expected.Count -lt 1 -or $expected.Count -gt 100000) { throw 'Invalid payload file count' }
    if ((New-Object IO.DriveInfo([IO.Path]::GetPathRoot($install))).AvailableFreeSpace -lt (2 * $bytes + 1GB)) { throw 'Insufficient disk space for offline extraction, staging and rollback' }
    $archiveNames = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($part in $parts.archives) {
        if ($part.name -cnotmatch '^[A-Za-z0-9._-]+\.zip$' -or -not $archiveNames.Add($part.name) -or $part.sha256 -cnotmatch '^[a-f0-9]{64}$' -or $part.bytes -le 0 -or $part.bytes -ge 2GB) { throw 'Invalid offline archive entry' }
        $path = Join-Path $ArchiveDirectory $part.name
        Assert-Plain $path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing offline archive $($part.name). Download every part into the setup directory." }
        if ((Get-Item -LiteralPath $path).Length -ne $part.bytes -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $part.sha256) { throw "Offline archive hash mismatch $($part.name)" }
    }
    Clean-OwnedStage
    New-Item -ItemType Directory -Path $payload -Force | Out-Null
    Set-Content -LiteralPath $marker -Value $owner -Encoding ASCII
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($part in $parts.archives) {
        $zip = [IO.Compression.ZipFile]::OpenRead((Join-Path $ArchiveDirectory $part.name))
        try {
            foreach ($entry in $zip.Entries) {
                $name = $entry.FullName
                if ($name.Contains('\') -or $name.Contains(':') -or $name.StartsWith('/') -or @($name.Split('/') | Where-Object { $_ -in @('', '.', '..') -or $_ -match '[<>"|?*\x00-\x1f]' -or $_.EndsWith('.') -or $_.EndsWith(' ') -or $_ -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$' }).Count) { throw 'Unsafe archive entry' }
                if (-not $expected.ContainsKey($name) -or -not $seen.Add($name)) { throw 'Unexpected or duplicate archive entry' }
                $record = $expected[$name]
                if ($entry.Length -ne $record.bytes) { throw 'Archive entry size differs from manifest' }
                $destination = [IO.Path]::GetFullPath((Join-Path $payload $name.Replace('/', '\')))
                if (-not $destination.StartsWith($payload + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Archive entry escapes extraction root' }
                New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($destination)) -Force | Out-Null
                $reader = $entry.Open()
                $writer = [IO.File]::Open($destination, 'CreateNew', 'Write', 'None')
                try {
                    $buffer = New-Object byte[] 131072
                    $written = [long]0
                    while (($count = $reader.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $written += $count
                        if ($written -gt $record.bytes) { throw 'Archive entry expanded past its limit' }
                        $writer.Write($buffer, 0, $count)
                    }
                } finally { $writer.Dispose(); $reader.Dispose() }
                if ($written -ne $record.bytes -or (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -cne $record.sha256) { throw 'Extracted payload hash mismatch' }
            }
        } finally { $zip.Dispose() }
    }
    if ($seen.Count -ne $expected.Count) { throw 'Offline archives are incomplete' }
    Copy-Item -LiteralPath $PayloadManifestPath -Destination (Join-Path $payload 'payload-manifest.json')
    Write-Output "PREPARED $payload"
    exit 0
} catch {
    [Console]::Error.WriteLine('OCR_PAYLOAD_ERROR ' + $_.Exception.Message)
    exit 1
}
