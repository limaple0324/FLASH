# Integration Architecture Baseline

本文件記錄 SP2、SP3、SP3.5 整合前的現有程式基準與固定接入邊界。

## 一、現有結構

### 1. 啟動入口

- `main.py`
- 負責建立服務、執行啟動流程、自我檢查、視窗偵測與桌面介面。
- 現有 `create_main_window()` 是 SP1 工程驗證畫面，不作為 SP2／SP3 商業邏輯容器。

### 2. 基礎核心

- `core/bootstrap.py`
  - 統一啟動流程。
  - 讀取設定、發布啟動事件、執行自我檢查。
  - 已存在 `workspace_enabled` 設定，可作為工作區逐步接入開關。

- `core/sp1_boundaries.py`
  - 固定 Recovery、Smart Reconnect、External Adapter 邊界。
  - 遊戲特定行為應放在 Adapter 或 Plugin，不修改 SP1 穩定契約。

- `core/window_registry.py`
  - 管理角色與視窗身分。
  - 可作為 SP2 角色資料與實際遊戲視窗之間的連接點。

- `core/window_registry_store.py`
  - 保存角色視窗註冊資料。
  - 不直接承擔活動、習慣或陪伴決策資料。

### 3. 共用服務

- `services/app_context.py`
  - 現有服務容器。
  - 新增的工作區、活動進度、卡片與陪伴決策服務，應透過此處註冊與取得。

- `services/event_bus.py`
  - 現有事件發布機制。
  - SP1 偵測結果應轉為事件，再由 SP2／SP3 消費，避免核心層直接操作介面。

- `services/logger_service.py`
  - 統一紀錄。
  - 所有整合模組沿用，不另建平行紀錄系統。

### 4. 平台與遊戲適配

- `adapters/windows_window.py`
- `adapters/windows_background_capture.py`
- `adapters/background_capability.py`

以上模組維持平台能力責任，只提供偵測或操作結果，不加入玩家旅程、活動優先度或陪伴文案。

### 5. 設定與資料路徑

- `config/config_manager.py`
- `config/path_manager.py`

所有新增設定與持久化資料必須沿用現有設定管理與資料路徑，不在專案內建立散落的絕對路徑。

## 二、固定分層

整合後維持以下責任：

1. **SP1 Engine（引擎）**
   - 視窗、背景能力、恢復、重連、事件、紀錄、持久化基礎。

2. **SP2 Brain（決策層）**
   - 角色、組別、活動、進度、狀態、習慣、優先度與決策。

3. **SP3 Presentation（呈現層）**
   - 工作區、卡片、主畫面、回饋與陪伴呈現。

4. **SP3.5 Companion（陪伴層）**
   - 個性、信任、主動程度、提醒語氣與安靜原則。

## 三、禁止跨界

- 不把 SP2 決策直接寫入 Adapter。
- 不把 SP3 文案直接寫入 Recovery 或 Reconnect 核心。
- 不讓主視窗直接判斷活動優先度。
- 不讓卡片自行修改角色或活動狀態。
- 不更改 SP1 已固定的 Recovery、Reconnect、External Adapter 契約。
- 不在 `main.py` 集中堆疊所有新功能。

## 四、正式接入點

### 接入點 A｜服務註冊

在 `build_services()` 逐步註冊 SP2／SP3 服務，保留現有 SP1 服務建立順序。

### 接入點 B｜啟動完成

在 `Bootstrap.start()` 完成後載入產品狀態，但不影響自我檢查結果。

### 接入點 C｜事件轉換

將視窗、斷線、恢復、活動完成等結果發布為事件，再交由決策層處理。

### 接入點 D｜呈現入口

將 `create_main_window()` 逐步拆為獨立 View，不讓 UI 繼續集中在 `main.py`。

### 接入點 E｜持久化

角色視窗註冊沿用現有 Registry；組別、活動進度與習慣使用獨立 Store，並共同使用 `PathManager`。

## 五、第一批低風險方向

- 產品名稱由「FLASH SP1」調整為以「輔」為主。
- 建立純資料模型，不接遊戲操作。
- 建立三態：待命中、執行中、已完成。
- 建立組別與活動資料骨架。
- 建立單元測試後，再接入主視窗。

此基準一旦提交，後續整合不得繞過以上邊界。