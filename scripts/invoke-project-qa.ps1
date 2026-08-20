[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadPath,

    [string]$ApiUrl = $env:CASCADE_QA_API_URL,
    [string]$ApiKey = $env:CASCADE_QA_API_KEY,
    [string]$ResultPath = "",
    [string]$ProjectRoot = ".",
    [switch]$ApplyProposals,
    [switch]$FailOnGate
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ApiUrl)) {
    $ApiUrl = "http://localhost:8040"
}

$payloadFile = (Resolve-Path -LiteralPath $PayloadPath).Path
$payload = Get-Content -LiteralPath $payloadFile -Raw
$null = $payload | ConvertFrom-Json
$headers = @{ "Content-Type" = "application/json" }
if (-not [string]::IsNullOrWhiteSpace($ApiKey)) {
    $headers["Authorization"] = "Bearer $ApiKey"
}

$endpoint = $ApiUrl.TrimEnd("/") + "/evaluations"
Write-Host "Submitting project QA evidence to $endpoint"
$result = Invoke-RestMethod -Method Post -Uri $endpoint -Headers $headers -Body $payload

Write-Host "QA status: $($result.status)"
Write-Host "Summary: $($result.summary)"
Write-Host "Findings: $($result.stats.findings); proposed changes: $($result.stats.proposed_changes); gate passed: $($result.quality_gate.passed)"

if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
    $resultJson = $result | ConvertTo-Json -Depth 100
    Set-Content -LiteralPath $ResultPath -Value $resultJson -Encoding utf8
    Write-Host "Saved QA result to $ResultPath"
}

if ($ApplyProposals) {
    $root = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ProjectRoot).Path)
    $rootPrefix = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    foreach ($change in @($result.proposed_changes)) {
        $target = [System.IO.Path]::GetFullPath((Join-Path $root $change.path))
        if (-not $target.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing proposal outside project root: $($change.path)"
        }
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "Proposal target does not exist: $target"
        }
        $content = Get-Content -LiteralPath $target -Raw
        $matches = ([regex]::Matches($content, [regex]::Escape([string]$change.original))).Count
        if ($matches -ne 1) {
            throw "Refusing proposal for $($change.path): expected exactly one source match, found $matches"
        }
        $updated = $content.Replace([string]$change.original, [string]$change.replacement)
        [System.IO.File]::WriteAllText($target, $updated)
        Write-Host "Applied documented proposal $($change.rule_id) to $($change.path)"
    }
}

if ($FailOnGate -and -not [bool]$result.quality_gate.passed) {
    Write-Error "Project QA quality gate failed: $($result.quality_gate.reason)"
    exit 2
}

$result
