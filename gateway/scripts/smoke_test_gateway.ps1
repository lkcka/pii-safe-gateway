# Smoke test: verify DOCX extraction + PII anonymization (no external LLM required).
param(
    [string]$GatewayUrl = "http://localhost:8000",
    [string]$Model = "openai/gpt-4o-mini",
    [string]$SampleB64Path = "",
    [switch]$SkipGatewayCall
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..\..")

function Invoke-VerifyExtraction {
    param([string]$FilterUrl = "http://localhost:8001")

    Write-Host "`n=== Step 1: verify extraction + anonymization (no external LLM) ==="
    $env:FILTER_SERVICE_URL = $FilterUrl
    & python (Join-Path $scriptDir "verify_sample_docx.py")
    if ($LASTEXITCODE -ne 0) {
        throw "verify_sample_docx.py failed (exit $LASTEXITCODE)"
    }
}

function Get-GatewayAppliedCount {
    # stderr from docker compose (version warning) must not fail the script
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $logs = docker compose -f (Join-Path $repoRoot "docker-compose.yml") logs --tail=30 gateway 2>&1
    $ErrorActionPreference = $prevEap
    $match = $logs | Select-String -Pattern "File processed: sample\.docx.*applied=(\d+)" | Select-Object -Last 1
    if ($match -and $match.Matches.Count -gt 0) {
        return [int]$match.Matches[0].Groups[1].Value
    }
    return -1
}

if (-not $SampleB64Path) {
    $SampleB64Path = Join-Path $scriptDir "sample.docx.b64.txt"
}
if (-not (Test-Path $SampleB64Path)) {
    Write-Error "Base64 file not found: $SampleB64Path. Run: docker compose exec gateway python scripts/create_sample_docx.py"
}

# Prefer in-container verify (uses docker network to filter-service)
Write-Host "Running verify inside gateway container..."
docker compose -f (Join-Path $repoRoot "docker-compose.yml") exec -T gateway python scripts/verify_sample_docx.py
if ($LASTEXITCODE -ne 0) {
    Write-Warning "In-container verify failed. Retrying once (Filter Service can be non-deterministic on CPU)..."
    docker compose -f (Join-Path $repoRoot "docker-compose.yml") exec -T gateway python scripts/verify_sample_docx.py
    if ($LASTEXITCODE -ne 0) {
        throw "verify_sample_docx.py failed after retry"
    }
}

if ($SkipGatewayCall) {
    Write-Host "`nOK: extraction + anonymization verified (gateway call skipped)."
    exit 0
}

Write-Host "`n=== Step 2: optional gateway /v1/chat/completions integration ==="

$b64 = (Get-Content -Path $SampleB64Path -Raw -Encoding UTF8).Trim()

$bodyObj = [ordered]@{
    model    = $Model
    stream   = $false
    messages = @(
        [ordered]@{
            role    = "user"
            content = "Выведи дословно содержимое приложенного документа, включая таблицу."
        }
    )
    files = @(
        [ordered]@{
            filename       = "sample.docx"
            content_base64 = $b64
        }
    )
}

$body = $bodyObj | ConvertTo-Json -Depth 10 -Compress
$utf8Body = [System.Text.Encoding]::UTF8.GetBytes($body)

Write-Host "Sending request to $GatewayUrl/v1/chat/completions ..."
$response = Invoke-RestMethod `
    -Uri "$GatewayUrl/v1/chat/completions" `
    -Method Post `
    -ContentType "application/json; charset=utf-8" `
    -Body $utf8Body

$content = $response.choices[0].message.content
Write-Host "`n--- Assistant content (first 500 chars) ---"
if ($content.Length -gt 500) {
    Write-Host $content.Substring(0, 500)
    Write-Host "..."
} else {
    Write-Host $content
}

Write-Host "`n=== Step 3: check gateway logs (primary verification) ==="
Start-Sleep -Seconds 1
$applied = Get-GatewayAppliedCount
if ($applied -ge 2) {
    Write-Host "OK: gateway logs show applied=$applied for sample.docx"
} elseif ($applied -ge 0) {
    Write-Warning "Gateway logs show applied=$applied (expected >= 2). Filter may need retry."
    Write-Host "Check: docker compose logs --tail=50 gateway"
    exit 1
} else {
    Write-Warning "Could not parse applied= from gateway logs."
    Write-Host "Check manually: docker compose logs --tail=50 gateway"
    exit 1
}

Write-Host "`nNOTE: LLM response may not echo [PERSON]/[PHONE]/[EMAIL] markers."
Write-Host "Markers are injected into messages sent upstream; logs applied>=2 is the reliable check."
