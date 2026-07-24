# 倉庫資料清查（2026-07-24）

## 清查基準

- 倉庫：`limaple0324/FLASH`
- `main`：`538bdbc`
- 整合分支：`integration/sp2-sp3-sp35`
- 清查起點：`568c0d4`
- 開放中的合併請求：無

本次只判斷檔案是否仍有程式、測試、建置、發布、診斷或既定產品決策用途。
「目前尚未由主程式呼叫」不等於無用；已對應 SP1／SP2／SP3／SP3.5 或
2026-07-24 使用者需求的骨架一律保留。

## 已移除

| 檔案 | 判定 |
|---|---|
| `__init__ (5).py`、`__init__ (6).py`、`__init__ (7).py` | 三個內容相同的誤上傳副本，不是套件入口，倉庫內無引用 |
| `CHANGELOG.md` | 只有 `# package`，沒有任何版本紀錄或引用 |
| 根目錄 `path_manager.py` | 舊啟動原型，已由 `main.py` 與 `config/path_manager.py` 完整取代，無引用 |
| `ROADMAP.md` | 舊 SP1 路線圖，內容已由 `SP1_VERIFICATION.md`、完整總整理與 `INTEGRATION_BOARD.md` 取代 |
| `tools/register_flash_auto_sync_task.ps1` | 舊式每 15 分鐘 Git 同步排程建立器，與目前單一「更新輔」流程衝突 |
| `tools/sync_desktop_from_github.ps1` | 舊式原始碼桌面同步器，未被建置、測試或正式發布流程使用 |

## 已確認保留

- `main.py`、`core/`、`config/`、`services/`：啟動、自我檢查、事件、持久化與服務組裝。
- `adapters/`：Windows 視窗、背景讀取與提醒卡工作區邊界；仍需 Windows 11 實機驗證。
- `domain/`、`workspace/`、`cards/`：SP2 資料、進度、工作區、卡片與歷史紀錄。
- `ui/`：首頁及提醒卡浮層呈現。視覺尚未定案的部分仍是已確認的替換式骨架。
- `core/window_binding.py`：雖尚未接入正式啟動流程，但已有安全重綁測試，直接對應斷線重連需求。
- `plugins/__init__.py`：保留 SP1 已固定的 Plugin 擴充邊界。
- `assets/`、`FLASH.spec`：固定藍底白色加號圖示與 Windows 成品建置。
- `.github/workflows/build-windows.yml`：測試、Windows 建置、雜湊驗證與 `release/latest` 發布。
- `tools/更新輔.cmd`、`tools/輔系統/`：唯一玩家更新入口與更新核心。
- `tools/檢查輔同步狀態.*`：用來辨識舊電腦上可能殘留的排程，不會建立排程。
- `scripts/windows_setup_sp1.ps1`、`tools/verify_windows_release.ps1`：來源環境與正式成品驗證。
- `tests/`：對應目前程式契約、Windows 命令檔及尚待接線的安全邊界。
- `PROJECT_CONTEXT.md`、`SP1_VERIFICATION.md`、`docs/`：交接、既定決策、需求與驗收依據。
- `ARCHITECTURE.md`：保留為 SP1 歷史工程基準，並已連到現行整合架構文件。

## 其他結果

- 沒有追蹤 `build/`、`dist/`、快取、日誌、設定檔或玩家資料。
- 兩個圖示資源都有建置與測試引用，不是重複垃圾檔。
- 遠端仍有歷史功能與驗證分支；部分含整合分支之外的獨立驗證提交。本次不刪，
  避免在正式整合與發布完成前失去追溯資料。
- 待確認的使用者需求第 3、8 項仍保留在需求文件，未因尚未實作而刪除或猜測。

## 後續規則

1. 新檔案必須能對應程式、測試、建置、診斷或正式需求。
2. 實驗副本不得以 `檔名 (數字)` 形式提交。
3. 玩家更新只維持「更新輔」單一入口，不重新建立定時 Git 同步。
4. 遠端歷史分支待整合里程碑正式合併與發布後，再另輪判斷是否刪除。
