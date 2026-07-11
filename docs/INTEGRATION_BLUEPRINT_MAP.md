# SP2／SP3／SP3.5 Integration Map

本文件將既有 Blueprint 對應至目前程式層次，不重新命名、不重新設計。

## SP2｜Brain（決策層）

| 已完成 Blueprint | 程式責任 | 預定模組 |
|---|---|---|
| 產品哲學 | 固定產品原則與不可違反規則 | `product/principles.py` |
| 工作區系統 | 工作區狀態、目前組別、目前活動 | `workspace/models.py`、`workspace/service.py` |
| 玩家旅程 | 開啟、選組、登入、執行、恢復流程 | `journey/models.py`、`journey/service.py` |
| 卡片系統 | 卡片身分、資訊層級、生命週期 | `cards/models.py`、`cards/service.py` |
| 工作區行為 | Focus、Priority、Context、Memory、Suggestion | `decision/signals.py`、`decision/service.py` |
| 產品規格／資訊流程 | 模組間事件與資料流 | `services/event_bus.py` 擴充事件，不改核心介面 |
| 角色進度 | 角色、組別、活動、進度、完成判定 | `domain/character.py`、`domain/group.py`、`domain/activity.py` |
| 狀態模型 | 待命中、執行中、已完成 | `domain/status.py` |
| 恢復 | 中斷、斷線、登入後接續 | `recovery/coordinator.py`，沿用 SP1 邊界 |
| 優先度／活動決策 | 時間、情境、進度、角色重要度 | `decision/priority.py` |
| 玩家習慣 | 學習、建議、可調整、不越界 | `habit/models.py`、`habit/service.py` |

## SP3｜Presentation（呈現層）

| 已完成 Blueprint | 程式責任 | 預定模組 |
|---|---|---|
| 產品呈現 | 主畫面資訊結構 | `ui/main_view.py` |
| 陪伴體驗 | 少操作、不打擾、當下相關 | `presentation/view_state.py` |
| 情感回饋 | 完成、提醒、恢復後回饋 | `presentation/messages.py` |
| 工作區呈現 | 目前組別、活動、下一步 | `ui/workspace_view.py` |
| 卡片呈現 | 右下角、最多三張、30 秒 | `ui/card_overlay.py` |
| 儀表板 | 長期彙整資訊 | `ui/dashboard_view.py`，非第一批整合 |

## SP3.5｜Companion（陪伴層）

| 已完成 Blueprint | 程式責任 | 預定模組 |
|---|---|---|
| 陪伴個性 | 安心、可靠、值得託付背後 | `companion/personality.py` |
| 主動程度 | 何時提醒、建議、保持安靜 | `companion/policy.py` |
| 語氣原則 | 簡潔、人性化、不像系統通知 | `companion/messages.py` |
| 信任邊界 | 不擅自行動、不捏造事實 | `companion/guardrails.py` |
| 長期默契 | 前期詢問、後續依習慣調整 | 與 `habit/service.py` 協作 |

## 現有 SP1 對接

| SP1 能力 | 提供給上層的資料 | 消費者 |
|---|---|---|
| 視窗註冊表 | 角色與視窗身分 | 角色／組別模型 |
| 視窗偵測 | 是否存在、是否安全 | 工作區、恢復、卡片 |
| 背景能力偵測 | 是否可讀取、是否可操作 | 執行能力判定 |
| Recovery | 恢復狀態與結果 | 恢復協調器、卡片 |
| Smart Reconnect | 斷線與重連結果 | 活動狀態、卡片、陪伴回饋 |
| Event Bus | 事件傳遞 | SP2 決策與 SP3 呈現 |
| Config／Path | 設定與持久化位置 | 所有新增 Store |
| Logger | 統一紀錄 | 所有整合模組 |

## 資料流

1. SP1 偵測事實。
2. Event Bus 發布事件。
3. SP2 更新角色、組別、活動與狀態。
4. Decision Service 決定提醒、建議或安靜。
5. SP3 建立 View State 與 Card。
6. SP3.5 套用陪伴語氣與信任邊界。
7. UI 只呈現結果，不自行做決策。

## 不納入第一批

- 真正遊戲背景輸入。
- 自動操作活動流程。
- 完整習慣學習。
- 儀表板完整頁面。
- 大規模主畫面重製。

以上項目必須在資料模型、測試與事件流穩定後再接入。