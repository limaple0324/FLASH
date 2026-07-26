# 輔（FLASH）｜SP1＋SP2＋SP3

FLASH（輔）是以穩定、可驗證與可擴充為核心的桌面輔助程式。

## 「輔」專案總入口

完整產品核心、SP1／SP2／SP3 決策、玩家流程、提醒卡規格、實作現況、待驗證項目及未來討論順序，統一以以下文件為基準：

- [「輔」專案完整總整理與未來路線圖](docs/00_輔_專案完整總整理與未來路線圖.md)
- [SP1 基礎系統｜狀態與驗收](docs/01_輔_SP1_基礎系統_狀態與驗收.md)
- [SP2 智慧邏輯｜狀態與驗收](docs/02_輔_SP2_智慧邏輯_狀態與驗收.md)
- [SP3 產品呈現｜狀態與驗收](docs/03_輔_SP3_產品呈現_狀態與驗收.md)
- [SP2 待確認需求與建議稿](docs/SP2_待確認需求與建議稿_2026-07-26.md)
- [SP1 獨立交付區域](deliverables/sp1/README.md)
- [SP2 獨立交付區域](deliverables/sp2/README.md)
- [SP3 獨立交付區域](deliverables/sp3/README.md)

後續進度必須分開標示：**已討論完成、已寫入倉庫、已完成程式實作、
已通過自動測試、已建立 Windows 成品、已通過 Windows 11 實機驗證、
已正式發布**。

SP1、SP2、SP3 各自的交付區與 SP1＋SP2＋SP3 完整累積成品已建立。
目前最新功能來源是 `f861126`，GitHub Actions run `30194797716` 與
artifact `8629766330` 已通過；完整累積成品保存在
`C:\Users\USER\Documents\輔\SP1+SP2+SP3成品`，桌面 `輔.lnk` 已更新但
未自動啟動。每個後續項目開始前仍先確認需求已討論完成。

## Current milestone

目前階段：**SP1＋SP2＋SP3 cumulative snapshot**

最新版本已包含正式五頁 UI、提醒卡、角色清單／詳細資料與備註、完整快捷
鍵、安全停止式同步／智慧重連、角色登入優先選擇及固定藍底白色加號圖示。
全專案角色預設優先序為
`主號 → 次要（分號／小號）→ 備用`，同層才比較較高等級。Windows 11
玩家情境驗收仍待進行；下列段落保留各階段歷史，不代表最新狀態。

SP2 工作區為 `C:\Users\USER\Documents\輔\SP2`，分支為
`sp2/completion-2026-07-26`，基底是已驗收 SP1 提交 `64ecfc2`。
既有 `integration/sp2-sp3-sp35@bd685fb` 只作唯讀來源盤點，不整包合併。

第一批純 SP2 核心提交 `f41e708` 已加入角色、組別、活動、三態、進度、
台灣時間重置、工作區狀態及組別提醒卡資料規則；96 項受影響測試與新增
套件編譯通過，尚未接入 SP3 畫面或任何遊戲操作。

第二批純 SP2 服務提交 `4d63733` 已加入活動進度、卡片歷史、卡片協調與
唯讀 View State；核心與服務共 108 項受影響測試、5 份新增模組編譯及
差異檢查通過。

第三批累積接線提交 `ae76d64` 已在目前 SP1 bootstrap 逐項註冊 8 個 SP2
store/service；51 項 SP2 註冊、服務與 SP1 啟動／自檢回歸測試通過。
目前仍未接入 SP3 畫面、遊戲操作或暫停領域。

第四批 `3bb6ac4` 已加入角色資料安全保存與不外露識別／視窗資訊的唯讀快照；
31 項角色、註冊與 SP1 啟動／自檢回歸測試通過，仍沒有角色 UI 或命魂欄位。

第五批 `acee84c` 曾加入未確認名稱的角色文字紀錄；使用者於 2026-07-26
更正正式名稱應為「備註」。SP3 正常流程不再註冊或呈現該舊欄位，既有
資料檔保持原樣、不自動搬移或刪除。

第六批 `be94642` 的角色詳細唯讀快照與安全選擇命令已收斂為目前確認的
角色資料與備註；命魂及其他暫停欄位不會進入 SP3。

第七批 `04bdfc0` 已加入提醒卡顯示時間安全保存、設定回滾、變更通知與
到期清除服務；53 項相關回歸測試通過，尚未接入 SP3 主視窗或浮層。

第八批 `d359682` 已加入 SP1 目標視窗檢查到 SP2 唯讀狀態的 EventBus
鏈路；32 項相關回歸測試通過，事件不含控制資料、像素或任何輸入。

`978d89e` 已補齊程式結束時的 Logger 資源釋放，Windows 自我檢查完成後
不再鎖住隔離驗證資料。`fecf340` 已把累積程式身分提升為 SP2 `0.2.0`，
並建立獨立的 `FLASH-SP1+SP2-Windows-*` 快照流程；478 項完整測試、
全原始碼編譯、隔離來源自我檢查及差異檢查通過。此累積快照不覆蓋 SP1，
也不包含或發布正式更新器。

GitHub Actions run `30183396825` 的 artifact `8626301455` 已下載並通過
bundle、雜湊及封裝版 8/8 自我檢查；Windows 11 上連續兩次顯示唯一
「輔」主視窗並可正常關閉，兩次關閉後均無 SP2 視窗殘留。

其後 `d8327ce` 已加入已確認週期活動目錄，`5580bcd` 已加入可解釋且
失敗關閉的決策服務，`e7d9225` 已加入七天觀察／第八天玩家確認的活動
順序記憶基礎。三批受影響測試分別為 15、26、26 項通過；完成大區塊後
一次執行的完整來源門檻為 501 項測試、全來源編譯、隔離自我檢查 8/8
與差異檢查通過。尚未建立包含三批新功能的 Windows 成品，因此前述
`fecf340` artifact 不代表目前最新程式。

目前已補建並驗收最新累積成品
`FLASH-SP1+SP2-Windows-0.2.0-43bc04a-snapshot`：GitHub Actions run
`30184919300`／artifact `8626722484` 成功，ZIP／EXE 雜湊、包內驗證、
封裝自我檢查 8/8 與 Windows 11 兩次圖形啟閉全部通過。

桌面 `啟動輔.lnk` 亦已切換到
`C:\Users\USER\AppData\Local\Programs\輔\SP1+SP2\FLASH.exe`，並由桌面
捷徑實際完成啟動、正常關閉與無殘留驗證。原 SP1 安裝位置完整保留。

目前最新 SP1 獨立成品來源為
`28110e6e4857514ee0c99cccacaa2acd5c7b9de7`。278 項測試、原始碼編譯、
來源與封裝 8/8 自我檢查、真實 14 視窗三種輸入政策、14 視窗同時斷線
重連、Windows EXE、ZIP 雜湊、解壓後驗證及兩次圖形啟閉均已通過。
永久成品與證據保存在 `C:\Users\USER\Documents\輔\SP1成品`。
驗收文件提交 `8d58315` 的 GitHub Actions run #130 亦已通過 278 項測試與
完整 Windows 建置；artifact 為 `8625523051`，SP1 專用 `release/sp1`
成功更新，`release/latest` 正確略過。

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
- 完整首次安裝、單一捷徑及交易式 SP1 專用更新
- Windows 11 包裝視窗的完整唯讀自我檢查與安全狀態顯示
- 同名 Flash 視窗的不可逆匿名啟動指紋與失敗關閉式一對一篩選
- 來源腳本與封裝 EXE 的 14 視窗唯讀整合驗證及安全彙總 JSON 報告
- 目前來源的 `B`／`C` 專用安全輸入與三種視窗操作權限
- 10 份完整參考畫面與 2 個線路數字模板的失敗關閉式重連辨識、強制登入
  與 60 秒無限失敗重試
- 依「最近一次登入資訊」動態選線，以及匿名指紋跨重啟保存重連階段與計時
- 三種輸入政策、14 個同時斷線（含 7 個最小化）、三種登入後彈窗與最終
  14/14 正常、未知 0、失敗 0 的實機證據

> 注意：這是獨立 SP1 本機正式驗收快照，不含即時更新器，沒有變更
> `main` 或 `release/latest`。乾淨帳號／別台電腦驗證由使用者決定延後
> 自行執行，不得標成已通過。

## Run from source

```powershell
python main.py
```

啟動後會顯示 SP1 自我檢查結果與紀錄檔位置。

目標桌面唯讀整合驗證：

```powershell
python scripts/verify_target_desktop_sp1.py --expected-count 14
```

封裝 EXE 可使用 `FLASH.exe --verify-target-desktop`，結果寫入應用程式資料
目錄的 `data\target_desktop_verification.json`。這個模式不送出輸入、不保存
畫面，也不輸出視窗、程序、匿名指紋或啟動參數。

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

正式交付標準請見 `SP1_VERIFICATION.md`。SP1 獨立本機交付已達成；合併
`main`、`release/latest` 及乾淨帳號／另一台電腦驗證是另外追蹤的延後項目，
不得倒寫成已完成。
