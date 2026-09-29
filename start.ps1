param(
    [switch]$PreviewOnly,
    [switch]$OBS,
    [ValidateSet('kimi', 'minimax')][string]$ModelProfile = 'kimi',
    [ValidateSet('local', 'supergrid')][string]$Runtime = 'local'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot '.runtime\uv-cache'
$directorPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $directorPython)) {
    uv sync --python 3.12 --extra flower --extra dev
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
Write-Host 'Noesis control room: http://127.0.0.1:8765'
if ($PreviewOnly) {
    & $directorPython -m noesis
} else {
    $directorArgs = @('scripts/run_demo.py', '--runtime', $Runtime)
    if ($OBS) { $directorArgs += '--obs' }
    if ($ModelProfile) { $directorArgs += @('--model-profile', $ModelProfile) }
    & $directorPython @directorArgs
}
