# One command for the drills: investigate a single case from a clean clone.
#   .\run_case.ps1 HHF-003                 # DuckDB lane (needs data/ built by the setup below)
#   .\run_case.ps1 HHF-003 -Backend tg     # TigerGraph lane (needs TG_HOST and TG_SECRET in .env)
# First run on a clean clone performs the setup: venv, requirements, organizer downloads, index (about 10 minutes).
# Exit codes: 0 answer written, 2 the investigation failed closed (see incidents/), 1 setup error.
param(
    [Parameter(Mandatory = $true)][string]$CaseId,
    [ValidateSet("duck", "tg", "mcp")][string]$Backend = "duck",
    [switch]$NoLlm,
    [switch]$NoWrite
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "setup: creating .venv and installing requirements"
    python -m venv .venv
    & $py -m pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit 1 }
}
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "setup: created .env from .env.example (LLM key optional; prose falls back to templates)" }
if (-not (Test-Path "data\kavach_r2.duckdb")) {
    Write-Host "setup: downloading organizer files and building the index"
    & $py -m kavach download --round 1; if ($LASTEXITCODE -ne 0) { exit 1 }
    & $py -m kavach download --round 2; if ($LASTEXITCODE -ne 0) { exit 1 }
    & $py -m kavach index; if ($LASTEXITCODE -ne 0) { exit 1 }
}
$argsList = @("-m", "kavach", "investigate", $CaseId, "--backend", $Backend)
if ($NoLlm) { $argsList += "--no-llm" }
if ($NoWrite) { $argsList += "--no-write" }
& $py @argsList
exit $LASTEXITCODE
