# Ledger Room: prepare a Windows host with an Intel Arc Pro B70 for the Stage II measurement.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_host.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup_host.ps1 -ModelsRoot D:\models -SkipModels
#
# Needs Python 3.10+ (3.12 used on the laptop) and the Intel Arc graphics driver from intel.com.
# Not yet run on the contest host (its OS was unknown when this was written).
# Then: .venv\Scripts\python -m eval.bench_b70   and   .venv\Scripts\python -m eval.offline_check
param(
    [string]$ModelsRoot = $(if ($env:LEDGER_MODELS_ROOT) { $env:LEDGER_MODELS_ROOT } else { Join-Path $HOME "models" }),
    [switch]$SkipModels,
    [switch]$KeepTelemetry
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path requirements.txt)) { throw "run from the ledger-room repository" }
function Step($t) { Write-Host "`n== $t" }
function Run($exe, [string[]]$argv) { & $exe @argv; if ($LASTEXITCODE -ne 0) { throw "$exe $argv failed ($LASTEXITCODE)" } }

Step "1/7 GPU and driver"
Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, AdapterRAM | Format-Table -AutoSize
if (-not (Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match "Intel" -and $_.Name -match "Arc" })) {
    Write-Warning "No Intel Arc adapter listed. Install the Intel Arc Pro graphics driver from intel.com first."
}

Step "2/7 Python"
$py = $null
foreach ($v in @("3.12", "3.13", "3.11")) {
    try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-$v"); break } } catch {}
}
if (-not $py) {
    try { & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; if ($LASTEXITCODE -eq 0) { $py = @("python") } } catch {}
}
if (-not $py) { throw "need Python 3.10+ (winget install Python.Python.3.12)" }
Write-Host "using $($py -join ' ')"

Step "3/7 virtual environment and packages"
$vpy = ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    $pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1) + @("-m", "venv", ".venv")
    Run $pyExe $pyArgs
}
Run $vpy @("-m", "pip", "install", "-q", "--upgrade", "pip")
Run $vpy @("-m", "pip", "install", "-q", "-r", "requirements.txt")

Step "4/7 OpenVINO telemetry off"
# `import openvino` loads openvino.tools.ovc, which sends a usage event to Google Analytics unless the consent file
# says 0 (found 2026-11-02). Ledger Room promises that nothing leaves the machine at run time.
if ($KeepTelemetry) { Write-Host "skipped (-KeepTelemetry)" }
else {
    Run ".venv\Scripts\opt_in_out.exe" @("--opt_out")
    $f = Join-Path $env:LOCALAPPDATA "Intel Corporation\openvino_telemetry"
    Write-Host "consent file: $f = $(if (Test-Path $f) { Get-Content $f } else { 'missing' })"
}

Step "5/7 inventory (OpenVINO devices, memory, disk)"
New-Item -ItemType Directory -Force -Path $ModelsRoot | Out-Null
Run $vpy @("-m", "eval.bench_b70", "--inventory-only", "--models-root", $ModelsRoot)

Step "6/7 models (OpenVINO INT4, pre-converted by Intel; 9B 5.85 GB, 27B 14.87 GB)"
if ($SkipModels) { Write-Host "skipped" }
else {
    $drive = (Get-Item $ModelsRoot).PSDrive
    Write-Host ("free at {0}: {1:N1} GB (need about 25 GB for both)" -f $ModelsRoot, ($drive.Free / 1GB))
    foreach ($repo in @("OpenVINO/Qwen3.5-9B-int4-ov", "OpenVINO/Qwen3.5-27B-int4-ov")) {
        $d = Join-Path $ModelsRoot ($repo -replace "^OpenVINO/", "")
        if (Test-Path (Join-Path $d "openvino_language_model.xml")) { Write-Host "present: $d"; continue }
        Run $vpy @("-m", "ledger_room.get_model", "--repo", $repo, "--dir", $d)   # HF_TOKEN from the environment if needed
    }
}

Step "7/7 tests (no model needed)"
Run $vpy @("-m", "pytest", "-q")

Write-Host @"

Done. Next:
  `$env:LEDGER_MODELS_ROOT = "$ModelsRoot"
  .venv\Scripts\python -m eval.bench_b70          # writes eval\results_b70.json
  .venv\Scripts\python -m eval.offline_check      # writes eval\results_offline.json
"@
