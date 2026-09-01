"""LINE 安全橋接設定視窗。"""
from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from tools.devspace_bridge.line_bridge_common import (
    PORT,
    Config,
    LineBridgeError,
    cloudflared_path,
    install_cloudflared,
    load_config,
    runtime_path,
    save_config,
)


class SetupWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("LINE 橋接設定")
        self.root.resizable(False, False)
        self.existing: Config | None = None
        try:
            self.existing = load_config()
        except LineBridgeError:
            pass

        frame = ttk.Frame(self.root, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text="LINE 安全橋接").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        ttk.Label(
            frame,
            text="目前只開放查詢、通知與控制入口；會操作遊戲的指令仍鎖定。",
            wraplength=520,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))

        ttk.Label(frame, text="頻道密鑰").grid(row=2, column=0, sticky="w", pady=4)
        self.secret = ttk.Entry(frame, width=64, show="●")
        self.secret.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="頻道存取權杖").grid(row=3, column=0, sticky="w", pady=4)
        self.token = ttk.Entry(frame, width=64, show="●")
        self.token.grid(row=3, column=1, sticky="ew", pady=4)

        self.config_state = tk.StringVar(
            value=f"LINE 金鑰：{'已設定' if self.existing else '尚未設定'}"
        )
        ttk.Label(frame, textvariable=self.config_state).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 2)
        )
        self.tunnel_state = tk.StringVar(
            value=f"對外連線元件：{'已安裝' if cloudflared_path().is_file() else '尚未安裝'}"
        )
        ttk.Label(frame, textvariable=self.tunnel_state).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=2
        )
        self.runtime_state = tk.StringVar(value="執行狀態：尚未啟動")
        ttk.Label(frame, textvariable=self.runtime_state, wraplength=520).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(2, 12)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="儲存設定", command=self.save).pack(side="left")
        ttk.Button(buttons, text="安裝對外連線元件", command=self.install_tunnel).pack(
            side="left", padx=8
        )
        ttk.Button(buttons, text="重新整理狀態", command=self.refresh).pack(side="left")
        ttk.Button(buttons, text="關閉", command=self.root.destroy).pack(side="right")

        ttk.Label(
            frame,
            text=(
                "密鑰只儲存在目前 Windows 使用者的本機資料夾，Windows 上使用系統資料保護加密。"
                "第一次啟動 LINE 橋接後，這裡可看到配對碼。"
            ),
            wraplength=520,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.refresh()

    def save(self) -> None:
        secret = self.secret.get().strip()
        token = self.token.get().strip()
        if not secret and self.existing:
            secret = self.existing.channel_secret
        if not token and self.existing:
            token = self.existing.access_token
        if not secret or not token:
            messagebox.showerror("LINE 橋接", "請輸入頻道密鑰與頻道存取權杖。")
            return
        config = Config(
            channel_secret=secret,
            access_token=token,
            allowed_user_ids=self.existing.allowed_user_ids if self.existing else (),
            port=self.existing.port if self.existing else PORT,
        )
        try:
            save_config(config)
        except Exception as exc:
            messagebox.showerror("LINE 橋接", f"儲存失敗：{exc}")
            return
        self.existing = config
        self.secret.delete(0, "end")
        self.token.delete(0, "end")
        self.config_state.set("LINE 金鑰：已設定")
        messagebox.showinfo("LINE 橋接", "設定已儲存。")

    def install_tunnel(self) -> None:
        self.tunnel_state.set("對外連線元件：下載與驗證中…")

        def worker() -> None:
            try:
                path = install_cloudflared()
                self.root.after(0, lambda: self._install_done(f"對外連線元件：已安裝｜{path.name}", None))
            except Exception as exc:
                self.root.after(0, lambda: self._install_done("對外連線元件：安裝失敗", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _install_done(self, text: str, error: str | None) -> None:
        self.tunnel_state.set(text)
        if error:
            messagebox.showerror("LINE 橋接", error)
        else:
            messagebox.showinfo("LINE 橋接", "對外連線元件已下載並通過雜湊驗證。")

    def refresh(self) -> None:
        path = runtime_path()
        if not path.is_file():
            self.runtime_state.set("執行狀態：尚未啟動")
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.runtime_state.set("執行狀態：狀態檔無法讀取")
            return
        active = payload.get("webhook_active")
        active_text = "已啟用" if active is True else ("未啟用" if active is False else "未知")
        parts = [
            "執行狀態：已啟動",
            f"LINE Webhook（事件回傳）：{active_text}",
            f"對外網址：{payload.get('webhook_url') or '本機模式'}",
        ]
        code = payload.get("pairing_code")
        if code:
            parts.append(f"初次配對碼：{code}（在 LINE 傳送：配對 {code}）")
        self.runtime_state.set("\n".join(parts))

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    SetupWindow().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
