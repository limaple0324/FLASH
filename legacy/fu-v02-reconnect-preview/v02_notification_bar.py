"""Independent disconnect *records*, never an assertion of live connectivity.

Only existing detector hits enter the model. Native placement/menu/drag methods
are borrowed from ClockBar's shared contract; no game, capture or input APIs live
here. Long text is drawn in bounded chunks so Tk never receives one enormous
native text item, and every character remains in the scrolling sequence.
"""
from __future__ import annotations

import bisect
import tkinter as tk
from tkinter import font as tkfont
from types import MethodType

from v02_game_clock_bar import ClockBar


def single_line(value):
    return str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")


class NotificationModel:
    PHASES = {
        "disabled": "未啟用",
        "waiting": "已啟用｜尚未掃描",
        "single_scanning": "單次掃描中",
        "single_done": "單次掃描完成",
        "continuous": "持續偵測",
    }

    def __init__(self):
        self.context = None
        self.epoch = 0
        self.enabled = None
        self.phase = "disabled"
        self.events = {}  # HWND -> (group name, complete window display name)

    def sync_context(self, group, enabled):
        enabled = bool(enabled)
        if group is not self.context or enabled != self.enabled:
            self.context = group  # retain object, avoiding same-index/id reuse
            self.enabled = enabled
            self.reset()

    def reset(self):
        self.epoch += 1
        self.events.clear()
        self.phase = "waiting" if self.enabled else "disabled"

    def set_phase(self, phase):
        if phase not in self.PHASES:
            raise ValueError("unknown notification phase")
        self.phase = phase

    def remember(self, hwnd, name):
        if self.context is not None and hwnd:
            self.events[int(hwnd)] = (str(self.context.name), str(name))

    def snapshot(self):
        return (self.epoch, self.phase,
                str(self.context.name) if self.context is not None else "",
                tuple(self.events.items()))

    def text(self):
        group = single_line(self.context.name) if self.context is not None else "未選組別"
        prefix = f"{group}｜{self.PHASES[self.phase]}"
        if not self.events:
            return ""
        records = "　；　".join(f"{single_line(group)}｜{single_line(name)}"
                            for group, name in self.events.values())
        return f"{prefix}｜斷線偵測紀錄 {len(self.events)} 個：{records}"


class Marquee:
    """Pixel viewport model, with bounded text items and an unabridged sequence."""
    CHUNK_SIZE = 128
    HOLD_FRAMES = 25

    def __init__(self):
        self.chunks = []
        self.ends = []
        self.total = 0
        self.viewport = 1
        self.offset = 0
        self.hold = self.HOLD_FRAMES

    def reset(self, text, measure, viewport):
        self.chunks = [text[i:i + self.CHUNK_SIZE]
                       for i in range(0, len(text), self.CHUNK_SIZE)]
        self.ends = []
        self.total = 0
        for chunk in self.chunks:
            self.total += max(1, measure(chunk))
            self.ends.append(self.total)
        self.viewport = max(1, viewport)
        self.offset = 0
        self.hold = self.HOLD_FRAMES

    def advance(self):
        maximum = max(0, self.total - self.viewport)
        if not maximum:
            return
        if self.hold:
            self.hold -= 1
        elif self.offset < maximum:
            self.offset = min(maximum, self.offset + 2)
            if self.offset == maximum:
                self.hold = self.HOLD_FRAMES
        else:
            self.offset = 0
            self.hold = self.HOLD_FRAMES

    def visible(self):
        first = bisect.bisect_right(self.ends, self.offset)
        for index in range(first, len(self.chunks)):
            left = self.ends[index - 1] if index else 0
            if left >= self.offset + self.viewport:
                break
            yield index, left - self.offset, self.chunks[index]


def initial_notification_settings(monitors, occupied, content_width):
    """Find a bottom-row gap using the *actual* old panel rectangles."""
    preferred = sorted(monitors, key=lambda monitor: not monitor["primary"])
    gaps = []
    for monitor in preferred:
        left, _top, right, _bottom = monitor["bounds"]
        bar_top, bar_height = monitor["taskbar"]
        height = min(42, bar_height)
        top = bar_top + max(0, (bar_height - height) // 2)
        cursor = left + 8
        blocks = sorted((max(left, x1 - 8), min(right, x2 + 8))
                        for x1, y1, x2, y2 in occupied
                        if y1 < top + height and top < y2 and x1 < right and left < x2)
        for x1, x2 in blocks + [(right - 8, right)]:
            if x1 > cursor:
                gaps.append((x1 - cursor, monitor, cursor - left))
            cursor = max(cursor, x2)
    natural = content_width + 38
    for width, monitor, local_x in gaps:
        if width >= natural:
            return {"monitor_id": monitor["id"], "local_x": local_x}
    if gaps:
        width, monitor, local_x = max(gaps, key=lambda gap: gap[0])
        if width >= 86:
            return {"monitor_id": monitor["id"], "local_x": local_x,
                    "width_delta": width - natural}
    # No physical minimum-width gap exists; preserve the old bars regardless.
    return {}


class NotificationBar:
    MAX_CONTENT_WIDTH = 320  # includes 12px left/right padding; gear adds 38px

    def __init__(self, app, state, model):
        self.app = app
        self.model = model
        self.clock_bar = self.notification_bar = None  # no recursive hit testing
        self.rpg_font_family = app.rpg_font_family
        self.floating_status_monitors = app.floating_status_monitors
        self.save_launch_config = app.save_launch_config
        for name in ClockBar.SHARED_METHODS:
            setattr(self, name, MethodType(getattr(type(app), name), self))
        self.load_floating_status_settings(state)
        self.floating_drag_offset = self.floating_drag_position = self.floating_drag_pointer = None
        self.floating_status_applied_size = self.floating_status_layout_cache = None
        self.applied_layout_key = None
        self.floating_status_menu_thread_id = 0
        self.render_key = None
        self.marquee = Marquee()
        self.text_items = {}
        self.timer_ids = set()
        self.destroyed = False
        if not state:
            occupied = [app.floating_status_native_rect()]
            if app.clock_bar is not None:
                occupied.append(app.clock_bar.floating_status_native_rect())
            self.load_floating_status_settings(initial_notification_settings(
                self.floating_status_monitors(), occupied,
                self.measure_floating_status_layout()["content_width"]))
        self.window = self.floating_status_window = tk.Toplevel(app)
        # Capture timers scheduled by the borrowed topmost method as well as the
        # animation. Cancellation is scoped to this panel, never the app's timers.
        self._tk_after = self.window.after
        self.window.after = self.schedule_timer
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-toolwindow", True)
        self.window.configure(bg="#121317")
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
        self.floating_status_gear = tk.Button(
            self.window, text="⚙", command=self.show_floating_status_menu,
            bg="#263040", fg="#cad6ea", activebackground="#404c60",
            activeforeground="#ffffff", relief="flat", bd=0,
            font=("Segoe UI Symbol", -20), takefocus=False)
        self.floating_status_gear.place(relx=1.0, x=-38, y=3, width=35, relheight=1.0, height=-6)
        self.canvas = tk.Canvas(self.window, bg="#121317", bd=0, highlightthickness=0,
                                takefocus=False)
        self.canvas.place(x=0, y=0, relwidth=1.0, width=-38, relheight=1.0)
        self.canvas.bind("<ButtonPress-1>", self.start_floating_status_drag)
        self.canvas.bind("<B1-Motion>", self.drag_floating_status)
        self.canvas.bind("<ButtonRelease-1>", self.stop_floating_status_drag)
        self.canvas.bind("<Double-Button-1>", lambda _event: app.restore_from_tray())
        self.update_floating_status()
        self.window.update_idletasks()
        self.apply_floating_status_window_style()
        self.window.deiconify()
        self.keep_floating_status_topmost()
        self.window.after(40, self.animate)

    def schedule_timer(self, delay, callback, *args):
        def run():
            self.timer_ids.discard(timer_id)
            if not self.destroyed:
                callback(*args)
        timer_id = self._tk_after(delay, run)
        self.timer_ids.add(timer_id)
        return timer_id

    def measure_floating_status_layout(self):
        font_key = (self.rpg_font_family, self.floating_status_font_size)
        key = (self.model.snapshot(), font_key)
        cached = self.floating_status_layout_cache
        if cached is None or cached["key"] != key:
            font = (cached["font"] if cached is not None and cached["font_key"] == font_key else
                    tkfont.Font(root=self.app, family=self.rpg_font_family,
                                size=-self.floating_status_font_size, weight="bold"))
            text = self.model.text()
            text_width = sum(font.measure(text[i:i + Marquee.CHUNK_SIZE])
                             for i in range(0, len(text), Marquee.CHUNK_SIZE))
            cached = {"key": key, "font_key": font_key, "font": font, "text": text,
                      "content_width": min(self.MAX_CONTENT_WIDTH, text_width + 24)}
            self.floating_status_layout_cache = cached
        return cached

    def update_floating_status(self):
        layout = self.measure_floating_status_layout()
        geometry = self.floating_status_geometry(layout)
        self.place_floating_status_default(geometry)
        self.applied_layout_key = layout["key"]
        self.render(layout, geometry[0] - 38, geometry[1])

    def render(self, layout, width, height):
        key = (self.model.snapshot(), layout["key"], width, height)
        if key != self.render_key:
            self.render_key = key
            self.marquee.reset(layout["text"], layout["font"].measure, max(1, width - 24))
            self.canvas.delete("notification_text")
            self.text_items.clear()
        visible = list(self.marquee.visible())
        wanted = {index for index, _x, _text in visible}
        for index in list(self.text_items):
            if index not in wanted:
                self.canvas.delete(self.text_items.pop(index))
        for index, x, text in visible:
            if index not in self.text_items:
                self.text_items[index] = self.canvas.create_text(
                    x + 12, height // 2, text=text, anchor="w", font=layout["font"],
                    fill="#ffffff", tags="notification_text")
            else:
                self.canvas.coords(self.text_items[index], x + 12, height // 2)

    def animate(self):
        if self.destroyed:
            return
        self.app.refresh_notification_context()
        # Only a changed stable snapshot/font can request layout; ordinary frames
        # neither measure text nor reposition the native panel (including drags).
        layout = self.measure_floating_status_layout()
        if layout["key"] != self.applied_layout_key:
            self.update_floating_status()
        else:
            width, height = self.floating_status_applied_size
            self.render(layout, width - 38, height)
        self.marquee.advance()
        self.window.after(40, self.animate)

    def destroy(self):
        if self.destroyed:
            return
        self.destroyed = True
        for timer_id in tuple(self.timer_ids):
            self.window.after_cancel(timer_id)
        self.timer_ids.clear()
        self.model.reset()
        self.window.destroy()
