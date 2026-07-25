# 輔｜SP1 獨立交付區域

此目錄是 SP1 的唯一獨立交付入口，不放入 SP2／SP3 功能，也不複製另一套
無法追蹤的原始碼。

## 目前來源

- GitHub：`https://github.com/limaple0324/FLASH.git`
- 分支：`sp1/completion-2026-07-25`
- 基準：`main@538bdbcffd32327cbd3cb32cea1b70cfd9d9e3c3`
- 最新功能提交：`f47e132d1d3677af9fa2952ee6dcd99e0d073918`
- 版本：SP1 0.1.2 工程驗證階段

## 目前來源驗證

- `5db5b5f` 本機完整基準：171 項測試、原始碼編譯、8 項來源自我檢查通過
- `689a186` 相容性修正：24 項受影響測試及 1 項防回歸測試通過
- GitHub Actions run #118：完整測試、來源自檢、EXE 建置、封裝 EXE 自檢、
  bundle 與雜湊驗證全部通過
- `git diff --check`：通過
- Win32 適配器與交易式更新器獨立重點覆核：沒有剩餘阻擋項目
- 驗證日期：2026-07-25（台灣時間）

最新 `689a186` Windows 工程快照已通過雲端完整工作與本機 Windows
PowerShell 5.1 包內驗證器，永久檔名為
`FLASH-SP1-Windows-0.1.2-689a186-snapshot.zip`，ZIP SHA-256 與 GitHub
artifact digest 均為
`30aca05eb2e8c84f10c58a86cc34af89cda8559b31989b8b6cb266e007b231fa`。
此快照沒有發布 `release/latest`。同一 ZIP 已在 Windows 11 build `26200`
以隔離資料目錄完成兩次啟動、正常關閉、重開、持久化、8/8 包裝自我檢查及
無主控台視窗驗收。這不代表乾淨的新 Windows 帳號、真正遊戲視窗、
背景／最小化、斷線重連、安全輸入、目標電腦更新或正式發布驗收已通過。

最新 SP1 獨立正式更新版為
`FLASH-SP1-Windows-0.1.2-f419e07-sp1-release.zip`。GitHub Actions run #120
已通過完整流程並發布到專用 `release/sp1`；本機 ZIP SHA-256 與 artifact
`8616031855` digest 均為
`746fc2c630b77e7d7e3d2db87089716cfe5ebf0fda37e5cf9010d264d5fd6b66`。
Windows 11 build `26200` 已實際由包內 `更新輔.cmd` 連線此通道，完成下載、
安裝前驗證、交易式套用與安裝後驗證。它不追蹤 `release/latest`，不會更新
成 SP2、SP3 或完整整合版。

最新完整安裝／更新版為
`FLASH-SP1-Windows-0.1.2-cee31ac-sp1-release.zip`。GitHub Actions run #123
的 183 項完整測試與所有建置／發布步驟通過；ZIP SHA-256 與 artifact
`8616207872` digest 均為
`40af7b7c6a8e8ab8c768cdf6aa5961cb82f1a28f6c7075204b316adbec52053a`。
Windows 11 已在隔離位置完成來源、暫存、安裝後三次成品驗證，並確認只建立
一個 `輔.lnk`，其目標、工作目錄與圖示均指向安裝後 `FLASH.exe`。

最新完整安全狀態版為
`FLASH-SP1-Windows-0.1.2-03db062-sp1-release.zip`。GitHub Actions run #124
的 184 項完整測試、來源／封裝 8/8 自我檢查與所有 Windows 建置／發布步驟
通過；ZIP SHA-256 與 artifact `8616418914` digest 均為
`e470f5c71b5c81a182d8d0532c1cfb2f00385233868735cdc797284fe99f097b`。
同提交的 Windows 11 包裝視窗已實際顯示完整自我檢查、保守主視窗狀態、
三項背景能力、角色資料、安全停用與紀錄位置。

最新真實桌面入口版為
`FLASH-SP1-Windows-0.1.2-ac1223a-sp1-release.zip`。GitHub Actions run #125
的 186 項完整測試、來源／封裝 8/8 自我檢查與全部 Windows 建置／發布步驟
通過；ZIP SHA-256 與 artifact `8616653423` digest 均為
`3854f49733dcd5d23f9a8452d0390f5aca31c7aaed1c89bc505f45240d38ff63`。
Windows 11 真實桌面安裝保留原有 `輔` 專案 junction，只建立
`啟動輔.lnk`，並通過捷徑屬性、EXE 雜湊、圖形啟動及正常關閉驗收。

最新真實背景擷取版為
`FLASH-SP1-Windows-0.1.2-960bacb-sp1-release.zip`。GitHub Actions run #127
的 188 項完整測試、來源／封裝 8/8 自我檢查與全部 Windows 建置／發布步驟
通過；ZIP SHA-256 與 artifact `8616874795` digest 均為
`14a6adba241defb0415d92ca66a48d3e1980c05204e5c7f3f4cfadeda13abe7d`。
14 個真實 Flash 視窗已通過部分／完全遮擋、非前景與最小化唯讀畫面擷取；
正式安裝亦已由 `更新輔.cmd` 交易式更新到同一提交。

最新本機功能來源 `2fae8bf` 已接入使用者確認的匿名啟動參數指紋；200 項
完整測試、原始碼編譯及 8/8 來源自我檢查通過。14 個真實 Flash 視窗各有
一個不同指紋，無效、缺失或重複身分一律拒絕，未輸出原始參數或傳送輸入。
成品來源 `49ee668` 的 GitHub Actions run #128 已完成 200 項測試、來源／
封裝 8/8 自檢及 `release/sp1` 發布。永久成品為
`FLASH-SP1-Windows-0.1.2-49ee668-sp1-release.zip`，artifact `8617136485`
與 ZIP SHA-256 均為
`40838dfff48e858079a479242aa670743e8f08b5ec0c5f3e7d73a5f01bd3852d`。
包裝指紋唯一選擇、正式交易式更新、圖形啟動與正常關閉均已通過。

最新本機來源 `f47e132` 新增可重複的目標桌面唯讀驗證器與封裝 EXE 專用
參數；209 項完整測試、編譯、8/8 來源自檢、真實 14/14 身分選擇及 14/14
非空白擷取通過，選錯 0 次。此提交尚未建立新的 Windows 成品。

## 後續成品紀錄

每個 Windows 工程快照必須留下可追溯的交付紀錄，至少包含：

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

## 交易式更新器首次遷移

舊版更新器只接受僅含 `FLASH.exe` 的單行 SHA-256；新版改為逐檔驗證整個
正式 payload。兩種格式刻意不相容，舊版不能安全地把自己直接升級成交易式
更新器。

交易式更新器首次發布時，玩家必須使用完整的正式 SP1 安裝包替換舊安裝，
不能用舊的 `更新輔.cmd` 完成這一次遷移。完成一次完整安裝後，保留包內固定
的 `更新輔.cmd`；日後即可由它把核心複製到 TEMP，再進行同一 commit 下載、
完整雜湊驗證、備份、原子替換、安裝後驗證及失敗回復。若固定啟動器的內容
日後需要改變，也必須再次使用完整安裝包，不得由正在執行的啟動器覆蓋自己。
