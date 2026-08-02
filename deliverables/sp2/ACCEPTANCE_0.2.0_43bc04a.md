# 輔｜SP2 0.2.0｜驗收證據｜43bc04a

驗收日期：2026-07-26（台灣時間）

更正：下列歷史成品曾使用未經玩家確認的「靈魂石」名稱；現行正式需求是
角色「備註」，SP3 正常流程已停止註冊及呈現舊欄位。此文件保留原成品
追溯事實，不把舊名稱延續為產品規格。

## 一、交付身分

- 分支：`sp2/completion-2026-07-26`
- 累積來源提交：
  `43bc04a7071dd4b6298ae050050e57d1620a9450`
- 程式版本：`0.2.0`
- 階段：`SP2`
- 成品種類：`sp2_snapshot`
- SP2 獨立交付：本文件、`README.md`、`SOURCE_SCOPE.md` 與專屬證據 ZIP
- 累積交付：`FLASH-SP1+SP2-Windows-0.2.0-43bc04a-snapshot.zip`

SP2 獨立交付是本層來源範圍、規格、測試與驗收證據，不複製第二套會漂移
的可執行原始碼。玩家可執行程式仍是明確標示的 SP1＋SP2 累積成品。

## 二、來源門檻

- 501 項完整自動測試通過。
- 全部目前來源目錄與 `main.py` 編譯通過。
- 隔離 SP2 0.2.0 來源自我檢查 8/8 通過。
- `git diff --check` 通過。

已包含：

- SP1 已驗收基礎。
- SP2 角色、組別、活動進度、工作區、提醒卡、角色清單／詳細資料與靈魂石。
- 已確認的 100／120／160 週期活動目錄、每日 00:00 重置、隔週規則、
  共用神秘考官、160 世界 BOSS 及 120／160 魔兵限制。
- 可解釋的提醒／建議／保持安靜決策服務。
- 七天觀察、第八天等待玩家確認的活動順序記憶基礎。

## 三、雲端 Windows 成品

- GitHub Actions run：`30184919300`
- Workflow 結果：`success`
- Artifact ID：`8626722484`
- Artifact：
  `FLASH-SP1+SP2-Windows-0.2.0-43bc04a-snapshot`
- GitHub artifact digest／下載 ZIP SHA-256：
  `9E4B266FFCD3B3928C56402D20EF635B6E9140377949DF9C8A87A748117588D4`
- `FLASH.exe` SHA-256：
  `5C2CA4936E43F2C7118B20C5C9B73FACD1D2F9702D0A5F07679A10A0A01FC0AC`

包內 `BUILD_INFO.txt` 已核對 `version=0.2.0`、`milestone=SP2`、
`build_kind=sp2_snapshot`、正確分支、完整提交與 run ID。包內
`SHA256SUMS.txt` 與驗證器通過；成品不包含更新器或正式發布頻道。

## 四、Windows 11 實機證據

- 隔離驗收位置：
  `C:\Users\USER\Documents\輔\SP2驗收\20260726-1045-43bc04a`
- 封裝版 `self_check.json`：
  `version=0.2.0`、`sprint=SP2`、`self_check_passed=true`，8 項皆通過。
- 第一次啟動：只找到 1 個屬於指定 `43bc04a` EXE 的「輔」視窗。
- 第一次正常關閉：殘留指定成品視窗 0。
- 第二次啟動：只找到 1 個屬於指定 `43bc04a` EXE 的「輔」視窗。
- 第二次正常關閉：殘留指定成品視窗 0。
- 驗收過程未操作 14 個 Flash 遊戲視窗。

## 五、桌面最新進度安裝

- 桌面捷徑：`C:\Users\USER\Desktop\啟動輔.lnk`
- 最新安裝位置：
  `C:\Users\USER\AppData\Local\Programs\輔\SP1+SP2\FLASH.exe`
- 安裝後 EXE SHA-256：
  `5C2CA4936E43F2C7118B20C5C9B73FACD1D2F9702D0A5F07679A10A0A01FC0AC`
- 捷徑 TargetPath、WorkingDirectory 與 IconLocation 均已切換到
  `SP1+SP2` 新位置。
- 從桌面捷徑實際啟動只出現 1 個指定安裝版「輔」視窗；正常關閉後殘留 0。
- 原 `C:\Users\USER\AppData\Local\Programs\輔\SP1\FLASH.exe` 保留，
  沒有被新累積版覆蓋。
- 舊捷徑與更新紀錄備份於：
  `C:\Users\USER\AppData\Local\Programs\輔系統\更新交易\20260726-1050-SP1+SP2-0.2.0-43bc04a`

## 六、不得擴大宣稱

- 真正活動地圖名稱、各活動精確完成時機與參與角色範圍仍需隨實際畫面補足。
- 自動習慣穩定門檻、跨組同步、提醒位、玩家可見登記頁仍待確認。
- 魂器、寵物天賦、黑曜石、命魂與背包維持暫停。
- 乾淨帳號／另一台電腦及完整真正遊戲活動情境依使用者決定延後。
- 此成品是 SP1＋SP2 累積工程快照，不是 `release/latest` 正式發布。
