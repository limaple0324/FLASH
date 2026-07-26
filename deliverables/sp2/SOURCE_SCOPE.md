# SP2 來源範圍清單

## 第一批｜純資料模型與安全保存

- SP2 程式提交：`f41e70839b41bb47c9abe2cd3e710b852b5ad11b`
- 移植來源：`integration/sp2-sp3-sp35@bd685fbfd346de6cec32d9bbfc2184571dbf4ab0`
- 累積基底：SP1 `64ecfc23eb55b819058fc88f481a09cb0bbbc913`
- 驗證：96 項受影響測試通過；`domain/`、`workspace/`、`cards/` 編譯通過

### 本批 SP2 新增

- `domain/character.py`：穩定角色資料與重要度
- `domain/group.py`：組別與單組角色去重
- `domain/activity.py`：活動定義與重置規則
- `domain/status.py`：待命中／執行中／已完成三態
- `domain/progress.py`：活動開始、完成次數與台灣時間每日重置
- `domain/progress_store.py`：原子保存與損壞隔離
- `workspace/models.py`、`workspace/service.py`：工作區純資料與安全更新
- `cards/`：組別級卡片、最多三張、優先原因、顯示時間、生命週期、
  斷線／恢復歷史與原子保存
- `requirements.txt`：加入 Windows 所需 `tzdata`
- 14 份對應單元測試檔

### 本批明確沒有新增

- `main.py` 正常流程接線
- SP3 首頁、浮層或任何玩家畫面
- 遊戲視窗讀取、鍵盤、滑鼠或自動操作
- 魂器、寵物天賦、黑曜石、命魂、背包
- 玩家可見視窗登記頁
- 未確認的提醒區編號／綁定或寵物欄位

## 第二批｜純 SP2 服務層

- SP2 程式提交：`4d6373300563f14f9871c2d59ca8b5fcd795a282`
- 移植來源：`integration/sp2-sp3-sp35@bd685fbfd346de6cec32d9bbfc2184571dbf4ab0`
- 驗證：108 項核心與服務受影響測試通過；5 份新增服務／快照模組編譯通過

### 本批 SP2 新增

- `services/activity_progress_service.py`：活動定義、進度、完成與重置協調
- `services/card_history_service.py`：只保存斷線／恢復必要歷史
- `services/card_coordinator.py`：可見卡片與必要歷史的一致協調
- `cards/view_state.py`、`services/card_view_state_service.py`：提供 SP3
  將來可讀取、但不可反向修改 SP2 的唯讀快照
- 4 份服務測試檔，共 14 項新增純服務測試

### 本批刻意延後

- `main.py` 與 `build_services()` 註冊；下一批獨立接線與驗證
- 任何 SP3 畫面或遊戲輸入
- 第一批已列出的所有暫停領域與未確認內容

## 第三批｜累積正常流程服務註冊

- SP2 程式提交：`ae76d64c10f2bd3f827eda080d613f807aa8de7f`
- 驗證：51 項 SP2 註冊、服務與 SP1 啟動／自檢回歸測試通過；
  `main.py`、`services/`、`cards/`、`domain/`、`workspace/` 編譯通過

### 本批 SP2 新增

- 在已驗收 SP1 的 `main.py` 逐項註冊 `ActivityProgressStore`、
  `ActivityProgressService`、`WorkspaceService`、`CardHistoryStore`、
  `CardHistoryService`、`CardService`、`CardCoordinator` 與
  `CardViewStateService`
- 活動進度與卡片歷史固定保存於受管理 `data/` 目錄
- 損壞資料沿用 fail-safe 隔離並寫入警告紀錄
- 5 組正常流程註冊／共用實例測試

### 本批明確沒有新增

- SP3 HomeView 接線或任何 UI
- 遊戲讀取、鍵盤、滑鼠或自動操作
- 所有暫停領域與未確認內容
