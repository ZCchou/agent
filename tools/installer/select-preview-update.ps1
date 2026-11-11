param([string]$InstallDirectory)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
foreach ($module in @('Microsoft.PowerShell.Management', 'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Security')) {
    Import-Module (Join-Path $PSHOME ('Modules/' + $module)) -ErrorAction Stop
}
$context = $null
$result = 1

try {
    if (-not $InstallDirectory) {
        Add-Type -AssemblyName System.Windows.Forms
        $picker = New-Object Windows.Forms.FolderBrowserDialog
        $picker.Description = 'Select the existing agent installation folder containing agent-desktop.exe'
        if ($picker.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { throw 'Folder selection cancelled' }
        $InstallDirectory = $picker.SelectedPath
        $picker.Dispose()
    }
    $hostPath = Join-Path $InstallDirectory 'agent-desktop.exe'
    if (-not (Test-Path -LiteralPath $hostPath -PathType Leaf)) { throw 'Select the folder containing agent-desktop.exe' }
    $hostHash = (Get-FileHash -LiteralPath $hostPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $hostBytes = (Get-Item -LiteralPath $hostPath).Length
    $matches = @()
    foreach ($kind in @('ocr', 'document')) {
        $directory = Join-Path $PSScriptRoot ('profiles/' + $kind)
        $manifest = Get-Content -LiteralPath (Join-Path $directory 'update.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($manifest.application_id -cne ('io.github.zcchou.agent.' + $kind + 'preview')) { throw 'Invalid preview profile identity' }
        $old = $manifest.base_files.'agent-desktop.exe'
        $new = $manifest.files.'agent-desktop.exe'
        if (($hostHash -ceq $old.sha256 -and $hostBytes -eq [long]$old.bytes) -or
            ($hostHash -ceq $new.sha256 -and $hostBytes -eq [long]$new.bytes)) {
            $matches += @{ Directory = $directory; Manifest = $manifest }
        }
    }
    if ($matches.Count -ne 1) { throw 'The selected application does not match one supported preview version' }
    $selected = $matches[0]
    Write-Output ('DETECTED ' + $selected.Manifest.application_id)
    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    $context = Join-Path $temporaryRoot ('agent-preview-update-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $context | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $context 'payload') | Out-Null
    Copy-Item -LiteralPath (Join-Path $selected.Directory 'update.json') -Destination (Join-Path $context 'update.json')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'engine.ps1') -Destination (Join-Path $context 'apply-update.ps1')
    foreach ($name in @('agent-desktop.exe', 'agent_desktop_lib.dll')) {
        Copy-Item -LiteralPath (Join-Path $selected.Directory ('payload/' + $name)) -Destination (Join-Path $context ('payload/' + $name))
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'shared/agent-core.exe') -Destination (Join-Path $context 'payload/agent-core.exe')
    & (Join-Path $PSHOME 'powershell.exe') -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $context 'apply-update.ps1') -InstallDirectory $InstallDirectory
    $result = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('PREVIEW_UPDATE_ERROR ' + $_.Exception.Message)
    $result = 1
} finally {
    if ($context -and (Test-Path -LiteralPath $context)) {
        try {
            $resolved = [IO.Path]::GetFullPath($context)
            if (-not $resolved.StartsWith($temporaryRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
                [IO.Path]::GetFileName($resolved) -cnotmatch '^agent-preview-update-[a-f0-9]{32}$') { throw 'Unsafe temporary update context' }
            foreach ($directory in @($context, (Join-Path $context 'payload'))) {
                if ((Get-Item -LiteralPath $directory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked temporary update context' }
            }
            foreach ($name in @('update.json', 'apply-update.ps1', 'payload/agent-desktop.exe', 'payload/agent_desktop_lib.dll', 'payload/agent-core.exe')) {
                $file = Join-Path $context $name
                if (Test-Path -LiteralPath $file -PathType Leaf) { Remove-Item -LiteralPath $file -Force }
            }
            [IO.Directory]::Delete((Join-Path $context 'payload'), $false)
            [IO.Directory]::Delete($context, $false)
        } catch {
            Write-Warning ('Temporary update context retained: ' + $_.Exception.Message)
        }
    }
}
exit $result
