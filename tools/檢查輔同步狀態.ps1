# 檢查輔同步狀態
# 用途：檢查 Windows 工作排程裡是否還有舊的自動同步。

$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$TaskName = "FLASH GitHub Desktop Auto Sync"
$Desktop = [Environment]::GetFolderPath("Desktop")
$ReportDir = Join-Path $Desktop "輔"
$ReportPath = Join-Path $ReportDir "同步狀態檢查.txt"

New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

function Write-Line([string]$Message) {
    Write-Host $Message
    Add-Content -LiteralPath $ReportPath -Value $Message -Encoding UTF8
}

if (Test-Path -LiteralPath $ReportPath) {
    Clear-Content -LiteralPath $ReportPath
}

Write-Line "輔｜同步狀態檢查"
Write-Line "檢查時間：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Line ""

try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    if (-not $task) {
        Write-Line "結果：沒有找到舊的自動同步排程。"
        Write-Line ""
        Write-Line "判斷：目前不會每隔一段時間自動跳出同步視窗。"
        Write-Line "建議：之後使用桌面的「更新輔.cmd」手動更新即可。"
    }
    else {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        $triggerText = ($task.Triggers | ForEach-Object { $_.ToString() }) -join "`n"
        $actionText = ($task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join "`n"

        Write-Line "結果：找到舊的自動同步排程。"
        Write-Line ""
        Write-Line "排程名稱：$TaskName"
        Write-Line "目前狀態：$($task.State)"
        Write-Line "上次執行：$($info.LastRunTime)"
        Write-Line "下次執行：$($info.NextRunTime)"
        Write-Line "上次結果碼：$($info.LastTaskResult)"
        Write-Line ""
        Write-Line "執行內容："
        Write-Line $actionText
        Write-Line ""
        Write-Line "觸發方式："
        Write-Line $triggerText
        Write-Line ""
        Write-Line "判斷：你看到突然跳出又消失的視窗，很可能就是這個排程。"
        Write-Line "建議：如果已改用「更新輔.cmd」，可以考慮停用舊同步，避免它一直自動跑。"
    }
}
catch {
    Write-Line "檢查失敗：$($_.Exception.Message)"
}

Write-Line ""
Write-Line "報告位置：$ReportPath"
Write-Host ""
Write-Host "按任意鍵關閉。"
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
