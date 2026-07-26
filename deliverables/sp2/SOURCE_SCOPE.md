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
