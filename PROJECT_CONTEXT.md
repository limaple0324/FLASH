# 輔｜SP1 專案接續記錄

更新日期：2026-07-25（台灣時間）

## 倉庫與工作基準

- GitHub：`https://github.com/limaple0324/FLASH.git`
- 工作區：`C:\Users\USER\Documents\輔\SP1`
- 目前分支：`sp1/completion-2026-07-25`
- 基準：`origin/main@538bdbcffd32327cbd3cb32cea1b70cfd9d9e3c3`
- 最新提交：`ac1223a45ccfd12ec3096ccd8fd7933cdbb2bfe3`
- 既有整合分支只保留 SP2／SP3 累積成果；目前不新增上層功能。

## 目前來源證據

- `5db5b5f` 本機完整基準：171 項測試、原始碼編譯、8 項完整來源自我檢查
  及 `git diff --check` 通過。
- `689a186` 雜湊相容性修正：受影響的 24 項更新器／發行驗證測試及 1 項
  防回歸測試通過。
- GitHub Actions run #118：原始碼編譯、完整測試、來源自我檢查、Windows
  EXE 建置、封裝 EXE 自我檢查、bundle 版型與雜湊驗證全部通過。
- Win32 適配器強化與交易式更新器均已完成獨立重點覆核，沒有剩餘阻擋項目。
- 最新 `689a186` Windows 工程快照已永久保存；ZIP SHA-256 與 GitHub
  artifact digest 均為
  `30aca05eb2e8c84f10c58a86cc34af89cda8559b31989b8b6cb266e007b231fa`。
- 本機 Windows PowerShell 5.1 包內驗證器通過；正式 `release/latest`
  發布步驟正確略過。
- Windows 11 build `26200` 隔離資料模式已完成兩次圖形啟動、正常關閉、
  重開、設定／紀錄／登錄資料持久化、8/8 包裝自我檢查及無主控台視窗驗收。
- `f419e07` 的 GitHub Actions run #120 已通過完整測試、編譯、來源／封裝
  自檢、Windows EXE、bundle 與雜湊驗證，並成功發布獨立 `release/sp1`。
- 最新正式 SP1 ZIP 與 artifact digest 均為
  `746fc2c630b77e7d7e3d2db87089716cfe5ebf0fda37e5cf9010d264d5fd6b66`。
- Windows 11 已實際由 `更新輔.cmd` 連線 `release/sp1`，完成安裝前驗證、
  交易式套用與安裝後驗證。
- `cee31ac` 的 GitHub Actions run #123 已通過 183 項完整測試與全部建置／
  發布步驟；最新完整安裝／更新 ZIP SHA-256 與 artifact digest 均為
  `40af7b7c6a8e8ab8c768cdf6aa5961cb82f1a28f6c7075204b316adbec52053a`。
- Windows 11 隔離位置已完成真正成品首次安裝、三次成品驗證及單一捷徑
  TargetPath／WorkingDirectory／IconLocation 驗收。
- `03db062` 的 GitHub Actions run #124 已通過 184 項完整測試、來源／封裝
  8/8 自我檢查、Windows EXE 與 bundle 驗證，並發布到 `release/sp1`。
- Windows 11 已以同提交的視窗成品實際確認完整自我檢查、保守主視窗狀態、
  三項背景能力、角色資料、安全停用與紀錄位置均正確顯示；永久 ZIP 與
  artifact digest 均為
  `e470f5c71b5c81a182d8d0532c1cfb2f00385233868735cdc797284fe99f097b`。
- `ac1223a` 的 GitHub Actions run #125 已通過 186 項完整測試、來源／封裝
  8/8 自我檢查、Windows EXE、bundle 與 `release/sp1` 發布。Windows 11
  真實桌面已完成交易式首次安裝；既有 `輔` junction 保留，安裝器改建唯一
  `啟動輔.lnk`，其目標、工作目錄、圖示、啟動／正常關閉及 EXE 雜湊均通過。
- 尚未完成乾淨新帳號、真正遊戲輸入、斷線重連及視窗
  標題／身分驗收，因此 SP1 尚未正式完成。

## 固定範圍

- 每個項目先確認已討論完成；未確認內容不得自行補造。
- 視窗登記頁內容與判定尚未確認，暫停。
- 魂器、寵物天賦、黑曜石、命魂與背包暫停。
- 遊戲輸入維持停用。
- 只處理 SP1 來源、Windows 成品與正式驗收；SP1 完成後才開始 SP2。

## 下一個小型區塊

使用已驗證的 `ac1223a` SP1 真實桌面入口版繼續乾淨 Windows 11 新帳號
驗收；現有 `C:\Users\USER\Desktop\輔` 專案 junction 與
`C:\Users\USER\Desktop\啟動輔.lnk` 正式程式入口均已驗收，不得互相覆寫。
真正遊戲輸入、斷線重連及 Flash 視窗身分只依已確認規格執行，不自行猜測。
SP1 必要門檻未全部通過前，不開始 SP2。
