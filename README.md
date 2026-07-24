# 輔（FLASH）

FLASH（輔）是以穩定、可驗證與可擴充為核心的桌面輔助程式。

## 「輔」專案總入口

完整產品核心、SP1／SP2／SP3 決策、玩家流程、提醒卡規格、實作現況、待驗證項目及未來討論順序，統一以以下文件為基準：

- [「輔」專案完整總整理與未來路線圖](docs/00_輔_專案完整總整理與未來路線圖.md)

後續進度必須分開標示：**已討論完成、已寫入倉庫、已完成程式實作、已通過雲端測試、已通過 Windows 實機驗證**。

## 目前里程碑

- `main`／正式成品：**SP1 0.1.2 工程驗證版**
- `integration/sp2-sp3-sp35`：持續整合 SP2／SP3／SP3.5

整合分支已包含產品與領域資料骨架、工作區與組別級提醒卡、提醒卡生命週期、
歷史紀錄、玩家顯示時間設定及浮層骨架。實際進度以
[`docs/INTEGRATION_BOARD.md`](docs/INTEGRATION_BOARD.md) 為準。

SP1 目前已包含：

- application bootstrap
- centralized persistent paths
- JSON configuration
- service registry
- event bus and logging
- Recovery / Smart Reconnect / External Adapter contracts
- structured self-check system
- Windows desktop verification window
- Windows desktop auto-sync scripts
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

## 玩家更新

正式成品只保留一個玩家需要操作的更新入口：

```text
更新輔.cmd
```

它會取得 `release/latest` 的最新成品、核對 `FLASH.exe` 雜湊、更新目前安裝
資料夾，並保留既有桌面「輔 V0.2」捷徑名稱與圖示。

舊版每 15 分鐘 Git 同步排程已停用，不再提供建立該排程的腳本。若舊電腦曾
安裝過，可執行成品內 `輔系統\檢查輔同步狀態.cmd` 取得可判斷的中文報告。

## Verify a downloaded Windows build

下載並解壓 GitHub Actions Artifact 後執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\輔系統\verify_windows_release.ps1
```

驗收腳本會核對 `FLASH.exe`、SHA-256 與建置資訊，通過後啟動程式。

## Delivery status

正式交付標準請見 `SP1_VERIFICATION.md`。只有 GitHub Actions 建置、Windows
成品驗證、單一更新流程與目標電腦實機驗收全部通過，才視為 SP1 完成。
