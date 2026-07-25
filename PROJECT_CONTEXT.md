# 輔｜SP1 專案接續記錄

更新日期：2026-07-25（台灣時間）

## 倉庫與工作基準

- GitHub：`https://github.com/limaple0324/FLASH.git`
- 工作區：`C:\Users\USER\Documents\輔\SP1`
- 目前分支：`sp1/completion-2026-07-25`
- 基準：`origin/main@538bdbcffd32327cbd3cb32cea1b70cfd9d9e3c3`
- 最新功能提交：`f47e132d1d3677af9fa2952ee6dcd99e0d073918`
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
- `960bacb` 的 GitHub Actions run #127 已通過 188 項完整測試與全部建置／
  發布；14 個真實 Flash 視窗的唯讀 `PrintWindow` 實測涵蓋 13 個非前景、
  4 個部分遮擋、1 個完全遮擋及 5 個最小化視窗。最小化擷取已修正為使用
  正常視窗尺寸，5 個視窗均取得 911／916×629 有效畫面；沒有保存畫面或
  傳送輸入。正式安裝亦已交易式更新到同一提交。
- `2fae8bf` 已接入使用者確認的匿名啟動參數 SHA-256 指紋：無效、缺失或
  重複身分一律拒絕，且不輸出原始參數、程序命令列或登入權杖。本機 200 項
  完整測試、原始碼編譯及 8/8 來源自我檢查通過；14 個執行中 Flash 視窗
  實測為 14 個程序、14 個不同指紋，嚴格一對一成立，未傳送輸入。
- 成品來源 `49ee668` 的 GitHub Actions run #128 已通過 200 項測試、來源／
  封裝 8/8 自檢、Windows EXE、bundle 與 `release/sp1` 發布。artifact
  `8617136485`／永久 ZIP SHA-256 為
  `40838dfff48e858079a479242aa670743e8f08b5ec0c5f3e7d73a5f01bd3852d`；
  包裝指紋選擇、交易式正式更新、圖形啟動、正常關閉與無殘留程序均通過。
- `f47e132` 已把 14 視窗實機檢查固定成來源腳本與封裝 EXE 的
  `--verify-target-desktop` 唯讀模式；報告只含彙總數量，不含控制代碼、
  程序代號、指紋、像素或原始參數。本機 209 項測試、編譯、8/8 來源自檢及
  真實 14/14 身分選取／14/14 非空白擷取通過。
- 成品來源 `0bd575c` 的 GitHub Actions run #129、artifact `8617356997`、
  包內驗證器、正式交易式更新、安裝版 `--verify-target-desktop`、一般圖形
  啟閉與無殘留程序全部通過。永久 ZIP SHA-256 為
  `ab02bf2b5d8d8eba3fcf5278329cf4c23741da360b2b824c40ce8473f762629f`。
- 尚未完成乾淨新帳號、真正遊戲輸入及斷線重連驗收，因此 SP1 尚未正式完成。

## 固定範圍

- 每個項目先確認已討論完成；未確認內容不得自行補造。
- 視窗登記頁內容與判定尚未確認，暫停。
- 魂器、寵物天賦、黑曜石、命魂與背包暫停。
- 遊戲輸入維持停用。
- 只處理 SP1 來源、Windows 成品與正式驗收；SP1 完成後才開始 SP2。

## 下一個小型區塊

使用已驗證的 `0bd575c` 目標桌面唯讀驗證成品，繼續乾淨 Windows 11 新帳號
驗收；現有 `C:\Users\USER\Desktop\輔` 專案 junction 與
`C:\Users\USER\Desktop\啟動輔.lnk` 正式程式入口均已驗收，不得互相覆寫。
Flash 同名視窗只使用已確認的匿名啟動指紋並嚴格一對一；角色名稱對應仍不
猜測。真正遊戲輸入與斷線重連只依已確認規格執行。
SP1 必要門檻未全部通過前，不開始 SP2。
