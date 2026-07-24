# 輔｜專案接續記錄

更新日期：2026-07-25（台灣時間）

## 倉庫與工作基準

- GitHub：`https://github.com/limaple0324/FLASH.git`
- 倉庫名稱：`limaple0324/FLASH`
- 目前接續分支：`integration/sp2-sp3-sp35`
- 2026-07-25 交接基準：`c65b740e9e134057eb04142648ee2635862cb03c`
- 產品決策以正式文件為準；實作狀態以目前程式、測試、提交與自我檢查共同判斷。

## 2026-07-25 已核對的遠端狀態

- `main`：`538bdbcffd32327cbd3cb32cea1b70cfd9d9e3c3`，仍是 SP1 0.1.2 工程驗證版。
- `integration/sp2-sp3-sp35` 本次功能核對基準：
  `85f6e30057ed994ff8980b0d7d27ff7550b7e0b9`。
- `integration/sp2-sp3-sp35` 本次 Windows 快照來源基準：
  `a2bf410298358d6962b76592a08a458e94b88d03`。
- `release/latest`：`341ce3cdfd715531ae64c4b75f08fc3af4e8ad15`。
- 開放中的合併請求：無。
- 在快照來源 `a2bf410`，整合分支相對 `main`：多 84 筆、少 31 筆提交。
- 模擬合併會衝突：`PROJECT_CONTEXT.md`、`README.md`、`main.py`、
  `tests/test_home_view_text.py`、`tests/test_main_window_layout.py`、`ui/home.py`。
- 不可硬合併、強推或以任一方整檔覆蓋衝突。

## 目前已完成的程式

- SP1 既有服務建立、自我檢查、紀錄、錯誤處理、視窗安全邊界與發布工具。
- Sprint 1 產品名稱、角色、組別、活動、三態、進度、每日重置與安全保存。
- 工作區模型與服務。
- 組別級提醒卡、優先度、最多三張、生命週期、斷線／恢復歷史及原子化保存。
- 首頁提醒刷新、到期清除、Windows 工作區定位與可替換浮層骨架。
- 提醒卡預覽方案、關閉、錯誤保留及玩家可調整並保存顯示時間。
- 組別角色唯讀清單、角色詳細資料、安全保存與啟動載入。
- 每角色靈魂石模型、保存、顯示、修改／清除服務、獨立編輯視窗與安全角色配對。
- 角色詳細頁已正式接上靈魂石編輯；保存／清除後會重抓正確角色並刷新，
  失敗時保留原資料、顯示中文訊息並寫入紀錄。
- Windows 執行相依已明列 `tzdata`；全新虛擬環境只安裝
  `requirements.txt` 後，可正確載入 `Asia/Taipei` 並通過完整來源驗證。
- Windows 工作流程已分離 SP1 引擎中繼資料與 SP1＋SP2＋SP3 交付身分；
  只有 `main` 的正式 push 能更新 `release/latest`，手動建置只產生快照。
- 整合快照刻意不包含「更新輔」與更新核心，避免工程快照切回舊正式通道。

## SP1／SP2／SP3 獨立交付檔

- `docs/01_輔_SP1_基礎系統_狀態與驗收.md`
- `docs/02_輔_SP2_智慧邏輯_狀態與驗收.md`
- `docs/03_輔_SP3_產品呈現_狀態與驗收.md`
- 整體彙整：`docs/00_輔_專案完整總整理與未來路線圖.md`

四份文件共用同一套累積原始碼，不代表建立四套互相漂移的工程。

## 尚未接入或尚未驗證

- `WorkspaceService` 已有模型與測試，但尚未成為正常主程式工作區資訊來源。
- 正常 `main()` 尚未提供正式預設提醒卡方案，真正遊戲事件也尚未產生提醒卡。
- 真正斷線偵測、重新登入、背景／最小化能力與遊戲輸入尚未完成實機驗證。
- 本機整合快照已建立並通過 Windows 11 啟動煙霧測試，但尚未完成乾淨使用者帳號、
  真正遊戲、多視窗、背景／最小化、長時間效能與桌面「更新輔」完整驗收。
- 尚未由 GitHub Actions 產生並下載核對本次整合快照 artifact；`release/latest`
  仍維持舊 `main` 工程驗證版，沒有被本機快照覆蓋。

## 目前驗證快照

在 Windows 11 Enterprise 10.0.26200 的乾淨虛擬環境，針對
`integration/sp2-sp3-sp35@a2bf410` 重新執行：

- 484 項測試全部通過。
- 全部 Python 原始碼編譯通過。
- 完整來源程式自我檢查通過。
- `ZoneInfo("Asia/Taipei")` 可正常載入。
- 設定、紀錄、自我檢查報告與視窗註冊檔均成功建立。
- PyInstaller 6.21.0 已建立 `FLASH.exe`，並明確套用 `zoneinfo` 與 `tzdata` hook。
- 封裝後 `FLASH.exe --self-check`、解壓後重驗、SHA-256 與建置身分全部通過。
- Windows 11 GUI 已實際開啟標題為「輔」的主視窗，並可正常關閉。

本機獨立快照：
`C:\Users\USER\Documents\輔\成品\輔-整合工程驗證版-SP1-SP2-SP3-0.1.2-a2bf410-windows-x64.zip`

- `FLASH.exe` SHA-256：
  `56580bfe6fe3b124ef924faa795cdab9f7fe16979004d325475d1a242c1e3c3a`
- ZIP SHA-256：
  `216cf43415ab730b8915bfb3941368d2ee314fa09818b030af4c991ba765cbec`

以上證明本機 Windows 11 建置、封裝自我檢查與基本 GUI 啟動通過；不等於
GitHub Actions artifact、完整玩家情境、真正遊戲或正式發布驗收。

## 下一個小型接續區塊

由 GitHub Actions 手動建置 `integration/sp2-sp3-sp35`，下載並重驗遠端
artifact；保持 `release/latest` 不變。通過後接續 `WorkspaceService` 正常主流程
接線，仍以一個可獨立驗證的小區塊為單位。

## 固定接續提醒

- 不重新設計，不重新討論已定案 UI，直接依既有 Blueprint 落地。
- `docs/USER_REQUIREMENTS_2026-07-24.md` 的 15 項需求必須持續納入；第 3、8 項不可猜測。
- 依 `docs/INTEGRATION_BOARD.md` 的狀態與固定邊界接續，但不可只看勾選框判斷完成度。
- 每次只完成一個小型、明確、可獨立驗證的區塊，通過後立即提交並推送。
- 不可因完成單一區塊或中間里程碑而停止持續開發。
- 使用者不需要理解技術細節；玩家工具失敗時必須留下中文畫面提示或可查看紀錄。
- 舊式每十五分鐘 Git 同步已移除；玩家更新入口固定為單一「更新輔」。
- SP1、SP2、SP3 各保留可追溯的獨立交付檔案，另建立一份整體彙整檔；
  不複製成互相漂移的多套原始碼。
