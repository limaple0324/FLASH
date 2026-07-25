# 輔｜SP1 獨立交付區域

此目錄是 SP1 的唯一獨立交付入口，不放入 SP2／SP3 功能，也不複製另一套
無法追蹤的原始碼。

## 目前來源

- GitHub：`https://github.com/limaple0324/FLASH.git`
- 分支：`sp1/completion-2026-07-25`
- 基準：`main@538bdbcffd32327cbd3cb32cea1b70cfd9d9e3c3`
- 版本：SP1 0.1.2 工程驗證階段

## 目前來源驗證

- 自動測試：110 項通過
- 原始碼編譯：通過
- 完整來源自我檢查：8 項通過
- 驗證日期：2026-07-25（台灣時間）

以上只證明目前來源基準可測試；尚未建立本分支的 Windows 成品，也未完成
乾淨帳號、真正遊戲視窗、背景／最小化、斷線重連、安全輸入、更新及正式發布
驗收。

## 後續成品紀錄

建立 Windows 工程快照後，必須在本目錄留下可追溯的交付紀錄，至少包含：

- 來源 branch 與完整 commit
- 產品版本與 SP1 里程碑
- 測試、編譯及來源／包內自我檢查結果
- `FLASH.exe` 與 ZIP 的 SHA-256
- GitHub Actions run 或本機建置方式
- Windows 11 已通過與未通過項目
- 是否發布 `release/latest`（預設不得發布）
- 獨立 SP1 更新來源；不得因日後更新自動變成 SP2／SP3 或完整整合版

實際 `.exe`／ZIP 可放在使用者的成品目錄或 GitHub artifact；大型二進位檔不
直接提交進原始碼分支。

永久保存時使用外層檔名區分，例如
`FLASH-SP1-Windows-0.1.2-<commit>.zip`；內部執行檔仍保留 `FLASH.exe`，
避免破壞既有驗證與程序辨識。短期 Artifact 不能取代永久 SP1 成品。
