from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
from PIL import Image, ImageTk

from .config_store import ConfigStore, LOG_DIR
from .models import AppConfig, CROP_BY_LABEL, CROP_OPTIONS, Profile
from .scheduler import ProfileScheduler
from .win32_api import (
    BackgroundWindowSession,
    WindowInfo,
    enumerate_windows,
    get_window_pid,
    get_window_title,
    is_running_as_admin,
    is_window,
)


class ProfileDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        profiles: list[Profile],
        profile: Profile | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("編輯角色" if profile else "新增角色")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.profiles = profiles
        self.original = profile
        self.result: Profile | None = None

        self.path_var = tk.StringVar(value=profile.shortcut_path if profile else "")
        self.crop_var = tk.StringVar(value=(profile.crop.label if profile else CROP_OPTIONS[0].label))
        self.quantity_var = tk.IntVar(value=profile.quantity if profile else 16)
        self.enabled_var = tk.BooleanVar(value=profile.enabled if profile else True)

        body = ttk.Frame(self, padding=16)
        body.grid(sticky="nsew")
        ttk.Label(body, text="遊戲捷徑或 EXE").grid(row=0, column=0, sticky="w", pady=(0, 5))
        path_entry = ttk.Entry(body, textvariable=self.path_var, width=58)
        path_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(body, text="瀏覽…", command=self._browse).grid(row=1, column=1)

        ttk.Label(body, text="作物（每個角色只能選一種）").grid(
            row=2, column=0, sticky="w", pady=(14, 5)
        )
        ttk.Combobox(
            body,
            textvariable=self.crop_var,
            values=[option.label for option in CROP_OPTIONS],
            state="readonly",
            width=32,
        ).grid(row=3, column=0, sticky="w")

        ttk.Label(body, text="本輪最多新種格數").grid(
            row=4, column=0, sticky="w", pady=(14, 5)
        )
        ttk.Spinbox(body, from_=1, to=16, textvariable=self.quantity_var, width=8).grid(
            row=5, column=0, sticky="w"
        )
        ttk.Checkbutton(body, text="啟用此角色", variable=self.enabled_var).grid(
            row=6, column=0, sticky="w", pady=(14, 0)
        )

        buttons = ttk.Frame(body)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="儲存", command=self._save).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._save())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(50, path_entry.focus_set)
        self.wait_visibility()
        self._center(parent)

    def _center(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="選擇角色的遊戲捷徑或 EXE",
            filetypes=[
                ("遊戲捷徑或程式", "*.lnk *.exe"),
                ("捷徑", "*.lnk"),
                ("執行檔", "*.exe"),
                ("所有檔案", "*.*"),
            ],
        )
        if path:
            self.path_var.set(path)

    def _save(self) -> None:
        path = Path(self.path_var.get().strip())
        if not path.exists() or path.suffix.casefold() not in {".lnk", ".exe"}:
            messagebox.showerror("資料不完整", "請選擇存在的 .lnk 捷徑或 .exe 檔案。", parent=self)
            return
        shortcut_name = path.stem
        for item in self.profiles:
            if item is not self.original and item.shortcut_name.casefold() == shortcut_name.casefold():
                messagebox.showerror(
                    "名稱重複",
                    "捷徑名稱用來識別角色，因此每個角色的捷徑檔名必須不同。",
                    parent=self,
                )
                return
        try:
            quantity = int(self.quantity_var.get())
        except (TypeError, ValueError):
            quantity = 0
        if not 1 <= quantity <= 16:
            messagebox.showerror("數量錯誤", "新種格數必須介於 1 到 16。", parent=self)
            return
        crop = CROP_BY_LABEL[self.crop_var.get()]
        if self.original:
            result = self.original
            result.shortcut_path = str(path)
            result.shortcut_name = shortcut_name
            result.crop_key = crop.key
            result.quantity = quantity
            result.enabled = bool(self.enabled_var.get())
        else:
            result = Profile(
                shortcut_path=str(path),
                shortcut_name=shortcut_name,
                crop_key=crop.key,
                quantity=quantity,
                enabled=bool(self.enabled_var.get()),
            )
        self.result = result
        self.destroy()


class WindowPicker(tk.Toplevel):
    def __init__(self, parent: tk.Misc, current_hwnd: int = 0) -> None:
        super().__init__(parent)
        self.title("綁定遊戲視窗")
        self.geometry("1160x650")
        self.minsize(980, 560)
        self.transient(parent)
        self.grab_set()
        self.current_hwnd = current_hwnd
        self.result: WindowInfo | None = None
        self.filter_var = tk.StringVar(value="Flash")
        self.windows: list[WindowInfo] = []
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.preview_token = 0
        self.preview_results: queue.Queue[
            tuple[int, Image.Image | None, str, str]
        ] = queue.Queue()
        self.preview_info = tk.StringVar(
            value="請在左側選擇視窗，再從預覽左上角確認角色名稱。"
        )

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        top = ttk.Frame(body)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="篩選標題：").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.filter_var, width=30)
        entry.pack(side="left", padx=(0, 8))
        ttk.Button(top, text="重新整理", command=self._refresh).pack(side="left")
        ttk.Label(
            body,
            text="左側逐一選擇 Flash 視窗，右側會顯示背景預覽；直接查看遊戲左上角角色名稱，不必猜 PID／HWND。",
            foreground="#555555",
        ).pack(anchor="w", pady=(0, 8))

        content = ttk.Panedwindow(body, orient="horizontal")
        content.pack(fill="both", expand=True)
        list_frame = ttk.Frame(content, padding=(0, 0, 8, 0))
        preview_frame = ttk.Frame(content, padding=(8, 0, 0, 0))
        content.add(list_frame, weight=2)
        content.add(preview_frame, weight=3)

        ttk.Label(list_frame, text="已開啟的 Flash 視窗", font=("Microsoft JhengHei UI", 10, "bold")).pack(
            anchor="w", pady=(0, 5)
        )
        list_area = ttk.Frame(list_frame)
        list_area.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            list_area,
            font=("Microsoft JhengHei UI", 10),
            activestyle="dotbox",
            exportselection=False,
        )
        scrollbar = ttk.Scrollbar(list_area, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.listbox.bind("<Double-Button-1>", lambda _event: self._choose())
        self.listbox.bind("<<ListboxSelect>>", self._preview_selected)
        self.filter_var.trace_add("write", lambda *_args: self._refresh())

        preview_header = ttk.Frame(preview_frame)
        preview_header.pack(fill="x", pady=(0, 5))
        ttk.Label(
            preview_header,
            text="角色畫面預覽",
            font=("Microsoft JhengHei UI", 10, "bold"),
        ).pack(side="left")
        ttk.Button(preview_header, text="重新擷取預覽", command=self._preview_selected).pack(
            side="right"
        )
        self.preview_label = ttk.Label(
            preview_frame,
            text="選擇左側視窗後會在這裡顯示遊戲畫面",
            anchor="center",
            relief="sunken",
            background="#111827",
            foreground="white",
        )
        self.preview_label.pack(fill="both", expand=True)
        ttk.Label(
            preview_frame,
            textvariable=self.preview_info,
            wraplength=610,
            foreground="#374151",
        ).pack(fill="x", pady=(7, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="綁定選取視窗", command=self._choose).pack(
            side="right", padx=(0, 8)
        )
        self.bind("<Escape>", lambda _event: self.destroy())
        self._refresh()
        self.after(100, self._poll_preview_results)
        self.after(50, entry.focus_set)

    def _refresh(self) -> None:
        needle = self.filter_var.get().strip().casefold()
        self.windows = [
            info
            for info in enumerate_windows()
            if not needle or needle in info.title.casefold()
        ]
        self.listbox.delete(0, "end")
        selected = None
        for index, info in enumerate(self.windows):
            self.listbox.insert("end", info.display)
            if info.hwnd == self.current_hwnd:
                selected = index
        if selected is not None:
            self.listbox.selection_set(selected)
            self.listbox.see(selected)
        elif self.windows:
            self.listbox.selection_set(0)
            self.listbox.see(0)
        self.after_idle(self._preview_selected)

    def _preview_selected(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection or selection[0] >= len(self.windows):
            self.preview_photo = None
            self.preview_label.configure(
                image="", text="沒有可預覽的視窗", background="#111827"
            )
            self.preview_info.set("請先在左側選擇一個 Flash 視窗。")
            return
        info = self.windows[selection[0]]
        self.preview_token += 1
        token = self.preview_token
        state = "最小化" if info.minimized else "背景／顯示中"
        self.preview_label.configure(image="", text="正在擷取背景預覽…")
        self.preview_info.set(
            f"{state}｜PID {info.pid}｜HWND 0x{info.hwnd:X}｜請看預覽左上角角色名稱"
        )

        def worker() -> None:
            preview: Image.Image | None = None
            details = ""
            error = ""
            try:
                with BackgroundWindowSession(info.hwnd) as session:
                    frame = session.capture()
                    if frame is None and session.promote_offscreen():
                        frame = session.capture()
                    if frame is None:
                        raise OSError("無法取得此視窗的背景畫面")
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    preview = Image.fromarray(rgb)
                    preview.thumbnail((620, 440), Image.Resampling.LANCZOS)
                    details = (
                        f"{state}｜PID {info.pid}｜HWND 0x{info.hwnd:X}｜"
                        f"原始畫面 {frame.shape[1]}×{frame.shape[0]}｜{session.capture_method}"
                    )
            except Exception as exc:
                error = f"預覽失敗：{type(exc).__name__}: {exc}"

            self.preview_results.put((token, preview, details, error))

        threading.Thread(target=worker, name="window-preview", daemon=True).start()

    def _poll_preview_results(self) -> None:
        try:
            while True:
                token, preview, details, error = self.preview_results.get_nowait()
                if token != self.preview_token:
                    continue
                if error or preview is None:
                    self.preview_photo = None
                    self.preview_label.configure(image="", text=error or "無法顯示預覽")
                    self.preview_info.set(
                        "可按「重新擷取預覽」再試一次，或直接讓遊戲視窗離開最小化。"
                    )
                    continue
                self.preview_photo = ImageTk.PhotoImage(preview, master=self)
                self.preview_label.configure(image=self.preview_photo, text="")
                self.preview_info.set(details + "｜請以左上角角色名稱確認")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll_preview_results)

    def _choose(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            messagebox.showwarning("尚未選擇", "請先選擇一個遊戲視窗。", parent=self)
            return
        self.result = self.windows[selection[0]]
        self.destroy()


class MainWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("魔力莊園定時助手 1.0.1")
        self.root.geometry("1180x760")
        self.root.minsize(960, 650)
        self.root.option_add("*Font", ("Microsoft JhengHei UI", 10))
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.store = ConfigStore()
        self.config = self.store.load()
        self.scheduler = ProfileScheduler(self.config, self.store, self._queue_event)

        self._build_style()
        self._build_ui()
        self._refresh_profiles()
        self._poll_events()
        self._update_clock()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=30, font=("Microsoft JhengHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft JhengHei UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft JhengHei UI", 18, "bold"))
        style.configure("Hint.TLabel", foreground="#4b5563")
        style.configure("Start.TButton", font=("Microsoft JhengHei UI", 11, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="魔力莊園定時助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="新增角色並選擇捷徑、作物與格數 → 綁定目前遊戲視窗 → 按全域開始。成功後每 60 分鐘執行；介面不可用則每 3 分鐘重試。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 12))

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 9))
        self.add_button = ttk.Button(toolbar, text="新增角色", command=self._add_profile)
        self.edit_button = ttk.Button(toolbar, text="編輯角色", command=self._edit_profile)
        self.delete_button = ttk.Button(toolbar, text="刪除角色", command=self._delete_profile)
        self.bind_button = ttk.Button(toolbar, text="綁定視窗", command=self._bind_window)
        self.test_button = ttk.Button(toolbar, text="測試背景擷取", command=self._test_capture)
        self.toggle_button = ttk.Button(toolbar, text="啟用／停用", command=self._toggle_profile)
        for button in (
            self.add_button,
            self.edit_button,
            self.delete_button,
            self.bind_button,
            self.test_button,
            self.toggle_button,
        ):
            button.pack(side="left", padx=(0, 7))

        self.stop_button = ttk.Button(toolbar, text="停止全部", command=self._stop)
        self.start_button = ttk.Button(
            toolbar, text="開始全部", command=self._start, style="Start.TButton"
        )
        self.retry_button = ttk.Button(toolbar, text="選取角色立即重試", command=self._retry_now)
        self.stop_button.pack(side="right")
        self.start_button.pack(side="right", padx=(0, 7))
        self.retry_button.pack(side="right", padx=(0, 7))

        columns = ("enabled", "name", "window", "crop", "quantity", "status", "next")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", selectmode="browse")
        headings = {
            "enabled": "啟用",
            "name": "捷徑／角色",
            "window": "綁定視窗",
            "crop": "作物",
            "quantity": "新種格數",
            "status": "狀態",
            "next": "下次執行",
        }
        widths = {
            "enabled": 58,
            "name": 145,
            "window": 190,
            "crop": 165,
            "quantity": 78,
            "status": 330,
            "next": 100,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=50, anchor="center")
        self.tree.column("name", anchor="w")
        self.tree.column("window", anchor="w")
        self.tree.column("crop", anchor="w")
        self.tree.column("status", anchor="w")
        tree_scroll = ttk.Scrollbar(outer, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="top", fill="both", expand=True)
        tree_scroll.place(in_=self.tree, relx=1, rely=0, relheight=1, anchor="ne")
        self.tree.bind("<Double-Button-1>", lambda _event: self._edit_profile())

        log_header = ttk.Frame(outer)
        log_header.pack(fill="x", pady=(12, 5))
        ttk.Label(log_header, text="執行紀錄", font=("Microsoft JhengHei UI", 10, "bold")).pack(
            side="left"
        )
        ttk.Button(log_header, text="開啟紀錄資料夾", command=self._open_logs).pack(side="right")
        self.log_text = tk.Text(
            outer,
            height=9,
            wrap="word",
            state="disabled",
            bg="#101827",
            fg="#dbeafe",
            insertbackground="white",
            font=("Consolas", 9),
        )
        self.log_text.pack(fill="x")

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        self.global_status = ttk.Label(footer, text="排程已停止")
        self.global_status.pack(side="left")
        admin_text = "系統管理員權限：是" if is_running_as_admin() else "系統管理員權限：否（若遊戲以管理員執行，背景訊息可能被阻擋）"
        ttk.Label(footer, text=admin_text, style="Hint.TLabel").pack(side="right")

    def _queue_event(self, kind: str, payload: object) -> None:
        self.events.put((kind, payload))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind in {"profiles", "states"}:
                    self._refresh_profiles()
                elif kind == "running":
                    self._update_controls()
        except queue.Empty:
            pass
        self.root.after(150, self._poll_events)

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _selected_profile(self, warn: bool = True) -> Profile | None:
        selection = self.tree.selection()
        if not selection:
            if warn:
                messagebox.showwarning("尚未選擇", "請先選擇一個角色。", parent=self.root)
            return None
        profile_id = selection[0]
        return next((item for item in self.config.profiles if item.id == profile_id), None)

    @staticmethod
    def _countdown(due: datetime | None, running: bool) -> str:
        if running:
            return "執行中"
        if due is None:
            return "—"
        seconds = max(0, int((due - datetime.now()).total_seconds()))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _refresh_profiles(self) -> None:
        selection = self.tree.selection()
        _, states = self.scheduler.snapshot()
        existing = set(self.tree.get_children())
        configured = {profile.id for profile in self.config.profiles}
        for item_id in existing - configured:
            self.tree.delete(item_id)
        for profile in self.config.profiles:
            state = states.get(profile.id)
            valid_window = is_window(profile.window_hwnd)
            window_text = (
                f"{profile.window_title} / PID {profile.window_pid}"
                if valid_window
                else ("綁定已失效" if profile.window_hwnd else "尚未綁定")
            )
            status = "已停用" if not profile.enabled else (state.status if state else "待機")
            values = (
                "是" if profile.enabled else "否",
                profile.shortcut_name,
                window_text,
                profile.crop.label,
                profile.quantity,
                status,
                self._countdown(state.next_due if state else None, state.running if state else False),
            )
            if self.tree.exists(profile.id):
                self.tree.item(profile.id, values=values)
            else:
                self.tree.insert("", "end", iid=profile.id, values=values)
        if selection and self.tree.exists(selection[0]):
            self.tree.selection_set(selection[0])
            self.tree.focus(selection[0])
        elif len(self.config.profiles) == 1:
            only_id = self.config.profiles[0].id
            self.tree.selection_set(only_id)
            self.tree.focus(only_id)
            self.tree.see(only_id)
        self._update_controls()

    def _update_clock(self) -> None:
        self._refresh_profiles()
        self.root.after(1000, self._update_clock)

    def _update_controls(self) -> None:
        running, _ = self.scheduler.snapshot()
        self.global_status.configure(text="排程執行中" if running else "排程已停止")
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        # Profile mutations can invalidate a window while a worker is clicking.
        mutation_state = "disabled" if running else "normal"
        for button in (
            self.add_button,
            self.edit_button,
            self.delete_button,
            self.bind_button,
            self.test_button,
        ):
            button.configure(state=mutation_state)
        self.retry_button.configure(state="normal" if running else "disabled")

    def _add_profile(self) -> None:
        dialog = ProfileDialog(self.root, self.config.profiles)
        self.root.wait_window(dialog)
        if dialog.result:
            self.config.profiles.append(dialog.result)
            self.store.save(self.config)
            self.scheduler.sync_profiles()
            self._refresh_profiles()
            self.tree.selection_set(dialog.result.id)
            self.tree.focus(dialog.result.id)
            self.tree.see(dialog.result.id)

    def _edit_profile(self) -> None:
        running, _ = self.scheduler.snapshot()
        if running:
            return
        profile = self._selected_profile()
        if not profile:
            return
        dialog = ProfileDialog(self.root, self.config.profiles, profile)
        self.root.wait_window(dialog)
        if dialog.result:
            self.store.save(self.config)
            self.scheduler.sync_profiles()
            self._refresh_profiles()

    def _delete_profile(self) -> None:
        profile = self._selected_profile()
        if not profile:
            return
        if not messagebox.askyesno(
            "刪除角色",
            f"確定刪除「{profile.shortcut_name}」的設定？此操作不會刪除遊戲捷徑。",
            parent=self.root,
        ):
            return
        self.config.profiles = [item for item in self.config.profiles if item.id != profile.id]
        self.store.save(self.config)
        self.scheduler.sync_profiles()
        self._refresh_profiles()

    def _bind_window(self) -> None:
        profile = self._selected_profile()
        if not profile:
            return
        picker = WindowPicker(self.root, profile.window_hwnd)
        self.root.wait_window(picker)
        if picker.result:
            for other in self.config.profiles:
                if other.id != profile.id and other.window_hwnd == picker.result.hwnd:
                    messagebox.showerror(
                        "視窗已被使用",
                        f"此視窗已綁定到「{other.shortcut_name}」，不能重複綁定。",
                        parent=self.root,
                    )
                    return
            profile.window_hwnd = picker.result.hwnd
            profile.window_pid = picker.result.pid
            profile.window_title = picker.result.title
            self.store.save(self.config)
            self._refresh_profiles()
            self._append_log(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{profile.shortcut_name}] 手動綁定 {picker.result.display}"
            )

    def _toggle_profile(self) -> None:
        profile = self._selected_profile()
        if not profile:
            return
        self.scheduler.set_enabled(profile.id, not profile.enabled)

    def _test_capture(self) -> None:
        profile = self._selected_profile()
        if not profile:
            return
        if not is_window(profile.window_hwnd):
            messagebox.showerror("無法測試", "角色尚未綁定有效的遊戲視窗。", parent=self.root)
            return
        self.test_button.configure(state="disabled")
        self._append_log(
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{profile.shortcut_name}] 開始測試背景擷取"
        )

        def worker() -> None:
            error = ""
            result: dict[str, object] = {}
            image_path: Path | None = None
            try:
                with BackgroundWindowSession(profile.window_hwnd) as session:
                    frame = session.capture()
                    if frame is None and session.promote_offscreen():
                        frame = session.capture()
                    if frame is None:
                        raise OSError("PrintWindow、BitBlt 與螢幕外還原都無法取得畫面")
                    result = self.scheduler.vision.diagnostic(frame)
                    result["capture_method"] = session.capture_method
                    LOG_DIR.mkdir(parents=True, exist_ok=True)
                    image_path = LOG_DIR / f"capture-{profile.shortcut_name}-{datetime.now():%Y%m%d-%H%M%S}.png"
                    cv2.imwrite(str(image_path), frame)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

            def finish() -> None:
                self.test_button.configure(state="normal")
                if error:
                    messagebox.showerror("背景擷取失敗", error, parent=self.root)
                    self._append_log(f"背景擷取失敗：{error}")
                else:
                    details = json.dumps(result, ensure_ascii=False, indent=2)
                    messagebox.showinfo(
                        "背景擷取完成",
                        f"辨識結果：\n{details}\n\n截圖已儲存：\n{image_path}",
                        parent=self.root,
                    )
                    self._append_log(f"背景擷取完成：{details}")

            self.root.after(0, finish)

        threading.Thread(target=worker, name="capture-test", daemon=True).start()

    def _start(self) -> None:
        enabled = [profile for profile in self.config.profiles if profile.enabled]
        if not enabled:
            messagebox.showwarning("沒有啟用角色", "請先新增並啟用至少一個角色。", parent=self.root)
            return
        unbound = [profile.shortcut_name for profile in enabled if not is_window(profile.window_hwnd)]
        if unbound:
            names = "、".join(unbound)
            if not messagebox.askyesno(
                "部分角色未綁定",
                f"下列角色目前沒有有效視窗：{names}\n\n開始後會嘗試執行其捷徑並自動綁定。是否繼續？",
                parent=self.root,
            ):
                return
        self.scheduler.start()
        self._update_controls()

    def _stop(self) -> None:
        self.scheduler.stop()
        self._update_controls()

    def _retry_now(self) -> None:
        profile = self._selected_profile()
        if profile:
            self.scheduler.retry_now(profile.id)

    def _open_logs(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        import os

        os.startfile(LOG_DIR)  # type: ignore[attr-defined]

    def _on_close(self) -> None:
        running, _ = self.scheduler.snapshot()
        if running and not messagebox.askyesno(
            "結束程式",
            "關閉程式會停止所有定時工作。確定結束？",
            parent=self.root,
        ):
            return
        self.scheduler.stop()
        self.store.save(self.config)
        self.root.destroy()
