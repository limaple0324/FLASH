# Devspace 本機橋接層

用途：讓目前 GPT 對話可以透過 GitHub 專用佇列，把「輔」的本機驗證工作交給 Windows 執行，再把結果自動送回 GitHub。

目前第一版刻意採安全白名單，不提供任意命令、任意刪檔、任意關程序或遊戲點擊。

## 固定架構

GPT → `automation/devspace-queue` 任務檔 → Windows 橋接程式 → 隔離工作樹執行 → 結果檔推回同一佇列分支 → GPT 讀取結果。

橋接程式永遠使用指定的 40 碼提交建立獨立工作樹，不切換、不修改使用者正在操作的「輔」工作樹。

## 第一版可執行工作

- `ping`：確認橋接程式在線。
- `repo_snapshot`：建立指定提交的隔離工作樹並回報狀態。
- `read_text`：唯讀指定提交內的小型文字檔。
- `run_tests`：只執行明確列出的測試檔／測試節點。
- `build_candidate`：用既有 `scripts/build_coordinator.py` 建置候選 `FLASH.exe`，不安裝。
- `installed_fu_hash`：讀取目前正式 `FLASH.exe` 雜湊。
- `installed_fu_self_check`：執行正式版 `--self-check`。
- `process_snapshot`：只列出「輔」與 Flash 相關程序，不終止程序。

## 明確禁止

第一版沒有以下能力：

- 任意終端命令。
- 任意 PowerShell／CMD 指令。
- 刪除使用者檔案。
- 關閉任意程序。
- 滑鼠／鍵盤操作。
- 修改正式桌面版。
- 自動操作遊戲。

需要這些能力時，必須另外增加明確、範圍受限的動作，不能把橋接層改成無限制終端。

## 任務格式

```json
{
  "schema_version": 1,
  "task_id": "fu-test-20260819-001",
  "action": "run_tests",
  "target_commit": "38cf30dc1e4248b967c4900199364ac5ba0d6d96",
  "args": {
    "tests": ["tests/test_windows_timed_click.py"],
    "timeout_seconds": 900
  }
}
```

任務放在：`tools/devspace_bridge/queue/inbox/<task_id>.json`

結果回到：`tools/devspace_bridge/queue/results/<task_id>.json`

## Windows 啟動／停止

雙擊 `啟動Devspace橋接.cmd` 後，橋接程式會以背景 Python 執行並輪詢專用佇列。

雙擊 `停止Devspace橋接.cmd` 只會建立停止請求，橋接程式自行安全退出，不會使用強制終止程序。

本機狀態資料預設位於：`%LOCALAPPDATA%\輔\Devspace`
