param(
    [ValidateSet('cpu', 'cuda')][string]$Device = 'cpu',
    [string]$Python = 'python',
    [string]$OutputDirectory = '',
    [string]$EnvironmentRoot = '',
    [switch]$ReuseEnvironment
)
$ErrorActionPreference = 'Stop'
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = Join-Path $repository ('.artifacts/rag-runtime-build/' + $Device)
if (-not $EnvironmentRoot) {
    $EnvironmentRoot = Join-Path $buildRoot 'venv'
}
$EnvironmentRoot = [IO.Path]::GetFullPath($EnvironmentRoot)
$buildPython = Join-Path $environmentRoot 'Scripts/python.exe'
$lock = Join-Path $repository ('services/agent-core/requirements-rag-' + $Device + '.lock')
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repository ('.artifacts/rag-runtime-' + $Device + '/rag-runtime')
}
$destination = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $destination) {
    throw "Runtime destination already exists. Select a new output directory: $destination"
}
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
if (-not (Test-Path -LiteralPath $buildPython)) {
    & $Python -m venv $environmentRoot
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the isolated inference build environment' }
}
& $buildPython -c "import sys,struct; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8, 'CPython 3.12 x64 is required'"
if ($LASTEXITCODE -ne 0) { throw 'Unsupported inference build interpreter' }
if (-not $ReuseEnvironment) {
    & $buildPython -m pip install --cache-dir (Join-Path $buildRoot 'pip-cache') --only-binary=:all: -r $lock
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install fixed inference dependencies' }
}
& $buildPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Inference dependency check failed' }
$verifyPins = @'
from importlib import metadata
from pathlib import Path
import sys
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if "==" not in raw or raw.startswith("#"):
        continue
    name, expected = raw.strip().split("==", 1)
    actual = metadata.version(name)
    if actual != expected:
        raise SystemExit(f"Fixed inference dependency mismatch: {name} expected {expected}, got {actual}")
'@
& $buildPython -c $verifyPins $lock
if ($LASTEXITCODE -ne 0) { throw 'Inference environment differs from the fixed lock' }
$runRoot = Join-Path $buildRoot ([Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
$source = Join-Path $repository 'services/agent-core/src'
$entry = Join-Path $source 'agent/rag/worker.py'
$arguments = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
    '--name', 'rag-worker', '--paths', $source,
    '--distpath', (Join-Path $runRoot 'dist'), '--workpath', (Join-Path $runRoot 'work'),
    '--specpath', $runRoot,
    '--collect-all', 'transformers', '--collect-all', 'torch',
    '--collect-all', 'onnxruntime', '--collect-all', 'onnx',
    '--collect-all', 'tokenizers', '--collect-all', 'safetensors', '--collect-all', 'sentencepiece',
    '--exclude-module', 'tensorflow', '--exclude-module', 'jax',
    '--exclude-module', 'torchvision', '--exclude-module', 'torchaudio',
    '--exclude-module', 'IPython', '--exclude-module', 'pytest',
    '--exclude-module', 'tensorboard', '--exclude-module', 'tensorrt', $entry
)
$previousPyInstallerCache = $env:PYINSTALLER_CONFIG_DIR
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $buildRoot 'pyinstaller-cache'
    $env:TEMP = Join-Path $buildRoot 'temp'
    $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
    & $buildPython @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Inference runtime bundle build failed' }
} finally {
    $env:PYINSTALLER_CONFIG_DIR = $previousPyInstallerCache
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
}
$bundle = Join-Path $runRoot 'dist/rag-worker'
if (-not (Test-Path -LiteralPath (Join-Path $bundle 'rag-worker.exe'))) {
    throw 'Inference build did not produce rag-worker.exe'
}
New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($destination)) | Out-Null
Copy-Item -LiteralPath $bundle -Destination $destination -Recurse
& $buildPython (Join-Path $PSScriptRoot 'rag_runtime_assets.py') $destination --cuda-license (Join-Path $repository '.artifacts/local-runtime-work/engines/LICENSE-NVIDIA-CUDA.html')
if ($LASTEXITCODE -ne 0) { throw 'Inference runtime asset pruning or license inventory failed' }
$hashes = [ordered]@{}
Get-ChildItem -LiteralPath $destination -File -Recurse | Sort-Object FullName | ForEach-Object {
    $relative = [IO.Path]::GetRelativePath($destination, $_.FullName).Replace('\', '/')
    $hashes[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
$manifest = [ordered]@{
    schema_version = 1
    protocol_version = 1
    executable = 'rag-worker.exe'
    device_bundle = $Device
    python = '3.12'
    requirements_sha256 = (Get-FileHash -LiteralPath $lock -Algorithm SHA256).Hash.ToLowerInvariant()
    files = $hashes
}
$identityCode = 'import json,sys; from pathlib import Path; sys.path.insert(0,str(Path(sys.argv[1])/"tools")); from rag_runtime_bundle import build_identity; print(json.dumps(build_identity(Path(sys.argv[1]),sys.argv[2])))'
$identityJson = & $buildPython -c $identityCode $repository $Device
if ($LASTEXITCODE -ne 0) { throw 'Failed to record runtime source identity' }
$manifest.build_identity = $identityJson | ConvertFrom-Json
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $destination 'runtime.json') -Encoding utf8
Write-Output "Built inference runtime at $destination"
Write-Output 'Ship the entire rag-runtime folder beside agent-core.exe as one trusted application resource.'
