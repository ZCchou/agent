"""Check the offline prerequisite decision without launching a system installer."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(POWERSHELL, "native Windows PowerShell required")
class VcPrerequisiteTests(unittest.TestCase):
    def test_skip_only_verified_equal_or_newer_installed_x64_runtime(self):
        harness = r"""
param([string]$Installer)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$null, [ref]$null)
$definitions = @($ast.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.FunctionDefinitionAst]
} | ForEach-Object { $_.Extent.Text })
. ([ScriptBlock]::Create(($definitions -join [Environment]::NewLine)))

function Get-SafePath([string]$Root, [string]$Relative) { Join-Path $Root $Relative }
function Get-AuthenticodeSignature([string]$LiteralPath) {
    $status = if ($LiteralPath -like '*vc_redist.x64.exe') { $case.signature } else { 'Valid' }
    [pscustomobject]@{ Status = $status; SignerCertificate = @{ Subject = 'O=Microsoft Corporation' } }
}
function Get-Item([string]$LiteralPath) {
    $version = if ($LiteralPath -like '*vc_redist.x64.exe') { $case.file_version } else { '140.0.0.1' }
    [pscustomobject]@{ VersionInfo = @{ FileVersion = $version } }
}
function Get-ItemProperty {
    [CmdletBinding()]
    param([string]$LiteralPath)
    if ($LiteralPath -ne 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64' -or -not $case.runtime) {
        throw 'missing VC runtime registration'
    }
    $case.runtime
}
function Test-Path([string]$LiteralPath) { $LiteralPath -like '*EdgeUpdate*' }
function Get-ItemPropertyValue([string]$LiteralPath, [string]$Name, [string]$ErrorAction) { '140.0.0.1' }
function Start-Process([string]$FilePath, [string[]]$ArgumentList, [string]$WindowStyle, [switch]$Wait, [switch]$PassThru) {
    $script:launch = $FilePath
    [pscustomobject]@{ ExitCode = 0 }
}
$manifest = [pscustomobject]@{ prerequisites = @{
    'vc_redist.x64.exe' = @{ file_version = '14.44.35211.0' }
    'MicrosoftEdgeWebView2RuntimeInstallerX64.exe' = @{ file_version = '140.0.0.1' }
}}
$results = foreach ($case in ([Console]::In.ReadToEnd() | ConvertFrom-Json)) {
    $script:launch = ''
    $message = ''
    try { $null = Install-Prerequisites $env:TEMP $manifest }
    catch { $message = $_.Exception.Message }
    [pscustomobject]@{ launched = ($script:launch -like '*vc_redist.x64.exe'); error = $message }
}
ConvertTo-Json -InputObject @($results) -Compress
"""
        expected = [
            ("same", {"Installed": 1, "Version": "v14.44.35211.00"}, "Valid", "14.44.35211.0", False, ""),
            ("newer numeric", {"Installed": 1, "Version": "14.100.1.0"}, "Valid", "14.44.35211.0", False, ""),
            ("older", {"Installed": 1, "Version": "v14.44.35210.99"}, "Valid", "14.44.35211.0", True, ""),
            ("missing", None, "Valid", "14.44.35211.0", True, ""),
            ("not installed", {"Installed": 0, "Version": "v99.9.9.9"}, "Valid", "14.44.35211.0", True, ""),
            ("no installed flag", {"Version": "v99.9.9.9"}, "Valid", "14.44.35211.0", True, ""),
            ("invalid version", {"Installed": 1, "Version": "v99.9.9.9-extra"}, "Valid", "14.44.35211.0", True, ""),
            ("integer overflow", {"Installed": 1, "Version": "v99999999999999.0.0.0"}, "Valid", "14.44.35211.0", True, ""),
            ("bad packaged signature", {"Installed": 1, "Version": "v14.44.35211.0"}, "NotSigned", "14.44.35211.0",
             False, "Microsoft prerequisite signature invalid"),
            ("bad packaged version", {"Installed": 1, "Version": "v14.44.35211.0"}, "Valid", "14.44.35210.0",
             False, "Microsoft prerequisite file version differs"),
        ]
        scenarios = [{"runtime": runtime, "signature": signature, "file_version": file_version}
                     for _, runtime, signature, file_version, _, _ in expected]
        with tempfile.TemporaryDirectory(prefix="agent-vc-prerequisite-") as directory:
            helper = Path(directory) / "prerequisite-harness.ps1"
            helper.write_text(harness, encoding="utf-8")
            result = subprocess.run(
                [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(helper),
                 str(ROOT / "installer/install-offline-runtime.ps1")],
                input=json.dumps(scenarios), text=True, capture_output=True, timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(len(actual), len(expected))
        for case, observed in zip(expected, actual, strict=True):
            with self.subTest(case=case[0]):
                self.assertEqual(observed["launched"], case[4], observed)
                if case[5]:
                    self.assertIn(case[5], observed["error"])
                else:
                    self.assertEqual(observed["error"], "")
