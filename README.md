# 輔（FLASH）｜SP1

FLASH（輔）是以穩定、可驗證與可擴充為核心的桌面輔助程式。

## 「輔」專案總入口

完整產品核心、SP1／SP2／SP3 決策、玩家流程、提醒卡規格、實作現況、待驗證項目及未來討論順序，統一以以下文件為基準：

- [「輔」專案完整總整理與未來路線圖](docs/00_輔_專案完整總整理與未來路線圖.md)
- [SP1 基礎系統｜狀態與驗收](docs/01_輔_SP1_基礎系統_狀態與驗收.md)
- [SP1 獨立交付區域](deliverables/sp1/README.md)

後續進度必須分開標示：**已討論完成、已寫入倉庫、已完成程式實作、
已通過自動測試、已建立 Windows 成品、已通過 Windows 11 實機驗證、
已正式發布**。

目前只進行 SP1 的完成工作，而且 SP1 尚未正式完成。SP1 正式完成後才開始
新增 SP2，SP2 完成後才開始新增 SP3。每個項目開始前先確認需求已討論完成。

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
- 舊排程同步的註冊／執行工具已移除，僅保留同步狀態檢查器
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

玩家只保留單一「更新輔」入口；舊的每十五分鐘 Git 同步不是目前更新方式，
不得重新啟用。SP1 獨立成品還必須使用固定的 SP1 更新來源，不能因日後更新
自動變成 SP2／SP3 或完整整合版。

## Verify a downloaded Windows build

下載並解壓 GitHub Actions Artifact 後執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\輔系統\verify_windows_release.ps1
```

驗收腳本會核對 `FLASH.exe`、SHA-256 與建置資訊，通過後啟動程式。

## Delivery status

正式交付標準請見 `SP1_VERIFICATION.md`。只有 GitHub Actions 建置、Windows
成品、單一更新流程、真正遊戲安全與目標電腦實機驗收全部通過，才視為
SP1 完成。
