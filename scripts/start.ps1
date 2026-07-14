$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runtime = Join-Path $root "runtime"
$python = Join-Path $runtime "python.exe"
$app = Join-Path $root "app.py"
$configPath = Join-Path $root "config.json"
$installer = Join-Path $PSScriptRoot "install_runtime.ps1"
$clearPort = Join-Path $PSScriptRoot "clear_port.ps1"

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONNOUSERSITE = "1"
$env:NO_PROXY = "127.0.0.1,localhost"
$env:no_proxy = "127.0.0.1,localhost"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Write-Host "[INFO] Embedded Python is missing; preparing it now..."
        & $installer
    }

    & $python -c "import gradio, gradio_client, httpx, requests, soundfile, yt_dlp" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[INFO] Python dependencies are incomplete; repairing them now..."
        & $installer
    }

    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Missing configuration file: $configPath"
    }
    if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
        throw "Missing application file: $app"
    }

    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $hostName = [string]$config.server.host
    $port = [int]$config.server.port
    if ([string]::IsNullOrWhiteSpace($hostName)) {
        throw "config.json server.host is empty"
    }
    if ($port -lt 1 -or $port -gt 65535) {
        throw "config.json server.port is invalid: $port"
    }

    $msstRootValue = [string]$config.msst.root
    if ([string]::IsNullOrWhiteSpace($msstRootValue)) { $msstRootValue = "." }
    if ([System.IO.Path]::IsPathRooted($msstRootValue)) {
        $msstRoot = [System.IO.Path]::GetFullPath($msstRootValue)
    }
    else {
        $msstRoot = [System.IO.Path]::GetFullPath((Join-Path $root $msstRootValue))
    }
    $msstRequired = @(
        (Join-Path $msstRoot "runtime-rocm\Scripts\python.exe"),
        (Join-Path $msstRoot "msst\msst_separate.py"),
        (Join-Path $msstRoot "msst\pretrain\vocal_models\model_bs_roformer_ep_317_sdr_12.9755.ckpt"),
        (Join-Path $msstRoot "msst\configs\vocal_models\model_bs_roformer_ep_317_sdr_12.9755.ckpt.yaml")
    )
    $msstMissing = @($msstRequired | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($msstMissing.Count -eq 0) {
        Write-Host "[INFO] Bundled MSST is ready: $msstRoot" -ForegroundColor Green
    }
    else {
        Write-Host "[WARN] Bundled MSST is incomplete; SoulX/none modes remain available." -ForegroundColor Yellow
        $msstMissing | ForEach-Object { Write-Host "       Missing: $_" -ForegroundColor Yellow }
    }

    Write-Host "[INFO] Checking old SVCVC-API listener on port $port..."
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $clearPort -Port $port -ExpectedApp $app -WaitSeconds 10
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    $displayHost = $hostName
    if ($displayHost -eq "0.0.0.0") { $displayHost = "127.0.0.1" }
    if ($displayHost -eq "::" -or $displayHost -eq "::1") { $displayHost = "[::1]" }

    Write-Host "[INFO] SoulX-Singer SVC must be ready at http://127.0.0.1:7861"
    Write-Host "[INFO] Starting SVCVC-API at http://${displayHost}:$port"
    & $python $app
    exit $LASTEXITCODE
}
catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
