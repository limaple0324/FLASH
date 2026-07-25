# 輔（FLASH）

FLASH（輔）是以穩定、可驗證與可擴充為核心的桌面輔助程式。

## 「輔」專案總入口

完整產品核心、SP1／SP2／SP3 決策、玩家流程、提醒卡規格、實作現況、待驗證項目及未來討論順序，統一以以下文件為基準：

- [「輔」專案完整總整理與未來路線圖](docs/00_輔_專案完整總整理與未來路線圖.md)
- [SP1 基礎系統｜狀態與驗收](docs/01_輔_SP1_基礎系統_狀態與驗收.md)
- [SP2 智慧邏輯｜狀態與驗收](docs/02_輔_SP2_智慧邏輯_狀態與驗收.md)
- [SP3 產品呈現｜狀態與驗收](docs/03_輔_SP3_產品呈現_狀態與驗收.md)

後續進度必須分開標示：**已討論完成、已寫入倉庫、已完成程式實作、
已通過自動測試、已建立 Windows 成品、已通過 Windows 11 實機驗證、已正式發布**。

## 目前里程碑

- `main`／正式成品：**SP1 0.1.2 工程驗證版**
- `integration/sp2-sp3-sp35`：持續整合 SP2／SP3／SP3.5

整合分支已包含產品與領域資料骨架、工作區與組別級提醒卡、提醒卡生命週期、
歷史紀錄、玩家顯示時間設定、浮層骨架、組別角色清單、角色詳細資料，以及
靈魂石正常編輯與安全保存流程。`893f65e` 已通過 485 項自動測試；GitHub
Actions run `30134982561` 已成功建立、下載並獨立重驗
`輔-整合工程驗證版-SP1-SP2-SP3-0.1.2-893f65e-windows-x64`。包內驗證器、
隔離 `--self-check` 與 Windows 11 標題「輔」主視窗啟閉均通過，且沒有殘留程序。
這仍只是基本成品冒煙，不代表完整玩家、真正遊戲、DPI／多螢幕、乾淨帳號、
背景能力、更新流程或正式發布驗收。實際進度以
[`docs/INTEGRATION_BOARD.md`](docs/INTEGRATION_BOARD.md) 為準。

快照 ZIP SHA-256：
`E9E0E39E6E7ACC158B8D3A770180C31DE485554143A8CE2548B3A97CEF5BDA69`；
`FLASH.exe` SHA-256：
`36fa8642f552594b21b5e30b2622e7cf385acae090f56436e2f1c9420e9b7e0f`。
整合快照不含更新器，工作流程的發布步驟已跳過，`release/latest` 仍是
`341ce3cdfd715531ae64c4b75f08fc3af4e8ad15`。

SP1 目前已包含：

- application bootstrap
- centralized persistent paths
- JSON configuration
- service registry
- event bus and logging
- Recovery / Smart Reconnect / External Adapter contracts
- structured self-check system
- Windows desktop verification window
- single player-facing Windows update flow
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
