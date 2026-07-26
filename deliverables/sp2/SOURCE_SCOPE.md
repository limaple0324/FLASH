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

## 第四批｜角色資料與安全唯讀快照

- SP2 程式提交：`3bb6ac40e2e5983d05ef21b8ae62f1adab87bfb5`
- 驗證：31 項角色、註冊與 SP1 啟動／自檢回歸測試通過；新增模組與
  `main.py` 編譯、差異檢查通過

### 本批 SP2 新增

- `domain/character_store.py`：角色等級／重要度的原子保存、有效備份與
  損壞隔離
- `services/character_view_service.py`：只依穩定角色身分合併 SP1 登記與
  SP2 角色資料，不用顯示名稱猜測
- 玩家快照不包含角色識別碼、視窗控制代號、程序資訊或健康狀態
- `main.py` 註冊 `CharacterStore` 與 `CharacterViewService`

### 本批明確沒有新增

- SP3 角色清單／詳細資料視窗
- 玩家可見視窗登記頁的新欄位或判定
- 命魂、魂器、寵物天賦、黑曜石、背包

## 第五批｜每角色靈魂石紀錄

- SP2 程式提交：`acee84c055a31d969bfeb06a578614a0ae5f1a19`
- 驗證：36 項靈魂石、註冊與 SP1 啟動／自檢回歸測試通過；新增模組與
  `main.py` 編譯、差異檢查通過

### 本批 SP2 新增

- `domain/soul_stone.py`：每角色獨立、不可修改的靈魂石文字紀錄
- `domain/soul_stone_store.py`：原子保存、重複身分拒絕與損壞隔離
- `services/soul_stone_service.py`：查詢、新增、修改與清除；先保存成功才
  替換記憶體狀態
- `main.py` 註冊受管理資料路徑下的 `SoulStoneStore` 與
  `SoulStoneService`

### 本批明確沒有新增

- SP3 靈魂石編輯視窗或角色詳細畫面
- 命魂與其他五個暫停領域
- 遊戲讀取或輸入操作

## 第六批｜角色詳細唯讀快照與安全選擇

- SP2 程式提交：`be94642ce677b3716ba152cbcc749a0cad7a0e6c`
- 驗證：52 項角色詳細、靈魂石、註冊與 SP1 啟動／自檢回歸測試通過；
  新增模組與 `main.py` 編譯、差異檢查通過

### 本批 SP2 新增

- `services/character_detail_view_service.py`：把已確認角色摘要與靈魂石組成
  不可修改的詳細快照
- `services/character_detail_choice_service.py`：顯示層不取得角色識別碼，
  仍能透過無參數命令精確選擇穩定身分
- `main.py` 註冊 `CharacterDetailViewService`

### 本批刻意調整舊整合設計

- 移除舊版 `CharacterDetailViewService` 對命魂服務的依賴
- 詳細快照明確不存在 `life_soul`、魂器、寵物、黑曜石與背包欄位

### 本批明確沒有新增

- SP3 角色清單、詳細資料或靈魂石編輯視窗
- 所有暫停領域與任何遊戲操作

## 第七批｜提醒卡顯示時間與到期規則

- SP2 程式提交：`04bdfc0e6222c37eaa1a7a175b0dd927fdb9644c`
- 驗證：53 項提醒卡與 SP1 啟動／自檢回歸測試通過；新增模組與
  `main.py` 編譯、差異檢查通過

### 本批 SP2 新增

- `services/card_display_settings_service.py`：驗證、原子保存與套用提醒卡
  顯示秒數；保存失敗時回復原設定
- `services/card_expiry_monitor.py`：可由呈現層排程器驅動的到期清除邏輯
- 卡片變更訂閱、退訂與只在真實變更時通知
- `main.py` 載入／註冊顯示設定；無效設定安全退回 30 秒

### 本批明確沒有新增

- SP3 主視窗排程接線、浮層或卡片視覺
- 真實遊戲事件產生提醒卡
- 所有暫停領域與遊戲輸入

## 第八批｜SP1 目標視窗到 SP2 唯讀狀態

- SP2 程式提交：`d3596823fad6aa5df33ad38d12c1f34f84e5f8a5`
- 驗證：32 項目標視窗事件、EventBus 與 SP1 啟動／自檢回歸測試通過；
  新增模組與 `main.py` 編譯、差異檢查通過

### 本批 SP2 新增

- `core/target_window_observation.py`：只保留 `configured`、`safe`、`code`
  的不可修改觀察事實
- `services/target_window_state_service.py`：透過 EventBus 保存最新執行期
  唯讀狀態並隔離無效事件／失敗監聽者
- `main.py` 在正常啟動與自我檢查流程發布 SP1 目標視窗檢查結果

### 安全證據

- 不傳視窗控制代號、程序代號、路徑、座標、像素或原始技術訊息
- 不呼叫鍵盤、滑鼠或任何遊戲輸入
- 未設定、多重匹配與其他不安全結果維持 `safe=False`

### 本批明確沒有新增

- SP3 玩家訊息、首頁刷新或任何 UI
- 真正遊戲活動／斷線／恢復事件轉換
- 所有暫停領域

## 第九批｜Windows 資源釋放

- SP2 程式提交：`978d89ef5e3d632d60d60d8d43b05ce14bd2713b`
- `LoggerService.close()` 會沖洗、關閉並移除程式擁有的 handlers
- `main.run()` 在其他結束清理完成後釋放 Logger，不改寫原本退出結果
- 12 項 Logger、生命週期與目標視窗事件測試、編譯及隔離自我檢查通過

## 第十批｜SP1＋SP2 累積 Windows 成品身分

- SP2 程式提交：`fecf3404c70b9b4d0dd3576dc5b02134b39876fa`
- 累積程式版本：SP2 `0.2.0`
- 雲端 artifact：`FLASH-SP1+SP2-Windows-<version>-<commit>-snapshot`
- `sp2_snapshot` 只允許來自 `sp2/completion-2026-07-26`
- 快照必須包含 `SP1+SP2累積快照說明.txt` 與完整 SHA-256 manifest
- 快照禁止包含安裝器、更新器或 `release/latest`／`release/sp1` 頻道資料
- 478 項完整測試、全原始碼編譯、隔離 SP2 0.2.0 自我檢查與差異檢查通過
- GitHub Actions run `30183396825`／artifact `8626301455` 成功
- ZIP SHA-256：
  `94D8FCF1BFC1584C3F68D05CFCACAC71F5B73C90BF36FB41E340E871C448F831`
- EXE SHA-256：
  `A55C308C4FD9405C4449089B4B48573D19EEA6ECE26842F2302BA70BDAC6F20D`
- bundle、封裝 8/8 自我檢查及 Windows 11 兩次 GUI 啟閉通過

### 本批明確保留

- SP1 0.1.3 獨立成品、雜湊與驗收證據不覆蓋
- SP2 自己的來源範圍與驗收證據保留在本目錄
- 玩家尚未接受的習慣自動判定門檻、跨組、提醒位與視窗登記建議稿不實作

## 第十一批｜已確認週期活動

- SP2 程式提交：`d8327ce`
- `domain/activity_schedule.py` 保存台灣時間週期、隔週規則、角色等級限制、
  共用一次語意與未知參與對象的失敗關閉狀態
- 13 條活動／每日要求接入 `ActivityProgressService`
- 15 項受影響測試與編譯、差異檢查通過

## 第十二批｜可解釋決策服務

- SP2 程式提交：`5580bcd`
- `decision/` 保存十層順序及提醒／建議／保持安靜三種輸出
- 未知證據、玩家取消、無新資訊、重複提醒與不適合打擾情境會保持安靜
- 安全／斷線、時限與損失風險優先；同層依剩餘時間、角色重要度及穩定
  識別排序
- 26 項受影響測試與編譯、差異檢查通過

## 第十三批｜玩家確認式活動順序記憶

- SP2 程式提交：`e7d9225`
- `habit/` 保存每日順序、例外日、七天觀察與第八天回顧狀態
- 玩家可採用、取消回顧、修改、暫停、恢復、移除觀察或全部清除
- 未確認觀察不會自動套用；`5/7` 等穩定門檻仍等待使用者定版
- 26 項受影響測試與編譯、差異檢查通過

### 第十一至十三批整批門檻

- 501 項完整測試通過
- 全部目前來源目錄與 `main.py` 編譯通過
- 隔離 SP2 0.2.0 自我檢查 8/8 通過
- `git diff --check` 通過
