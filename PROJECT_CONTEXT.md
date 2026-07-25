# 輔｜SP1 專案接續記錄

更新日期：2026-07-25（台灣時間）

## 倉庫與工作基準

- GitHub：`https://github.com/limaple0324/FLASH.git`
- 工作區：`C:\Users\USER\Documents\輔\SP1`
- 目前分支：`sp1/completion-2026-07-25`
- 基準：`origin/main@538bdbcffd32327cbd3cb32cea1b70cfd9d9e3c3`
- 既有整合分支只保留 SP2／SP3 累積成果；目前不新增上層功能。

## 目前來源證據

- 110 項測試通過。
- 原始碼編譯通過。
- 8 項完整來源自我檢查通過。
- 自我檢查根目錄：
  `C:\Users\USER\Documents\輔\self-check-sp1-main-d415852405be43e480ba42bb80b838e0`
- 目前工作樹建立後尚未產生新的 Windows artifact。

## 固定範圍

- 每個項目先確認已討論完成；未確認內容不得自行補造。
- 視窗登記頁內容與判定尚未確認，暫停。
- 魂器、寵物天賦、黑曜石、命魂與背包暫停。
- 遊戲輸入維持停用。
- 只處理 SP1 來源、Windows 成品與正式驗收；SP1 完成後才開始 SP2。

## 下一個小型區塊

先建立 SP1 獨立交付與文件基準；接著修正 Windows 工作流程，使分支／手動
建置只能產生不發布的 SP1 artifact，只有核准的 `main` push 才能更新
`release/latest`；同時固定 SP1 獨立更新來源，使它不會自動前進到
SP2／SP3。之後建立本分支工程快照並進行 Windows 11 驗證。
