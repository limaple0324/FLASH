# 輔｜SP3 0.3.0｜驗收證據｜9cf06b4

驗收日期：2026-07-27（台灣時間）

## 完成

- 依現場 `smart_reconnect_state.json` 與執行紀錄確認：舊版仍保存十二個
  待重連匿名指紋，且舊狀態版本會被目前安全流程接受。
- 智慧重連狀態格式升級為第 4 版；第 1～3 版首次載入時一律清空並安全
  改寫為目前版本，不繼承待重連、登入後操作、重開或重試資格。
- 本版自己在同一匿名指紋連續兩幀確認斷線後建立的狀態，仍可跨程式
  重新啟動接續。
- 正常遊戲、手動登入、選線與選角在沒有本版斷線工作階段時維持只觀察。

## 本批驗證

- 智慧重連受影響範圍：`58 passed`。
- 變更模組編譯與 Git 差異檢查通過。
- 隔離 Windows 成品建立成功；來源摘要：
  `c0a67958f203bca9443f20ecbe91d1fe7e8aaddd27998634bf800e57c10d8608`。
- Windows 成品自我檢查：`8／8` 通過，檢查程序已正常結束。
- 已通過且本批未變更的其他功能沒有重複驗證。

## Windows 成品

- 完整累積成品資料夾：
  `C:\Users\USER\Documents\輔\SP1+SP2+SP3成品\FLASH-SP1+SP2+SP3-Windows-0.3.0-9cf06b4-snapshot`
- ZIP：
  `C:\Users\USER\Documents\輔\SP1+SP2+SP3成品\FLASH-SP1+SP2+SP3-Windows-0.3.0-9cf06b4-snapshot.zip`
- `FLASH.exe`：`44,901,583` bytes
- `FLASH.exe` SHA-256：
  `FE709FF7D408E5D4DCDF66FDCA8A43007677F02F6C3967CFF2F97052FC297F16`
- ZIP：`44,505,981` bytes
- ZIP SHA-256：
  `04BE5EC2EA27D17A37A2D5FCACCD7D510A7FEF6F188B1503B68D21D9912F0E63`
- 桌面 `輔.lnk` 仍指向正式累積安裝，正式安裝雜湊與本批成品一致。
- 驗證完成後沒有保持 `FLASH.exe` 執行。
- 本機第 3 版重連暫存已先保存為
  `smart_reconnect_state.v3-before-9cf06b4.json`，再由本版正式遷移為
  第 4 版；待重連、活動中及待重開數量均為 `0`。

## 待真實遊戲確認

- 由本版首次偵測真實單一角色斷線，確認只建立該角色的新版重連工作階段。
- 實際 14 視窗長時間運作與另一台乾淨 Windows 11 電腦驗證。
