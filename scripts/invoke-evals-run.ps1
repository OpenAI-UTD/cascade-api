[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadPath,

    [string]$ApiUrl = $env:CASCADE_EVALS_API_URL,
    [string]$ApiKey = $env:CASCADE_EVALS_API_KEY,
    [string]$ResultPath = "",
    [switch]$Wait,
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ApiUrl)) {
    $ApiUrl = "http://localhost:8041"
}

$payloadFile = (Resolve-Path -LiteralPath $PayloadPath).Path
$payload = Get-Content -LiteralPath $payloadFile -Raw
$null = $payload | ConvertFrom-Json
$headers = @{ "Content-Type" = "application/json" }
if (-not [string]::IsNullOrWhiteSpace($ApiKey)) {
    $headers["Authorization"] = "Bearer $ApiKey"
}

$endpoint = $ApiUrl.TrimEnd("/") + "/runs"
Write-Host "Submitting eval run to $endpoint"
$accepted = Invoke-RestMethod -Method Post -Uri $endpoint -Headers $headers -Body $payload
Write-Host "Run accepted: $($accepted.run_id) ($($accepted.status))"

$result = $accepted

if ($Wait) {
    $statusEndpoint = $ApiUrl.TrimEnd("/") + "/runs/" + $accepted.run_id
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $result = Invoke-RestMethod -Method Get -Uri $statusEndpoint -Headers $headers
        if ($result.status -in @("completed", "failed")) { break }
    }
    Write-Host "Evals status: $($result.status)"
    Write-Host "Aggregate score: $($result.aggregate_score)"
}

if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
    $resultJson = $result | ConvertTo-Json -Depth 100
    Set-Content -LiteralPath $ResultPath -Value $resultJson -Encoding utf8
    Write-Host "Saved eval run result to $ResultPath"
}

if ($result.status -eq "failed") {
    Write-Error "Eval run failed: $($result.error)"
    exit 2
}

$result
