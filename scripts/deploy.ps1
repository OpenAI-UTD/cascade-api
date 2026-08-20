param(
    [string]$ClusterName = "cascade",
    [string]$Namespace = "cascade-system",
    [switch]$SkipRemediationDeploy,
    [switch]$SkipNpmInstall
)

$ErrorActionPreference = "Stop"
$PortForwards = New-Object System.Collections.Generic.List[System.Diagnostics.Process]

function Write-Section { param([string]$Title) Write-Host ""; Write-Host "============================================================"; Write-Host $Title; Write-Host "============================================================" }
function Assert-NativeSuccess { param([string]$Description) if ($LASTEXITCODE -ne 0) { throw "$Description failed with exit code $LASTEXITCODE" } }
function Invoke-Checked { param([string]$Description, [scriptblock]$Command) Write-Host ""; Write-Host "---- $Description ----"; & $Command; Assert-NativeSuccess $Description }
function Invoke-Kubectl { param([string[]]$Arguments) $output = & kubectl @Arguments 2>&1; [pscustomobject]@{ Output = @($output); Text = ($output -join "`n"); ExitCode = $LASTEXITCODE } }
function Get-KubectlJson { param([string[]]$Arguments) $r = Invoke-Kubectl $Arguments; if ($r.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($r.Text)) { return $null }; try { $r.Text | ConvertFrom-Json } catch { return $null } }
function Get-FreePort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}
function Start-PortForward {
    param([string]$Service, [int]$LocalPort, [int]$RemotePort)
    $p = Start-Process -FilePath kubectl -ArgumentList @("-n", $Namespace, "port-forward", "svc/$Service", "$LocalPort`:$RemotePort") -WindowStyle Hidden -PassThru
    $script:PortForwards.Add($p) | Out-Null
    Start-Sleep -Seconds 3
    if ($p.HasExited) { throw "port-forward for svc/$Service exited early" }
    return $p
}
function Stop-PortForwards {
    foreach ($p in $script:PortForwards) {
        if ($null -ne $p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    }
    $script:PortForwards.Clear()
}
function Test-EndpointReady {
    param([string]$Service)
    for ($i = 1; $i -le 30; $i++) {
        $slices = Get-KubectlJson @("-n", $Namespace, "get", "endpointslice", "-l", "kubernetes.io/service-name=$Service", "-o", "json")
        if ($null -ne $slices -and @($slices.items).Count -ge 1) {
            foreach ($slice in @($slices.items)) {
                foreach ($endpoint in @($slice.endpoints)) {
                    if (@($endpoint.addresses).Count -ge 1 -and (($endpoint.conditions.ready -eq $true) -or ($null -eq $endpoint.conditions.ready))) { return }
                }
            }
        }
        Start-Sleep -Seconds 2
    }
    throw "Service $Service has no ready endpoints"
}
function Invoke-HttpJson {
    param([string]$Url)
    return Invoke-RestMethod -Method GET -Uri $Url -TimeoutSec 20
}
function Invoke-HttpText {
    param([string]$Url)
    return Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 20
}
function Write-CommandCenterDebug {
    Write-Section "Command Center UI Failure Debug"
    kubectl -n $Namespace get deploy,svc,endpoints,pods | Select-String "command-center|project-qa-service"
    kubectl -n $Namespace describe deployment project-qa-service
    kubectl -n $Namespace describe deployment command-center-api
    kubectl -n $Namespace describe deployment command-center
    kubectl -n $Namespace logs deployment/command-center-api --tail=100
    kubectl -n $Namespace logs deployment/command-center --tail=100
}

try {
    Write-Section "Deploy Cascade Command Center UI"
    foreach ($path in @(
        "services/project-qa-service/Dockerfile",
        "services/command-center-api/Dockerfile",
        "web/command-center/Dockerfile",
        "web/command-center/package.json",
        "infra/kubernetes/project-qa-service/deployment.yaml",
        "infra/kubernetes/project-qa-service/service.yaml",
        "infra/kubernetes/command-center-api/deployment.yaml",
        "infra/kubernetes/command-center-api/service.yaml",
        "infra/kubernetes/command-center/deployment.yaml",
        "infra/kubernetes/command-center/service.yaml"
    )) {
        if (-not (Test-Path $path)) { throw "Required Command Center UI file missing: $path" }
    }

    Invoke-Checked "Verify kubectl can reach Kubernetes" { kubectl version --request-timeout=5s | Out-Null }
    Invoke-Checked "Verify Docker is available" { docker info | Out-Null }
    Invoke-Checked "Verify kind is available" { kind version | Out-Null }
    Invoke-Checked "Verify kind cluster '$ClusterName' exists" { $clusters = @(kind get clusters); if ($clusters -notcontains $ClusterName) { throw "kind cluster '$ClusterName' does not exist. Create it or pass -ClusterName." } }
    Invoke-Checked "Use kind context" { kubectl config use-context "kind-$ClusterName" | Out-Null }
    Invoke-Checked "Verify Kubernetes API" { kubectl cluster-info | Out-Null }

    if (-not $SkipRemediationDeploy) {
        Write-Section "Ensure Remediation Baseline"
        & .\scripts\deploy-remediation.ps1 -ClusterName $ClusterName -Namespace $Namespace -SkipChaosDeploy
        Assert-NativeSuccess "Deploy Remediation baseline"
    } else {
        Write-Host "Skipping Remediation deploy by flag; existing Remediation services must already be available."
    }

    Write-Section "Build Command Center Frontend"
    Push-Location web\command-center
    try {
        if (-not $SkipNpmInstall) {
            Invoke-Checked "npm install" { npm install }
        }
        Invoke-Checked "npm run build" { npm run build }
    } finally {
        Pop-Location
    }

    Write-Section "Build Command Center UI Images"
    foreach ($image in @(
        @{ Name = "cascade-project-qa-service:dev"; Dockerfile = "services/project-qa-service/Dockerfile" },
        @{ Name = "cascade-command-center-api:dev"; Dockerfile = "services/command-center-api/Dockerfile" },
        @{ Name = "cascade-command-center:dev"; Dockerfile = "web/command-center/Dockerfile" }
    )) {
        Invoke-Checked "Build $($image.Name)" { docker build -f $image.Dockerfile -t $image.Name . }
    }

    Write-Section "Load Command Center UI Images Into kind"
    foreach ($imageName in @("cascade-project-qa-service:dev", "cascade-command-center-api:dev", "cascade-command-center:dev")) {
        Invoke-Checked "kind load $imageName" { kind load docker-image $imageName --name $ClusterName }
    }

    Write-Section "Apply Command Center UI Manifests"
    foreach ($path in @("infra/kubernetes/project-qa-service", "infra/kubernetes/command-center-api", "infra/kubernetes/command-center")) {
        $manifests = @(Get-ChildItem -Path $path -Filter "*.yaml")
        if ($manifests.Count -eq 0) { throw "No Kubernetes manifests found in $path" }
        Invoke-Checked "Apply $path" { kubectl apply -f $path }
    }

    foreach ($deployment in @("project-qa-service", "command-center-api", "command-center")) {
        Invoke-Checked "Restart $deployment" { kubectl -n $Namespace rollout restart "deployment/$deployment" }
    }

    Write-Section "Wait For Command Center UI Rollouts"
    foreach ($deployment in @("project-qa-service", "command-center-api", "command-center")) {
        Invoke-Checked "Wait for $deployment rollout" { kubectl -n $Namespace rollout status "deployment/$deployment" --timeout=240s }
    }

    Write-Section "Wait For Service Endpoints"
    foreach ($service in @("project-qa-service", "command-center-api", "command-center")) {
        Test-EndpointReady $service
        Write-Host "PASS: svc/$service has endpoints"
    }

    Write-Section "Smoke Test Command Center UI Services"
    $qaPort = Get-FreePort
    Start-PortForward "project-qa-service" $qaPort 8040 | Out-Null
    $qaHealth = Invoke-HttpJson "http://localhost:$qaPort/health"
    if ($qaHealth.status -ne "ok") { throw "project-qa-service /health returned unexpected payload" }
    Write-Host "PASS: project-qa-service /health"
    Stop-PortForwards

    $apiPort = Get-FreePort
    Start-PortForward "command-center-api" $apiPort 8031 | Out-Null
    $apiHealth = Invoke-HttpJson "http://localhost:$apiPort/health"
    if ($apiHealth.status -ne "ok") { throw "command-center-api /health returned unexpected payload" }
    Write-Host "PASS: command-center-api /health"
    Stop-PortForwards

    $uiPort = Get-FreePort
    Start-PortForward "command-center" $uiPort 8030 | Out-Null
    $html = Invoke-HttpText "http://localhost:$uiPort/"
    if ($html.StatusCode -ne 200 -or $html.Content -notmatch '<div id="root"') { throw "command-center / did not return expected HTML" }
    $proxyHealth = Invoke-HttpJson "http://localhost:$uiPort/api/retrieval/health"
    if (-not $proxyHealth.service) { throw "command-center /api/retrieval/health returned unexpected payload" }
    Write-Host "PASS: command-center / and /api/retrieval/health"
    Stop-PortForwards

    Write-Section "Command Center UI Deploy Complete"
    Write-Host "PASS: command-center deployed"
    Write-Host "Local access:"
    Write-Host "kubectl port-forward -n $Namespace svc/command-center 18300:8030"
    Write-Host "Open http://localhost:18300"
    Write-Host ".\scripts\accept.ps1"
} catch {
    Write-Host "ERROR: $($_.Exception.Message)"
    try { Write-CommandCenterDebug } catch { Write-Host "WARN: Failed to collect Command Center UI debug output: $($_.Exception.Message)" }
    exit 1
} finally {
    Stop-PortForwards
}
