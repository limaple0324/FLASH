"""Independent clock panel reusing the proven V0.2 native panel mechanics.

The adapter owns every mutable panel field; it never borrows the status panel's
position, drag state, popup state or preferences.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from types import MethodType


class ClockBar:
    SHARED_METHODS = (
        "floating_status_settings", "load_floating_status_settings",
        "floating_status_monitor", "floating_status_width_range", "floating_status_geometry",
        "floating_status_native_rect", "place_floating_status_default",
        "apply_floating_status_window_style", "keep_floating_status_topmost",
        "adjust_floating_status_font", "adjust_floating_status_width", "reset_floating_status_size",
        "show_floating_status_menu", "start_floating_status_drag", "drag_floating_status",
        "stop_floating_status_drag", "floating_status_contains_point",
    )

    def __init__(self, app, state):
        self.app = app
        self._destroyed = False
        self.clock_bar = None  # shared hit testing must not recurse back into this panel
        self.rpg_font_family = app.rpg_font_family
        self.floating_status_monitors = app.floating_status_monitors
        self.save_launch_config = app.save_launch_config
        for name in self.SHARED_METHODS:
            setattr(self, name, MethodType(getattr(type(app), name), self))
        self.load_floating_status_settings(state)
        self.floating_drag_offset = None
        self.floating_drag_position = None
        self.floating_drag_pointer = None
        self.floating_status_applied_size = None
        self.floating_status_layout_cache = None
        self.floating_status_menu_thread_id = 0
        self.value = "尚未校正"
        self.window = self.floating_status_window = tk.Toplevel(app)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-toolwindow", True)
        self.window.configure(bg="#ffffff")
        self.window.resizable(False, False)
        self.floating_status_menu = tk.Menu(self.window, tearoff=False)
        for text, command in (
            ("字體縮小", lambda: self.adjust_floating_status_font(-1)),
            ("字體放大", lambda: self.adjust_floating_status_font(1)),
            ("寬度縮短", lambda: self.adjust_floating_status_width(-40)),
            ("寬度加長", lambda: self.adjust_floating_status_width(40)),
            ("恢復預設尺寸", self.reset_floating_status_size),
        ):
            self.floating_status_menu.add_command(label=text, command=command)
        gear = self.floating_status_gear = tk.Button(
            self.window, text="⚙", command=self.show_floating_status_menu,
            bg="#ffffff", fg="#000000", activebackground="#e5e5e5",
            activeforeground="#000000", relief="flat", bd=0,
            font=("Segoe UI Symbol", -20), takefocus=False,
        )
        gear.place(relx=1.0, x=-38, y=3, width=35, relheight=1.0, height=-6)
        self.label = tk.Label(self.window, text=self.value, anchor="w", padx=12,
                              bg="#ffffff", fg="#000000", takefocus=False)
        self.label.place(x=0, y=0, relwidth=1.0, width=-38, relheight=1.0)
        self.label.bind("<ButtonPress-1>", self.start_floating_status_drag)
        self.label.bind("<B1-Motion>", self.drag_floating_status)
        self.label.bind("<ButtonRelease-1>", self.stop_floating_status_drag)
        self.label.bind("<Double-Button-1>", lambda _event: app.restore_from_tray())
        self.update_floating_status()
        self.window.update_idletasks()
        self.apply_floating_status_window_style()
        self.window.deiconify()
        self.keep_floating_status_topmost()

    def measure_floating_status_layout(self):
        # Fix the digit-width envelope so the bar does not jitter each millisecond.
        key = (self.rpg_font_family, self.floating_status_font_size)
        cached = self.floating_status_layout_cache
        if cached is None or cached["key"] != key:
            font = tkfont.Font(root=self.app, family=self.rpg_font_family,
                               size=-self.floating_status_font_size, weight="bold")
            # Measure every repeated digit: '8' is not the widest digit in all
            # configured fonts. Also reserve the invalid-state text, without prefixes.
            width = max(font.measure(f"{digit * 2}:{digit * 2}:{digit * 2}.{digit * 3}")
                        for digit in "0123456789")
            cached = {"key": key, "font": font,
                      "content_width": max(width, font.measure("尚未校正")) + 24}
            self.floating_status_layout_cache = cached
        return cached

    def update_floating_status(self):
        layout = self.measure_floating_status_layout()
        self.label.configure(text=self.value, font=layout["font"])
        self.place_floating_status_default(self.floating_status_geometry(layout))

    def update(self, text):
        if getattr(self, "_destroyed", False):
            return
        window = getattr(self, "window", None)
        if window is not None:
            try:
                if not window.winfo_exists():
                    self._destroyed = True
                    return
            except tk.TclError:
                self._destroyed = True
                return
        value = text if text else "尚未校正"
        if self.value != value:
            self.value = value
            self.update_floating_status()

    def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        self.window.destroy()
