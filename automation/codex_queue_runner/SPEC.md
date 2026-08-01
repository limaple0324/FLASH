# Codex Queue Runner 第二版安全契約

## 工作階段

1. `select_claim` 只讀取 Issue #19、核對來源與分支，以全域工作流程鎖寫入 `CLAIMED`。
2. `agent` 只有 `contents: read` 與 OpenAI 金鑰；使用釘選的官方 Codex 動作，輸出 patch、最終回報、事件與任務資料。
3. `validate` 從乾淨 `BASE_COMMIT` 套用 patch，以 NUL 分隔 Git 真實差異檢查範圍，再用固定 pytest argv 執行最小測試。
4. `push_writeback` 不持有 OpenAI 金鑰、不執行候選程式；它驗證雜湊與父提交，以 Git plumbing 建立單一提交並用一般 push 回寫。

## 固定禁止事項

- 不得修改或合併 `main`，不得操作任何 `release/` 分支、正式發布或自動合併。
- 不得使用 force push、`--ff-only`、`shell=True`、自由測試命令、未受控 MCP、`danger-full-access`、`full-auto` 或使用者 Codex 設定。
- 代理產物的符號連結、子模組、可執行檔模式、路徑穿越、絕對路徑與 `FORBIDDEN` 變更一律阻擋。

## 啟用

自動事件僅在 `CODEX_QUEUE_ENABLED == 'true'` 時執行。`workflow_dispatch` 預設乾跑且不回寫。PR #21 合併與安全審查通過前，不得執行 live 模式、使用 OpenAI 金鑰或推送任務分支。
