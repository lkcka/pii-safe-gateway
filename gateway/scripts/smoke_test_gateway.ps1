# Smoke test: send sample.docx to Gateway /v1/chat/completions and verify PII markers.
param(
    [string]$GatewayUrl = "http://localhost:8000",
    [string]$Model = "gpt-4o-mini",
    [string]$SampleB64Path = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $SampleB64Path) {
    $SampleB64Path = Join-Path $scriptDir "sample.docx.b64.txt"
}

if (-not (Test-Path $SampleB64Path)) {
    Write-Error "Base64 file not found: $SampleB64Path. Run: python scripts/create_sample_docx.py"
}

$b64 = (Get-Content -Path $SampleB64Path -Raw).Trim()

$body = @{
    model    = $Model
    stream   = $false
    messages = @(
        @{
            role    = "user"
            content = "Выведи дословно содержимое приложенного документа, включая таблицу."
        }
    )
    files = @(
        @{
            filename       = "sample.docx"
            content_base64 = $b64
        }
    )
} | ConvertTo-Json -Depth 10

Write-Host "Sending request to $GatewayUrl/v1/chat/completions ..."

$response = Invoke-RestMethod `
    -Uri "$GatewayUrl/v1/chat/completions" `
    -Method Post `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($body))

$responseJson = $response | ConvertTo-Json -Depth 20
Write-Host "`n--- Response ---"
Write-Host $responseJson

$markers = @("[PERSON]", "[PHONE]", "[EMAIL]")
$found = @()
foreach ($m in $markers) {
    if ($responseJson -match [regex]::Escape($m)) {
        $found += $m
    }
}

Write-Host "`n--- Marker check ---"
if ($found.Count -ge 2) {
    Write-Host "OK: found markers: $($found -join ', ')"
} else {
    Write-Warning "Expected at least 2 of [PERSON]/[PHONE]/[EMAIL] in response. Found: $($found -join ', ')"
    Write-Host "Also check gateway logs: docker compose logs --tail=50 gateway"
}
