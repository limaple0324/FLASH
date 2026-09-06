from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
QUICK = ROOT / "quick_code_library.py"
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_quick_code_library.py"

quick = QUICK.read_text(encoding="utf-8")
old = 'from tkinter import messagebox, ttk\n'
new = 'from tkinter import font as tkfont, messagebox, ttk\n'
if quick.count(old) != 1:
    raise SystemExit(f"quick-code font import anchor count={quick.count(old)}")
quick = quick.replace(old, new, 1)

old = '''        initial_position: tuple[int, int] | None = None,\n        log_callback: Callable[[str], None] | None = None,\n    ):\n        self.root, self.store, self.copy_service = root, store, copy_service\n        self.manage_callback, self.save_position = manage_callback, save_position\n        self.position = initial_position or (24, 120)\n        self.log_callback = log_callback or (lambda _message: None)\n        self.window: tk.Toplevel | None = None\n        self.button: ttk.Button | None = None\n        self.popup: tk.Toplevel | None = None\n'''
new = '''        initial_position: tuple[int, int] | None = None,\n        log_callback: Callable[[str], None] | None = None,\n        save_scale: Callable[[float], None] | None = None,\n        initial_scale: float = 1.0,\n    ):\n        self.root, self.store, self.copy_service = root, store, copy_service\n        self.manage_callback, self.save_position = manage_callback, save_position\n        self.save_scale = save_scale or (lambda _scale: None)\n        self.position = initial_position or (24, 120)\n        self.scale = self._normalize_scale(initial_scale)\n        self.log_callback = log_callback or (lambda _message: None)\n        self.window: tk.Toplevel | None = None\n        self.button: ttk.Button | None = None\n        self.popup: tk.Toplevel | None = None\n        self.size_popup: tk.Toplevel | None = None\n'''
if quick.count(old) != 1:
    raise SystemExit(f"quick-code ctor anchor count={quick.count(old)}")
quick = quick.replace(old, new, 1)

old = '''        self.popup_scrollbar: ttk.Scrollbar | None = None\n        self.hwnd = 0\n        self.popup_hwnd = 0\n        self.disposed = False\n'''
new = '''        self.popup_scrollbar: ttk.Scrollbar | None = None\n        self.hwnd = 0\n        self.popup_hwnd = 0\n        self.size_popup_hwnd = 0\n        self._main_style_name = f"QuickCodeFloating{id(self)}.TButton"\n        self._popup_style_name = f"QuickCodePopup{id(self)}.TButton"\n        self._style: ttk.Style | None = None\n        self.disposed = False\n'''
if quick.count(old) != 1:
    raise SystemExit(f"quick-code style attrs anchor count={quick.count(old)}")
quick = quick.replace(old, new, 1)

anchor = '''    def _log(self, message: str) -> None:\n'''
helper = '''    SCALE_STEPS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0)\n\n    @classmethod\n    def _normalize_scale(cls, value: object) -> float:\n        try:\n            raw = float(value)\n        except (TypeError, ValueError):\n            raw = 1.0\n        return min(cls.SCALE_STEPS, key=lambda item: abs(item - raw))\n\n    def _scaled_font_spec(self) -> tuple[str, int, str]:\n        family, size, weight = "Segoe UI", 9, "normal"\n        try:\n            actual = tkfont.nametofont("TkDefaultFont").actual()\n            family = str(actual.get("family") or family)\n            size = max(1, abs(int(actual.get("size") or size)))\n            weight = str(actual.get("weight") or weight)\n        except Exception:\n            pass\n        return family, max(7, int(round(size * self.scale))), weight\n\n    def _apply_scale_styles(self) -> None:\n        if self._style is None:\n            self._style = ttk.Style(self.root)\n        font_spec = self._scaled_font_spec()\n        pad_x = max(5, int(round(10 * self.scale)))\n        pad_y = max(2, int(round(4 * self.scale)))\n        self._style.configure(\n            self._main_style_name, font=font_spec, padding=(pad_x, pad_y),\n        )\n        self._style.configure(\n            self._popup_style_name, font=font_spec, padding=(pad_x, pad_y),\n        )\n\n    def set_scale(self, value: object, *, persist: bool = False) -> bool:\n        scale = self._normalize_scale(value)\n        if self.disposed:\n            return False\n        self.scale = scale\n        try:\n            self._apply_scale_styles()\n            self.close_popup()\n            self.close_size_popup()\n            if self.window is not None and self.hwnd:\n                self.window.geometry(self._geometry_offset(*self.position))\n                self.window.update_idletasks()\n                width = max(1, int(self.window.winfo_reqwidth()))\n                height = max(1, int(self.window.winfo_reqheight()))\n                clamped = self._clamp_to_work_area(\n                    self.position[0], self.position[1], width, height,\n                )\n                if clamped is None:\n                    return False\n                if not self._move_native(self.hwnd, *clamped):\n                    return False\n                if clamped != self.position:\n                    self.position = clamped\n                    if persist:\n                        self.save_position(*clamped)\n            if persist:\n                self.save_scale(scale)\n            return True\n        except Exception:\n            return False\n\n    def _change_scale(self, direction: int) -> None:\n        steps = self.SCALE_STEPS\n        current = self._normalize_scale(self.scale)\n        index = steps.index(current)\n        target = steps[max(0, min(len(steps) - 1, index + int(direction)))]\n        self.set_scale(target, persist=True)\n\n    def _reset_scale(self) -> None:\n        self.set_scale(1.0, persist=True)\n\n'''
if quick.count(anchor) != 1:
    raise SystemExit(f"quick-code scale helper anchor count={quick.count(anchor)}")
quick = quick.replace(anchor, helper + anchor, 1)

old = '''            self.window = tk.Toplevel(self.root, takefocus=False)\n            self.button = ttk.Button(self.window, text="魔力代碼", takefocus=False)\n            self.button.pack()\n            self.button.bind("<ButtonPress-1>", self._press)\n            self.button.bind("<B1-Motion>", self._motion)\n            self.button.bind("<ButtonRelease-1>", self._release)\n'''
new = '''            self.window = tk.Toplevel(self.root, takefocus=False)\n            self._apply_scale_styles()\n            self.button = ttk.Button(\n                self.window, text="魔力代碼", takefocus=False, style=self._main_style_name,\n            )\n            self.button.pack()\n            self.button.bind("<ButtonPress-1>", self._press)\n            self.button.bind("<B1-Motion>", self._motion)\n            self.button.bind("<ButtonRelease-1>", self._release)\n            self.button.bind("<ButtonRelease-3>", self._right_release)\n'''
if quick.count(old) != 1:
    raise SystemExit(f"quick-code right-click setup anchor count={quick.count(old)}")
quick = quick.replace(old, new, 1)

old = '''            for sequence in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>"):\n'''
new = '''            for sequence in (\n                "<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>", "<ButtonRelease-3>",\n            ):\n'''
if quick.count(old) != 1:
    raise SystemExit(f"quick-code right-click cleanup anchor count={quick.count(old)}")
quick = quick.replace(old, new, 1)

anchor = '''    def toggle_popup(self) -> None:\n'''
helper = '''    def _right_release(self, event):\n        self.close_popup()\n        self.toggle_size_popup(int(event.x_root), int(event.y_root))\n        return "break"\n\n    def toggle_size_popup(self, x: int, y: int) -> None:\n        if self.size_popup is not None:\n            self.close_size_popup()\n            return\n        try:\n            self.size_popup = tk.Toplevel(self.root, takefocus=False)\n            self.size_popup.withdraw()\n            self._apply_scale_styles()\n            ttk.Button(\n                self.size_popup, text="縮小", takefocus=False,\n                style=self._popup_style_name, command=lambda: self._change_scale(-1),\n            ).pack(fill="x")\n            ttk.Button(\n                self.size_popup, text="100%", takefocus=False,\n                style=self._popup_style_name, command=self._reset_scale,\n            ).pack(fill="x")\n            ttk.Button(\n                self.size_popup, text="放大", takefocus=False,\n                style=self._popup_style_name, command=lambda: self._change_scale(1),\n            ).pack(fill="x")\n            self.size_popup.update_idletasks()\n            self.size_popup_hwnd = self._prepare_window(self.size_popup)\n            if not self.size_popup_hwnd:\n                raise RuntimeError("size popup no-activate style unavailable")\n            width = max(1, int(self.size_popup.winfo_reqwidth()))\n            height = max(1, int(self.size_popup.winfo_reqheight()))\n            clamped = self._clamp_to_work_area(int(x), int(y), width, height)\n            if clamped is None:\n                raise RuntimeError("size popup monitor work area unavailable")\n            x, y = clamped\n            self.size_popup.geometry(\n                f"{width}x{height}{self._geometry_offset(x, y)}"\n            )\n            self.size_popup.deiconify()\n            if not self._move_native(self.size_popup_hwnd, x, y):\n                raise RuntimeError("size popup no-activate positioning failed")\n        except Exception:\n            self.close_size_popup()\n            self._log("魔力代碼尺寸選單建立失敗；尺寸保持不變。")\n\n    def close_size_popup(self) -> None:\n        popup, self.size_popup = self.size_popup, None\n        self.size_popup_hwnd = 0\n        if popup is not None:\n            try:\n                popup.destroy()\n            except Exception:\n                pass\n\n'''
if quick.count(anchor) != 1:
    raise SystemExit(f"quick-code size popup anchor count={quick.count(anchor)}")
quick = quick.replace(anchor, helper + anchor, 1)

old = '''    def toggle_popup(self) -> None:\n        if self.popup is not None:\n'''
new = '''    def toggle_popup(self) -> None:\n        self.close_size_popup()\n        if self.popup is not None:\n'''
if quick.count(old) != 1:
    raise SystemExit(f"quick-code popup mutual-close anchor count={quick.count(old)}")
quick = quick.replace(old, new, 1)

old = '''                    text=item.Name,\n                    takefocus=False,\n                    command=lambda entry_id=item.Id: self._copy_current_and_close(entry_id),\n'''
new = '''                    text=item.Name,\n                    takefocus=False,\n                    style=self._popup_style_name,\n                    command=lambda entry_id=item.Id: self._copy_current_and_close(entry_id),\n'''
if quick.count(old) != 1:
    raise SystemExit(f"quick-code popup item style anchor count={quick.count(old)}")
quick = quick.replace(old, new, 1)
old = '''                text="管理魔力代碼庫",\n                takefocus=False,\n                command=self._manage_and_close,\n'''
new = '''                text="管理魔力代碼庫",\n                takefocus=False,\n                style=self._popup_style_name,\n                command=self._manage_and_close,\n'''
if quick.count(old) != 1:
    raise SystemExit(f"quick-code manage style anchor count={quick.count(old)}")
quick = quick.replace(old, new, 1)

old = '''        self.disposed = True\n        self.close_popup()\n        self._cleanup_main_window()\n'''
new = '''        self.disposed = True\n        self.close_popup()\n        self.close_size_popup()\n        self._cleanup_main_window()\n'''
if quick.count(old) != 1:
    raise SystemExit(f"quick-code dispose size popup anchor count={quick.count(old)}")
quick = quick.replace(old, new, 1)
QUICK.write_text(quick, encoding="utf-8", newline="\n")

app = APP.read_text(encoding="utf-8")
old = '''        self.pending_quick_code_floating_position: tuple[int, int] | None = None\n        self._launch_config_write_blocked = False\n'''
new = '''        self.pending_quick_code_floating_position: tuple[int, int] | None = None\n        self.pending_quick_code_floating_scale = 1.0\n        self._launch_config_write_blocked = False\n'''
if app.count(old) != 1:
    raise SystemExit(f"quick-code app init anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''            self.manage_quick_codes, self.save_quick_code_floating_position,\n            self.pending_quick_code_floating_position, self.write_log,\n        )\n'''
new = '''            self.manage_quick_codes, self.save_quick_code_floating_position,\n            self.pending_quick_code_floating_position, self.write_log,\n            save_scale=self.save_quick_code_floating_scale,\n            initial_scale=self.pending_quick_code_floating_scale,\n        )\n'''
if app.count(old) != 1:
    raise SystemExit(f"quick-code app wiring anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''    def save_quick_code_floating_position(self, x: int, y: int) -> None:\n        self.pending_quick_code_floating_position = (int(x), int(y))\n        self.save_launch_config()\n\n'''
new = '''    def save_quick_code_floating_position(self, x: int, y: int) -> None:\n        self.pending_quick_code_floating_position = (int(x), int(y))\n        self.save_launch_config()\n\n    def save_quick_code_floating_scale(self, scale: float) -> None:\n        self.pending_quick_code_floating_scale = QuickCodeFloatingControl._normalize_scale(scale)\n        self.save_launch_config()\n\n'''
if app.count(old) != 1:
    raise SystemExit(f"quick-code scale save callback anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''            floating_position = app_state.get("quick_code_floating_position")\n            if isinstance(floating_position, dict):\n                x, y = floating_position.get("x"), floating_position.get("y")\n                if type(x) is int and type(y) is int:\n                    self.pending_quick_code_floating_position = (x, y)\n            self.pending_active_group_name = str(app_state.get("active_group_name") or "")\n'''
new = '''            floating_position = app_state.get("quick_code_floating_position")\n            if isinstance(floating_position, dict):\n                x, y = floating_position.get("x"), floating_position.get("y")\n                if type(x) is int and type(y) is int:\n                    self.pending_quick_code_floating_position = (x, y)\n            self.pending_quick_code_floating_scale = QuickCodeFloatingControl._normalize_scale(\n                app_state.get("quick_code_floating_scale", 1.0)\n            )\n            self.pending_active_group_name = str(app_state.get("active_group_name") or "")\n'''
if app.count(old) != 1:
    raise SystemExit(f"quick-code scale load anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''        floating_position = (\n            floating.position if floating is not None\n            else self.__dict__.get("pending_quick_code_floating_position") or (24, 120)\n        )\n        auto_resize_var = self.__dict__.get("auto_resize_flash")\n'''
new = '''        floating_position = (\n            floating.position if floating is not None\n            else self.__dict__.get("pending_quick_code_floating_position") or (24, 120)\n        )\n        floating_scale = QuickCodeFloatingControl._normalize_scale(\n            floating.scale if floating is not None\n            else self.__dict__.get("pending_quick_code_floating_scale", 1.0)\n        )\n        auto_resize_var = self.__dict__.get("auto_resize_flash")\n'''
if app.count(old) != 1:
    raise SystemExit(f"quick-code scale save-read anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''                "quick_code_floating_position": {\n                    "x": int(floating_position[0]),\n                    "y": int(floating_position[1]),\n                },\n                "auto_resize_flash": auto_resize_flash,\n'''
new = '''                "quick_code_floating_position": {\n                    "x": int(floating_position[0]),\n                    "y": int(floating_position[1]),\n                },\n                "quick_code_floating_scale": float(floating_scale),\n                "auto_resize_flash": auto_resize_flash,\n'''
if app.count(old) != 1:
    raise SystemExit(f"quick-code scale save field anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''            self.pending_window_geometry = ""\n            self.pending_quick_code_floating_position = None\n            self.load_launch_config()\n'''
new = '''            self.pending_window_geometry = ""\n            self.pending_quick_code_floating_position = None\n            self.pending_quick_code_floating_scale = 1.0\n            self.load_launch_config()\n'''
if app.count(old) != 1:
    raise SystemExit(f"quick-code scale import-reset anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''            if floating is not None:\n                floating.set_position(\n                    *(self.pending_quick_code_floating_position or (24, 120))\n                )\n            self.refresh_group_selector()\n'''
new = '''            if floating is not None:\n                floating.set_position(\n                    *(self.pending_quick_code_floating_position or (24, 120))\n                )\n                floating.set_scale(\n                    self.pending_quick_code_floating_scale, persist=False,\n                )\n            self.refresh_group_selector()\n'''
if app.count(old) != 1:
    raise SystemExit(f"quick-code scale import-apply anchor count={app.count(old)}")
app = app.replace(old, new, 1)
APP.write_text(app, encoding="utf-8", newline="\n")

text = TEST.read_text(encoding="utf-8")
anchor = '''class SourceIntegrationTests(unittest.TestCase):\n'''
case = '''class QuickCodeScaleTests(unittest.TestCase):\n    def host(self):\n        store = mock.Mock(entries=())\n        return q.QuickCodeFloatingControl(\n            mock.Mock(), store, mock.Mock(), mock.Mock(), mock.Mock(),\n            save_scale=mock.Mock(), initial_scale=1.0,\n        )\n\n    def test_right_click_offers_noactivate_whole_control_scale_menu(self):\n        source = inspect.getsource(q.QuickCodeFloatingControl)\n        self.assertIn('"<ButtonRelease-3>"', source)\n        self.assertIn('text="縮小"', source)\n        self.assertIn('text="100%"', source)\n        self.assertIn('text="放大"', source)\n        self.assertIn('style=self._main_style_name', source)\n        self.assertIn('style=self._popup_style_name', source)\n        self.assertIn('self.size_popup_hwnd = self._prepare_window(self.size_popup)', source)\n        self.assertNotIn('SetForegroundWindow', source)\n        self.assertNotIn('focus_force', source)\n\n    def test_scale_is_discrete_clamped_and_persisted(self):\n        host = self.host()\n        host._apply_scale_styles = mock.Mock()\n        host.close_popup = mock.Mock()\n        host.close_size_popup = mock.Mock()\n        host.window = mock.Mock()\n        host.hwnd = 9\n        host.window.winfo_reqwidth.return_value = 160\n        host.window.winfo_reqheight.return_value = 50\n        host._clamp_to_work_area = mock.Mock(return_value=(10, 20))\n        host._move_native = mock.Mock(return_value=True)\n        self.assertTrue(host.set_scale(9.0, persist=True))\n        self.assertEqual(host.scale, 2.0)\n        host.save_scale.assert_called_once_with(2.0)\n        host._clamp_to_work_area.assert_called_once_with(24, 120, 160, 50)\n\n    def test_app_persists_and_restores_quick_code_scale(self):\n        source = (Path(__file__).parent / "flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn('self.pending_quick_code_floating_scale = 1.0', source)\n        self.assertIn('app_state.get("quick_code_floating_scale", 1.0)', source)\n        self.assertIn('"quick_code_floating_scale": float(floating_scale)', source)\n        self.assertIn('save_scale=self.save_quick_code_floating_scale', source)\n        self.assertIn('initial_scale=self.pending_quick_code_floating_scale', source)\n        self.assertIn('floating.set_scale(', source)\n\n\n'''
if text.count(anchor) != 1:
    raise SystemExit(f"quick-code scale regression anchor count={text.count(anchor)}")
text = text.replace(anchor, case + anchor, 1)
TEST.write_text(text, encoding="utf-8", newline="\n")

print("LIVE_FIX_APPLIED quick-code right-click whole-control scaling + persistence")
