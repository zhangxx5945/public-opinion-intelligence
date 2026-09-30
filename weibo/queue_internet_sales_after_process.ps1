param(
    [Parameter(Mandatory = $true)]
    [int]$WaitPid,
    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,
    [Parameter(Mandatory = $true)]
    [string]$TaskName,
    [Parameter(Mandatory = $true)]
    [string]$Category,
    [Parameter(Mandatory = $true)]
    [string]$KeywordsFile,
    [Parameter(Mandatory = $true)]
    [string]$StateDir
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$batch = Join-Path $PSScriptRoot 'weibo_government_service_batch_2025.py'
$statusPath = Join-Path $StateDir 'weibo_internet_sales_queue_status.json'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Save-QueueStatus {
    param([string]$Status, [Nullable[int]]$ProcessId = $null, [string]$OutLog = '', [Nullable[int]]$ExitCode = $null)
    $payload = [ordered]@{
        task = $TaskName
        status = $Status
        wait_pid = $WaitPid
        process_id = $ProcessId
        output_log = $OutLog
        exit_code = $ExitCode
        updated_at = (Get-Date).ToString('o')
    }
    $payload | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
}

Save-QueueStatus -Status 'waiting_for_public_culture'
Wait-Process -Id $WaitPid -ErrorAction SilentlyContinue

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$outLog = Join-Path $StateDir ("weibo_internet_sales_auto_${stamp}.out.log")
$errLog = Join-Path $StateDir ("weibo_internet_sales_auto_${stamp}.err.log")
$arguments = @(
    $batch,
    '--output-root', $OutputRoot,
    '--task-name', $TaskName,
    '--category', $Category,
    '--keywords-file', $KeywordsFile
)

Save-QueueStatus -Status 'starting' -OutLog $outLog
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WindowStyle Hidden -PassThru
Save-QueueStatus -Status 'running' -ProcessId $process.Id -OutLog $outLog
Wait-Process -Id $process.Id
$process.Refresh()
Save-QueueStatus -Status 'finished' -ProcessId $process.Id -OutLog $outLog -ExitCode $process.ExitCode
exit $process.ExitCode
