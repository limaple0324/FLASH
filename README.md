# FLASH

FLASH（輔）是以穩定、可驗證與可擴充為核心的桌面輔助程式。

## 「輔」專案總入口

完整產品核心、SP1／SP2／SP3 決策、玩家流程、提醒卡規格、實作現況、待驗證項目及未來討論順序，統一以以下文件為基準：

- [「輔」專案完整總整理與未來路線圖](docs/00_輔_專案完整總整理與未來路線圖.md)

後續進度必須分開標示：**已討論完成、已寫入倉庫、已完成程式實作、已通過雲端測試、已通過 Windows 實機驗證**。

## Current milestone

目前版本：**SP1 0.1.2 engineering verification build**

SP1 目前已包含：

- application bootstrap
- centralized persistent paths
- JSON configuration
- service registry
- event bus and logging
- Recovery / Smart Reconnect / External Adapter contracts
- structured self-check system
- Windows desktop verification window
- player-facing Windows updater and legacy sync status check
- PyInstaller build specification
- GitHub Actions test-and-build workflow
- release bundle metadata and SHA-256 verification

> 注意：目前是工程驗證版本。Recovery、Smart Reconnect 與遊戲視窗操作的介面已固定，但真正的遊戲／視窗適配器仍需接入並完成 Windows 實機驗收。

## Run from source

```powershell
python main.py
```

啟動後會顯示 SP1 自我檢查結果與紀錄檔位置。

## Test

```powershell
python -m pytest -q
```

## Windows setup and verification

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_setup_sp1.ps1
```

此腳本會檢查 Python、安裝必要套件、執行測試與無視窗自我檢查，最後啟動 FLASH。

## Windows 成品更新

玩家只需要使用成品資料夾內的 `更新輔.cmd`。更新程式會取得
`release/latest`，並保留既有桌面捷徑的名稱與圖示。

倉庫不再提供背景排程或定時 Git 同步。曾安裝舊排程的電腦，可執行
`檢查輔同步狀態.cmd` 確認並依中文提示停用。

## Verify a downloaded Windows build

下載並解壓 GitHub Actions Artifact 後執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\verify_windows_release.ps1
```

驗收腳本會核對 `FLASH.exe`、SHA-256 與建置資訊，通過後啟動程式。

## Delivery status

正式交付標準請見 `SP1_VERIFICATION.md`。只有 GitHub Actions 建置、Windows 成品驗證、更新流程與目標電腦實機驗收全部通過，才視為 SP1 完成。
