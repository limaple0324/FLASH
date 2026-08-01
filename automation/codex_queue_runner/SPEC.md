# Codex Queue Runner｜固定安全契約

本文件定義 Issue #20 的自動化執行器邊界。任何實作、測試、審查與後續修改都不得弱化本契約。

## 1. 目的

由 GitHub Issue #19 的結構化任務驅動 Codex CLI，自動完成單一任務的認領、施工、直接測試、範圍驗證、提交、推送與結果回寫；無法安全處理時寫入 Issue #18 並停止。

## 2. 觸發

- `issue_comment`：只接受 Issue #19 的留言事件。
- `schedule`：定時補漏，只掃描 Issue #19。
- `workflow_dispatch`：僅供受控測試與人工補跑。
- 只有留言作者／執行者 `limaple0324` 可建立可執行任務。

## 3. 任務選取

一次工作流程最多選取一項任務，且必須同時符合：

- `STATUS` 為 `READY` 或 `NEEDS_FIX`。
- `ROLE` 是允許的自動角色。
- `SOURCE_ISSUE`、`BASE_COMMIT`、`TARGET_BRANCH`、`SCOPE`、`OWNED_FILES`、`FORBIDDEN`、`ACCEPTANCE`、`MINIMUM_TESTS` 均存在且格式有效。
- `TARGET_BRANCH` 不得為 `main`、`release/latest`、`release/sp1` 或任何 `release/` 正式頻道。
- 不得重複認領已存在 `CLAIMED`／`WAITING_REVIEW`／`VERIFIED`／`CLOSED` 的同一 `QUEUE_ID`。

## 4. 權限與憑證

- 自動施工流程不得具有合併 `main`、正式發布或修改正式頻道的能力。
- checkout 使用 `persist-credentials: false`。
- GitHub 寫入由固定、可審查的回寫／推送步驟執行，不把 GitHub 憑證交給 Codex 提示或輸出。
- OpenAI 認證只來自 `secrets.OPENAI_API_KEY`。
- 不得輸出、記錄、提交或傳遞任何秘密值。
- 未設定 `OPENAI_API_KEY` 時必須明確失敗，不得改用其他認證或請使用者把金鑰貼到 Issue。

## 5. Codex 執行

- 使用官方 Codex CLI 的非互動執行模式處理單一任務。
- 提示必須包含來源 Issue、PR、基準提交、目標分支、範圍、所有權、禁止事項、驗收與最小測試。
- 不允許 Codex自行接下一個任務。
- 設定明確逾時、最大輸出與單次執行限制。
- 同一 `QUEUE_ID` 同時間只能有一個工作流程執行。

## 6. Git 與分支

- 工作前確認遠端 `TARGET_BRANCH` 存在，且 HEAD 符合 `BASE_COMMIT`；不一致即阻塞。
- 不得重設、改寫歷史或 force push。
- 不得建立或修改 `main`、`release/latest`、`release/sp1`。
- 只允許一般 fast-forward push 到 `TARGET_BRANCH`。
- 沒有程式差異時不得建立空提交。

## 7. 修改範圍驗證

Codex 執行後、測試前後都必須比對差異：

- 所有修改、新增、刪除與重新命名的路徑都必須落在 `OWNED_FILES`。
- 任一 `FORBIDDEN` 路徑或模式被修改時立即失敗。
- 禁止修改工作流程本身、派工解析器、回寫器或安全契約，除非任務明確屬於 Issue #20 且所有權包含該檔案。
- 超出範圍時不得自動回復後繼續；必須寫入阻礙並停止，保留證據。

## 8. 測試

- 只執行 `MINIMUM_TESTS` 明確列出的命令。
- 一般施工任務不得執行完整回歸或 Windows 建置。
- 只有任務欄位明確為 `FULL_REGRESSION: YES`／`WINDOWS_BUILD: YES`，且角色與批次關卡允許時才能執行。
- 測試命令必須經允許清單與危險字元檢查，不得直接把任務文字交給無限制 shell。
- 測試失敗即停止，不建立提交。

## 9. 結果與狀態

成功：

1. 建立一個可追溯提交。
2. 一般 push 至 `TARGET_BRANCH`。
3. 回寫來源 PR／Issue 的提交、檔案與測試證據。
4. 回寫 #19：`STATUS: WAITING_REVIEW` 與 `NEXT_ROLE`。

失敗或阻塞：

1. 不推送未驗證程式。
2. 以固定格式寫入 #18。
3. 回寫 #19：`STATUS: BLOCKED`。
4. 不自動重試相同失敗方法。

## 10. 不可自動化的決策

遇到以下情況必須停止等待使用者：

- 產品規格衝突或缺少決策。
- 需要擴大 `OWNED_FILES` 或修改共用資料契約。
- 需要合併 `main`。
- 需要正式發布或推進正式頻道。
- 需要桌面／Windows 11／14 視窗實機驗收。
- 可能覆蓋使用者資料、正式設定或桌面正式版。

## 11. 啟用條件

自動化只有在以下條件全部完成後才可啟用：

1. Issue #20 實作完成。
2. 自動化直接測試通過。
3. 安全程式審查通過。
4. 使用無產品影響的測試任務完成端到端演練。
5. 使用者明確批准將自動化基礎設施合併到 `main`。
6. GitHub Repository Secret `OPENAI_API_KEY` 已由使用者自行設定。

合併到 `main` 不等於允許合併產品 PR 或正式發布。