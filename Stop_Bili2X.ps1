$ErrorActionPreference = 'Stop'
# Match the full script path of this checkout, not every Python process.
$paths = @('bili2x_watcher.py', 'bili2x_worker.py') | ForEach-Object {
    [regex]::Escape((Join-Path $PSScriptRoot $_))
}
$pattern = '(?i)(?:"|\s)(' + ($paths -join '|') + ')(?:"|\s|$)'
$processes = @(Get-CimInstance Win32_Process)
$targets = @($processes | Where-Object {
    $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match $pattern
})
# Include children (e.g. FFmpeg) before terminating the worker/watcher.
$targetIds = [System.Collections.Generic.HashSet[int]]::new()
foreach ($target in $targets) { [void]$targetIds.Add([int]$target.ProcessId) }
do {
    $added = $false
    foreach ($item in $processes) {
        if ($targetIds.Contains([int]$item.ParentProcessId) -and $targetIds.Add([int]$item.ProcessId)) {
            $added = $true
        }
    }
} while ($added)
foreach ($targetId in $targetIds) {
    # A Python launcher may exit when its child exits; tolerate that race only.
    try {
        if (Get-Process -Id $targetId -ErrorAction SilentlyContinue) {
            Stop-Process -Id $targetId -Force -ErrorAction Stop
        }
    } catch {
        if (Get-Process -Id $targetId -ErrorAction SilentlyContinue) { throw }
    }
}
Write-Host ('Stopped {0} process(es) belonging to this checkout.' -f $targetIds.Count)
