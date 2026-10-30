# Regenerate every derived artefact after a production run, in order, stopping at the first failure.
#   .\scripts\finalize.ps1            # after: python -m kavach run --all --backend tg --persist
# Steps: organizer grader + our validator, attestation re-run, SAR filings (+ verify), containment plans (+ verify),
# viewer data, README results tables, judge-facing docs copied into round2/.
$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path $PSScriptRoot -Parent)
$py = ".venv\Scripts\python.exe"

function Step($title, [string[]]$cmd) {
    Write-Host ""
    Write-Host "== $title" -ForegroundColor Cyan
    & $py @cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $title (exit $LASTEXITCODE)" -ForegroundColor Red; exit $LASTEXITCODE }
}

Step "validator + organizer grader"   @("-m", "kavach", "check", "--grader")
Step "attestation re-run"             @("-m", "kavach", "attest", "--all")
Step "SAR filings"                    @("-m", "kavach.filing", "--all")
Step "SAR filings verify"             @("-m", "kavach.filing", "verify", "--all")
Step "containment plans"              @("-m", "kavach.containment", "all")
Step "containment verify"             @("-m", "kavach.containment", "--verify")
Step "graph write-back verify"        @("-m", "kavach", "writeback", "verify")
Step "viewer data"                    @("-m", "kavach", "site")
Step "README tables"                  @("-m", "kavach", "report")
Step "publish judge-facing docs"      @("-m", "kavach", "publish")
Write-Host ""
Write-Host "ALL STEPS PASSED" -ForegroundColor Green
