param(
    [switch]$SkipFrontend,
    [switch]$SkipDocker,
    [switch]$SkipPython,
    [switch]$SkipDockerBuild,
    [switch]$ContinueOnFailure
)

$ErrorActionPreference = "Continue"
$Results = New-Object System.Collections.Generic.List[object]

function Invoke-CiStep {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host ""
    Write-Host "============================================================"
    Write-Host $Name
    Write-Host "============================================================"
    $started = Get-Date
    $global:LASTEXITCODE = 0
    try {
        & $Command
        $exitCode = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } else { 0 }
    } catch {
        Write-Host "ERROR: $($_.Exception.Message)"
        $exitCode = 1
    }
    $status = if ($exitCode -eq 0) { "PASS" } else { "FAIL" }
    $script:Results.Add([pscustomobject]@{
        Step = $Name
        Status = $status
        ExitCode = $exitCode
        Seconds = [int]((Get-Date) - $started).TotalSeconds
    }) | Out-Null
    if ($exitCode -ne 0 -and -not $ContinueOnFailure) {
        throw "$Name failed with exit code $exitCode"
    }
}

try {
    Invoke-CiStep "Secret audit" {
        $psExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
        & $psExe -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\audit-secrets.ps1"
    }

    if (-not $SkipPython) {
        Invoke-CiStep "Python compile" {
            python -m compileall services tests
        }
        Invoke-CiStep "Python tests" {
            python -m pytest tests
        }
        Invoke-CiStep "Ruff" {
            python -m ruff check services tests
        }
    }

    Invoke-CiStep "PowerShell parser checks" {
        $failed = $false
        Get-ChildItem scripts -Filter *.ps1 -Recurse | ForEach-Object {
            $tokens = $null
            $errors = $null
            [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null
            if ($errors.Count -gt 0) {
                $failed = $true
                Write-Host "PowerShell parse errors in $($_.FullName)"
                $errors | ForEach-Object { Write-Host $_.Message }
            }
        }
        if ($failed) { throw "PowerShell parser checks failed" }
    }

    if (-not $SkipFrontend) {
        Invoke-CiStep "Frontend install" {
            Push-Location web\command-center
            try {
                if (Test-Path package-lock.json) { npm ci } else { npm install }
            } finally {
                Pop-Location
            }
        }
        Invoke-CiStep "Frontend typecheck" {
            Push-Location web\command-center
            try { npm run typecheck } finally { Pop-Location }
        }
        Invoke-CiStep "Frontend build" {
            Push-Location web\command-center
            try { npm run build } finally { Pop-Location }
        }
    }

    Invoke-CiStep "Kubernetes YAML parse validation" {
        python -m pip install pyyaml
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        python scripts/validate-k8s-manifests.py
    }

    Invoke-CiStep "Kustomize and kubectl client validation" {
        kubectl kustomize infra/kubernetes >$null
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        kubectl kustomize infra/kubernetes/base >$null
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        kubectl kustomize infra/kubernetes/overlays/low-resource >$null
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        kubectl kustomize targets/sock-shop >$null
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        kubectl cluster-info >$null 2>&1
        if ($LASTEXITCODE -eq 0) {
            kubectl apply --dry-run=client -f infra/kubernetes/network-policies
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            kubectl apply --dry-run=client -k targets/sock-shop
        } else {
            Write-Host "Skipping kubectl apply dry-runs: no Kubernetes API context is available."
            Write-Host "Offline YAML validation and kubectl kustomize render checks were completed."
            $global:LASTEXITCODE = 0
        }
    }

    if (-not $SkipDocker -and -not $SkipDockerBuild) {
        Invoke-CiStep "Docker build smoke" {
            docker build -f services/command-center-api/Dockerfile -t cascade-ci/command-center-api:test .
            docker build -f web/command-center/Dockerfile -t cascade-ci/command-center:test .
            docker build -f services/retrieval-service/Dockerfile -t cascade-ci/retrieval-service:test .
            docker build -f services/agent-orchestrator-service/Dockerfile -t cascade-ci/agent-orchestrator-service:test .
            docker build -f services/remediation-recommender-service/Dockerfile -t cascade-ci/remediation-recommender-service:test .
            docker build -f services/project-qa-service/Dockerfile -t cascade-ci/project-qa-service:test .
        }
    } else {
        $Results.Add([pscustomobject]@{ Step = "Docker build smoke"; Status = "SKIP"; ExitCode = 0; Seconds = 0 }) | Out-Null
    }
} catch {
    Write-Host "ERROR: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "============================================================"
Write-Host "Local CI Summary"
Write-Host "============================================================"
$Results | Format-Table -AutoSize

$failedSteps = @($Results | Where-Object { $_.Status -eq "FAIL" })
if ($failedSteps.Count -gt 0) {
    Write-Host "LOCAL CI: FAIL"
    exit 1
}

Write-Host "LOCAL CI: PASS"
exit 0
