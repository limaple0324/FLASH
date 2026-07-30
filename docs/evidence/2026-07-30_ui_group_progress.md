# 2026-07-30 介面與組別操作修正進度

## 目前範圍

- 修正共用卡片右上「設定／收合」按鈕壓到內容列。
- 修正單一角色「讀取角色 ID」不應被其他角色或無關快照失敗阻擋。
- 「可用組別無法更換」尚在定位，未找到足夠程式證據前不猜原因。

## 已定位程式碼

- `ui/home.py`：`HomeView._card()`，共用功能卡片控制按鈕位置。
- `ui/home.py`：`HomeView._select_group()`，可用組別下拉選單事件。
- `main.py`：`change_group()`，切換目前組別流程。
- `main.py`：`unique_window_for_group_entry()`、`read_role_id()`，角色 ID 讀取入口。

## 目前狀態

- 已修改 `ui/home.py`：新增功能卡片內容上方安全間距，避免右上「設定／收合」壓到第一列內容。
- 已修改 `main.py`：角色 ID 讀取改為檢查目前角色本身是否有唯一安全視窗，不再因其他角色或無關快照失敗直接拒絕。
- 已修改 `main.py`：切換組別時，若新組別視窗身分尚未完整，仍可切換目前組別畫面，但會清空同步與智慧重連授權並保持停用。
- 已新增受影響測試：`tests/test_home_sp3_layout.py`、`tests/test_main_window_layout.py`、`tests/test_main_input_sync.py`。
- 已通過局部受影響測試：`tests/test_home_sp3_layout.py`、`tests/test_main_window_layout.py`、`tests/test_main_input_sync.py`，共 67 項。
- 已通過組別相關測試：`tests/test_main_group_configuration_transfer.py`、`tests/test_main_group_selection_registration.py`、`tests/test_group_configuration_service.py`、`tests/test_sync_scope_service.py`，共 32 項。
- `git diff --check` 通過，僅有既有換行格式提醒。
- 尚未提交、推送、送審、雲端建置或桌面同步。

## Codex 審查回覆修正

- 修正 `main.py`：切換組別失敗回復時，若舊組別身分無法重新綁定，會清空操作授權後重新開啟操作閘門，避免同步與智慧重連後續取不到閘門。
- 修正 `ui/home.py`：切換到身分尚未完整的組別時，成功結果中的警告訊息會顯示在組別頁訊息列。
- 新增回歸測試：`tests/test_main_input_sync.py`、`tests/test_home_sp3_layout.py`。
- 已通過受影響測試：`tests/test_home_sp3_layout.py`、`tests/test_main_input_sync.py`、`tests/test_main_window_layout.py`，共 69 項。
- 已通過組別相關測試：`tests/test_main_group_configuration_transfer.py`、`tests/test_main_group_selection_registration.py`、`tests/test_group_configuration_service.py`、`tests/test_sync_scope_service.py`，共 32 項。
- `git diff --check` 通過，僅有既有換行格式提醒。
- 尚未推送重新送審、雲端建置或桌面同步。
