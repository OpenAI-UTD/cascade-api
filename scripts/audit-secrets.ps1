param(
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$keywords = @(
    "api_key", "apikey", "secret", "password", "passwd", "token", "bearer", "authorization",
    "private_key", "client_secret", "DATABASE_URL", "CLICKHOUSE_PASSWORD", "QDRANT_API_KEY",
    "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "kubeconfig",
    "BEGIN RSA PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY", "BEGIN PRIVATE KEY", "client-key-data",
    "client-certificate-data", "service_account", "refresh_token", "id_token", "access_token"
)

$safeValues = @("", "none", "null", "false", "true", "changeme", "change-me", "placeholder", "example", "demo", "demo-only-not-secret", "replace-me", "replace-with-local-key-if-needed", "<redacted>", "<placeholder>", "your-value-here")
$findings = New-Object System.Collections.Generic.List[object]
$tracked = @(& git ls-files)

function Test-SafeSecretValue {
    param([string]$Value)
    $normalized = $Value.Trim().Trim('"', "'").ToLowerInvariant()
    return ($safeValues -contains $normalized -or $Value -match '^\$\{?[A-Z0-9_]+\}?$')
}

function Test-DynamicCodeValue {
    param([string]$Line, [string]$Value)
    $trimmed = $Value.Trim().Trim('"', "'", '`')
    if ($Line -match '\$\{') { return $true }
    if ($trimmed -match '^(settings|self|payload|request|headers|metadata|approval|auth|token|key|digest|configured|configured_plain|configured_hashes|configuredApiToken|import\.meta|os\.getenv|_split|hashlib|hmac|json|base64|str|bool|int|len|any|all|dict|list|set|Path|api_key|api_url|access_token|refresh_token|id_token|client_secret|private_key)\b') { return $true }
    if ($trimmed -match '^[A-Za-z_][A-Za-z0-9_]*\(') { return $true }
    if ($trimmed -match '^[frbuFRBU]*["'']') { return $true }
    if ($Line -match '(?i)\b(api_keys|api_key_hashes|auth_header|signing_secret)\s*=\s*settings\.') { return $true }
    return $false
}

function Test-AllowedFixtureFinding {
    param([string]$File, [string]$Reason)
    $normalized = $File -replace '\\', '/'
    if ($normalized -match '^(tests|docs)/') { return $true }
    if ($normalized -match '^targets/sock-shop/') { return $true }
    if ($normalized -eq 'scripts/audit-secrets.ps1' -and $Reason -in @("non-placeholder assignment", "jwt-like token", "credential in URL", "kubeconfig credential data")) { return $true }
    return $false
}

foreach ($file in $tracked) {
    if (-not (Test-Path $file)) { continue }
    $lines = @(Get-Content -LiteralPath $file -ErrorAction SilentlyContinue)
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $lineNumber = $i + 1
        foreach ($keyword in $keywords) {
            if ($line -notmatch [regex]::Escape($keyword)) { continue }

            $highConfidence = $false
            $reason = "keyword"
            $assignmentPattern = '(?i)^\s*["'']?([A-Z0-9_.-]*(api[_-]?key|apikey|secret|password|passwd|token|authorization|private[_-]?key|client[_-]?secret|database_url|clickhouse_password|qdrant_api_key|openai_api_key|aws_access_key_id|aws_secret_access_key|service_account)[A-Z0-9_.-]*)["'']?\s*=\s*["'']?([^"'',\s#]+)'
            $match = [regex]::Match($line, $assignmentPattern)
            if ($match.Success) {
                $keyName = $match.Groups[1].Value.Trim()
                $value = $match.Groups[3].Value.Trim()
                $keyLooksCredentialLike = ($keyName -match 'api|secret|password|passwd|authorization|private|client|database|aws|openai|qdrant|clickhouse|service_account')
                $fileAllowsCodeIdentifiers = $file -match 'package-lock\.json$|deterministic\.py$'
                $dynamicCodeValue = Test-DynamicCodeValue $line $value
                if ($keyLooksCredentialLike -and -not $fileAllowsCodeIdentifiers -and -not $dynamicCodeValue -and -not (Test-SafeSecretValue $value)) {
                    $highConfidence = $true
                    $reason = "non-placeholder assignment"
                } elseif ($dynamicCodeValue) {
                    $reason = "dynamic code assignment"
                } else {
                    $reason = "placeholder assignment"
                }
            }
            $yamlEnvNamePattern = '(?i)^\s*-\s*name:\s*["'']?([A-Z0-9_.-]*(api[_-]?key|apikey|secret|password|passwd|token|authorization|private[_-]?key|client[_-]?secret|database_url|clickhouse_password|qdrant_api_key|openai_api_key|aws_access_key_id|aws_secret_access_key|service_account)[A-Z0-9_.-]*)["'']?\s*$'
            $yamlMatch = [regex]::Match($line, $yamlEnvNamePattern)
            if ($yamlMatch.Success -and ($i + 1) -lt $lines.Count) {
                $valueMatch = [regex]::Match($lines[$i + 1], '^\s*value:\s*["'']?([^"'']*)["'']?\s*(#.*)?$')
                if ($valueMatch.Success) {
                    $value = $valueMatch.Groups[1].Value.Trim()
                    if (-not (Test-SafeSecretValue $value)) {
                        $highConfidence = $true
                        $reason = "non-placeholder Kubernetes env value"
                    } else {
                        $reason = "placeholder Kubernetes env value"
                    }
                }
            }
            if ($line -match "-----BEGIN (RSA|OPENSSH|DSA|EC) PRIVATE KEY-----") {
                $highConfidence = $true
                $reason = "private key block"
            }
            if ($line -match "-----BEGIN [A-Z ]*PRIVATE KEY-----") {
                $highConfidence = $true
                $reason = "private key block"
            }
            if ($line -match '(?i)\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b') {
                $highConfidence = $true
                $reason = "jwt-like token"
            }
            if ($line -match '(?i)[a-z][a-z0-9+.-]*://[^:/\s]+:[^@\s]+@') {
                $highConfidence = $true
                $reason = "credential in URL"
            }
            if ($line -match '(?i)client-(key|certificate)-data:\s*[A-Za-z0-9+/=]{40,}') {
                $highConfidence = $true
                $reason = "kubeconfig credential data"
            }

            if ($highConfidence -and (Test-AllowedFixtureFinding $file $reason)) {
                $highConfidence = $false
                $reason = "documented/test/demo fixture"
            }

            $findings.Add([pscustomobject]@{
                File = $file
                Line = $lineNumber
                Key = $keyword
                Severity = if ($highConfidence) { "high" } else { "info" }
                Reason = $reason
            }) | Out-Null
        }
    }
}

if ($Json) {
    $findings | ConvertTo-Json -Depth 4
} else {
    if ($findings.Count -eq 0) {
        Write-Host "PASS: no suspicious secret keywords found in tracked files"
    } else {
        Write-Host "Secret hygiene scan findings (values redacted by design):"
        $findings | Sort-Object Severity, File, Line | Format-Table File, Line, Key, Severity, Reason -AutoSize
    }
}

$high = @($findings | Where-Object { $_.Severity -eq "high" })
if ($high.Count -gt 0) {
    Write-Host "FAIL: high-confidence secret-like assignments found. Remove real values or replace with placeholders before publishing."
    exit 1
}

Write-Host "PASS: no high-confidence secrets found in tracked files"
exit 0
