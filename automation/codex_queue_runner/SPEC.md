# Codex Queue Runner 第三版安全契約

## 信任邊界

- `runner-src` 永遠 checkout `main`，只執行受信任 Runner；`target-repo` 永遠 checkout 任務 `BASE_COMMIT`。
- Agent 使用 `target-repo` 作為工作目錄，且其最後一步必須是釘選官方 Codex Action。跨 job 只傳遞選取 context 與受控 JSON 最終輸出。
- 自動事件在 `CODEX_QUEUE_ENABLED == 'true'` 時固定 live 且回寫；手動預設 dry-run，不使用 Secret、不寫 GitHub、不推送。

## 狀態與交棒

- 任務原始定義僅接受 `limaple0324`；狀態僅接受該擁有者或帶 `STATE_WRITER: CODEX_QUEUE_RUNNER` 的 `github-actions[bot]`。
- `CLAIMED` 必須同時綁定 workflow run id 與原始任務 comment id，並有租約與已完成 run 回收機制。
- 固定交棒：WORKER_A → REQUIREMENTS_AUDIT → CODE_REVIEW → TEST_VALIDATION → WAITING_REVIEW；稽核或審查 fail 回到 NEEDS_FIX／WORKER_A。

## 推送與範圍

- validate 從乾淨基準套用 Agent JSON patch，以 NUL Git 差異檢查所有路徑、模式、符號連結與子模組，再以固定 pytest argv 測試。
- push 僅使用一次性憑證、固定父 `git commit-tree` 和一般 push；不得 force、`--ff-only` 或重複建立已安全辨識的提交。
- push 後留言失敗時，後續租約回收可依 queue id、提交訊息、父提交與分支 head 補寫，不能再次推送。
