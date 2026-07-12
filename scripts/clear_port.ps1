param(
    [int]$Port = 6666,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedApp,
    [int]$WaitSeconds = 10
)

if ($Port -lt 1 -or $Port -gt 65535) {
    Write-Host "[ERROR] Invalid TCP port: $Port" -ForegroundColor Red
    exit 20
}

$expected = [System.IO.Path]::GetFullPath($ExpectedApp)
$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$listenerPids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
$ownedPids = @()

foreach ($processId in $listenerPids) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Write-Host "[ERROR] Cannot inspect PID $processId on port $Port." -ForegroundColor Red
        exit 21
    }

    $commandLine = [string]$process.CommandLine
    if ($commandLine.IndexOf($expected, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        Write-Host "[ERROR] Port $Port belongs to another program; refusing to stop it." -ForegroundColor Red
        Write-Host "        PID: $processId"
        Write-Host "        CommandLine: $commandLine"
        exit 22
    }
    $ownedPids += $processId
}

foreach ($processId in $ownedPids) {
    Write-Host "[INFO] Stopping old SVCVC-API PID $processId..."
    Stop-Process -Id $processId -Force -ErrorAction Stop
}

if ($ownedPids.Count -gt 0) {
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $WaitSeconds))
    do {
        $remaining = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($remaining.Count -eq 0) {
            Write-Host "[INFO] Port $Port has been released."
            exit 0
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    $remainingPids = @($remaining | Select-Object -ExpandProperty OwningProcess -Unique)
    Write-Host "[ERROR] Port $Port was not released within $WaitSeconds seconds. Remaining PID(s): $($remainingPids -join ', ')" -ForegroundColor Red
    exit 23
}

exit 0
