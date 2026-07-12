$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$pythonVersion = "3.12.10"
$pythonTag = "312"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runtime = Join-Path $root "runtime"
$python = Join-Path $runtime "python.exe"
$requirements = Join-Path $root "requirements.txt"
$archive = Join-Path $env:TEMP "svcvc-python-$pythonVersion.zip"
$getPip = Join-Path $env:TEMP "svcvc-get-pip.py"

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Write-Host "[INFO] Downloading official CPython $pythonVersion embeddable x64..."
        $pythonUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
        Invoke-WebRequest -UseBasicParsing -Uri $pythonUrl -OutFile $archive

        if (Test-Path -LiteralPath $runtime) {
            Remove-Item -LiteralPath $runtime -Recurse -Force
        }
        New-Item -ItemType Directory -Path $runtime | Out-Null
        Expand-Archive -LiteralPath $archive -DestinationPath $runtime -Force

        $pth = Get-ChildItem -LiteralPath $runtime -Filter "python$pythonTag._pth" | Select-Object -First 1
        if ($null -eq $pth) {
            throw "Could not find python$pythonTag._pth in the embedded runtime"
        }
        $pthText = Get-Content -LiteralPath $pth.FullName -Raw
        $pthText = $pthText -replace "(?m)^#import site$", "import site"
        [System.IO.File]::WriteAllText($pth.FullName, $pthText, [System.Text.Encoding]::ASCII)

        Write-Host "[INFO] Installing pip..."
        Invoke-WebRequest -UseBasicParsing -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
        & $python $getPip --disable-pip-version-check
        if ($LASTEXITCODE -ne 0) {
            throw "get-pip.py failed with exit code $LASTEXITCODE"
        }
    }

    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        throw "Missing requirements file: $requirements"
    }

    Write-Host "[INFO] Installing SVCVC-API dependencies..."
    & $python -m pip install --break-system-packages --disable-pip-version-check --upgrade -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed with exit code $LASTEXITCODE"
    }
    Write-Host "[OK] Embedded Python and dependencies are ready."
}
catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
