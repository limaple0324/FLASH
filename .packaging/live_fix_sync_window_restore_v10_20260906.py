from pathlib import Path

ROOT = Path('legacy/fu-v02-reconnect-preview')
APP = ROOT / 'flash_sync_v02.py'
TEST = ROOT / 'test_fu_reconnect_integration.py'
LOCAL_TEST = ROOT / 'test_fu_local_20260830_002.py'

app = APP.read_text(encoding='utf-8')

old = '        self.required_sections = {"組別啟動設定"}\n'
new = '        self.required_sections = {"組別啟動設定", "同步視窗"}\n'
if app.count(old) != 1:
    raise SystemExit(f'required sections anchor count={app.count(old)}')
app = app.replace(old, new, 1)

anchor = '''        launch_tools = self.make_section("組別啟動設定", True)\n'''
sync_ui = '''        sync_tools = self.make_section("同步視窗", True)\n        sync_head = ttk.Frame(sync_tools)\n        sync_head.pack(fill="x", padx=10, pady=(6, 4))\n        ttk.Label(sync_head, textvariable=self.master_text).pack(side="left", padx=(0, 10))\n        self.capture_master_button = ttk.Button(\n            sync_head, text="點選主窗", command=self.capture_master,\n        )\n        self.capture_master_button.pack(side="left", padx=(0, 6))\n        self.batch_capture_button = tk.Button(\n            sync_head, text="批量加入", width=9, command=self.capture_follower,\n        )\n        self.batch_capture_button.pack(side="left", padx=(0, 6))\n        ttk.Button(\n            sync_head, text="移除選取", command=self.remove_selected_sync_windows,\n        ).pack(side="left", padx=(0, 6))\n        ttk.Button(\n            sync_head, text="清空", command=self.clear_followers,\n        ).pack(side="left", padx=(0, 6))\n        self.sync_toggle_button = ttk.Button(\n            sync_head, text="開始同步", command=self.toggle_current_sync,\n        )\n        self.sync_toggle_button.pack(side="left")\n\n        sync_status = ttk.Frame(sync_tools)\n        sync_status.pack(fill="x", padx=10, pady=(0, 4))\n        ttk.Label(sync_status, textvariable=self.status_text).pack(side="left")\n\n        self.sync_tree = ttk.Treeview(\n            sync_tools, columns=("role", "rect", "delay"), show="headings", height=4,\n        )\n        self.sync_tree.heading("role", text="同步視窗")\n        self.sync_tree.heading("rect", text="位置")\n        self.sync_tree.heading("delay", text="延遲ms")\n        self.sync_tree.column("role", width=250, anchor="w", stretch=True)\n        self.sync_tree.column("rect", width=260, anchor="w", stretch=True)\n        self.sync_tree.column("delay", width=80, anchor="center", stretch=False)\n        self.sync_tree.pack(fill="x", padx=10, pady=(0, 10))\n\n'''
if app.count(anchor) != 1:
    raise SystemExit(f'sync ui anchor count={app.count(anchor)}')
app = app.replace(anchor, sync_ui + anchor, 1)

old_refresh = '''    def refresh_followers(self) -> None:\n        removed = [hwnd for hwnd in self.followers if not user32.IsWindow(hwnd)]\n        valid = [hwnd for hwnd in self.followers if hwnd not in removed]\n        for hwnd in removed:\n            self.offsets.pop(hwnd, None)\n            self.role_ids.pop(hwnd, None)\n        self.followers = valid\n\n'''
new_refresh = '''    def refresh_followers(self) -> None:\n        removed = [hwnd for hwnd in self.followers if not user32.IsWindow(hwnd)]\n        valid = [hwnd for hwnd in self.followers if hwnd not in removed]\n        for hwnd in removed:\n            self.offsets.pop(hwnd, None)\n            self.role_ids.pop(hwnd, None)\n        self.followers = valid\n        tree = getattr(self, "sync_tree", None)\n        if tree is None:\n            return\n        tree.delete(*tree.get_children())\n        group = self.current_group()\n        shown: set[int] = set()\n        for hwnd in valid:\n            if not user32.IsWindow(hwnd):\n                continue\n            role = self.window_display_name_for_group(group, hwnd)\n            rect = get_window_rect(hwnd)\n            rect_text = f"{rect.left},{rect.top},{rect.right},{rect.bottom}"\n            tree.insert(\n                "", "end", iid=str(hwnd),\n                values=(role, rect_text, self.window_delay_ms(group, hwnd)),\n            )\n            shown.add(int(hwnd))\n        for index, entry in enumerate(group.launch_entries[1:], start=1):\n            hwnd = int(group.launch_hwnds.get(index, 0) or 0)\n            if hwnd and hwnd in shown:\n                continue\n            tree.insert(\n                "", "end", iid=f"entry:{entry.entry_id}",\n                values=(f"{self.launch_entry_display_name(entry)}（未開啟）", "-", int(entry.delay_ms)),\n            )\n\n    def selected_sync_hwnds(self) -> list[int]:\n        tree = getattr(self, "sync_tree", None)\n        if tree is None:\n            return []\n        selected: list[int] = []\n        for item in tree.selection():\n            try:\n                hwnd = int(item)\n            except (TypeError, ValueError):\n                continue\n            if hwnd and user32.IsWindow(hwnd):\n                selected.append(hwnd)\n        return selected\n\n    def remove_selected_sync_windows(self) -> None:\n        if self.running:\n            self.write_log("同步中不能移除同步視窗，請先停止。")\n            return\n        hwnds = self.selected_sync_hwnds()\n        if not hwnds:\n            self.write_log("沒有選到已開啟的同步視窗。")\n            return\n        group = self.current_group()\n        removed = set(int(hwnd) for hwnd in hwnds)\n        group.followers = [hwnd for hwnd in group.followers if int(hwnd) not in removed]\n        for hwnd in removed:\n            group.offsets.pop(hwnd, None)\n            group.role_ids.pop(hwnd, None)\n            identity = group.window_exact_identities.pop(hwnd, None)\n            clear_exact_window_target(hwnd, identity)\n        group.launch_hwnds = {\n            int(index): int(hwnd)\n            for index, hwnd in group.launch_hwnds.items()\n            if int(hwnd) not in removed\n        }\n        group.launch_verified_lifecycles = {\n            int(index): lifecycle\n            for index, lifecycle in group.launch_verified_lifecycles.items()\n            if int(index) in group.launch_hwnds\n        }\n        group.explicit_launch_bindings = {\n            entry_id: proof\n            for entry_id, proof in group.explicit_launch_bindings.items()\n            if int(proof[0]) not in removed\n        }\n        self.sync_launch_hwnds_from_group_windows(group)\n        self.refresh_followers()\n        self.update_master_text()\n        self.write_log(f"已移除 {len(removed)} 個同步視窗。")\n\n    def toggle_current_sync(self) -> None:\n        group_index = int(self.active_group_index.get())\n        if not (0 <= group_index < len(self.groups)):\n            return\n        group = self.groups[group_index]\n        if group.running:\n            self.stop_sync(group_index)\n            return\n        if (\n            group_index in self.pending_sync_start_groups\n            or id(group) in self.__dict__.setdefault("_launch_inflight_group_tokens", set())\n        ):\n            self.pending_sync_start_groups.discard(group_index)\n            self.__dict__.setdefault("_launch_inflight_start_tokens", set()).discard(id(group))\n            self.write_log(f"{group.name}已取消待啟動同步；窗口啟動與固定監管仍會安全完成。")\n            self.update_sync_state_text()\n            return\n        self.start_sync(group_index, skip_prepare=False, quiet=False)\n\n'''
if app.count(old_refresh) != 1:
    raise SystemExit(f'refresh followers anchor count={app.count(old_refresh)}')
app = app.replace(old_refresh, new_refresh, 1)

old_state = '''    def update_sync_state_text(self) -> None:\n        current = self.current_group()\n        group_name, state, group = self.status_display_parts()\n        if current.running:\n            self.status_text.set(f"{current.name}同步狀態：已開啟")\n        elif group.running:\n            self.status_text.set(f"{group_name}同步狀態：已開啟")\n        else:\n            self.status_text.set(f"{current.name}同步狀態：{state}")\n        self.update_window_title()\n\n'''
new_state = '''    def update_sync_state_text(self) -> None:\n        current = self.current_group()\n        group_name, state, group = self.status_display_parts()\n        if current.running:\n            self.status_text.set(f"{current.name}同步狀態：已開啟")\n        elif group.running:\n            self.status_text.set(f"{group_name}同步狀態：已開啟")\n        else:\n            self.status_text.set(f"{current.name}同步狀態：{state}")\n        button = getattr(self, "sync_toggle_button", None)\n        if button is not None:\n            waiting = bool(\n                int(self.active_group_index.get()) in self.pending_sync_start_groups\n                or id(current) in self.__dict__.setdefault("_launch_inflight_group_tokens", set())\n            )\n            button.configure(\n                text="停止同步" if current.running else ("取消等待" if waiting else "開始同步")\n            )\n        self.update_window_title()\n\n'''
if app.count(old_state) != 1:
    raise SystemExit(f'update sync state anchor count={app.count(old_state)}')
app = app.replace(old_state, new_state, 1)

# Preserve an entry->HWND lifecycle proof for identityless shortcuts after a
# successful strict reattach commit.  The next sync-start scan can then verify
# the same window without requiring user=/pass= in the shortcut target.
anchor_commit = '''        self.write_log(f"自動重連：本次嚴格身份驗證納管 {len(committed)} 個新視窗。")\n'''
insert_commit = '''        for index, hwnd in sorted(matches.items()):\n            if entry_identities.get(int(index), ""):\n                continue\n            lifecycle = snapshot_lifecycles.get(int(hwnd))\n            if lifecycle is None or not (0 <= int(index) < len(group.launch_entries)):\n                continue\n            entry_id = str(group.launch_entries[int(index)].entry_id)\n            if entry_id:\n                group.explicit_launch_bindings[entry_id] = (int(hwnd), lifecycle)\n\n''' + anchor_commit
if app.count(anchor_commit) != 1:
    raise SystemExit(f'identityless runtime proof anchor count={app.count(anchor_commit)}')
app = app.replace(anchor_commit, insert_commit, 1)

APP.write_text(app, encoding='utf-8', newline='\n')

# Keep the regression inside ExistingWindowReattachTests, which the cumulative
# Windows packaging gate already executes. The exact base source does not have
# that class yet, so keep a fallback for local reconstruction tests.
test = TEST.read_text(encoding='utf-8')
anchor_test = 'class ExistingWindowReattachTests(unittest.TestCase):\n'
method_body = '''    def test_sync_window_ui_and_identityless_runtime_proof_are_present(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn('self.required_sections = {"組別啟動設定", "同步視窗"}', source)\n        self.assertIn('make_section("同步視窗", True)', source)\n        self.assertIn('text="點選主窗"', source)\n        self.assertIn('text="批量加入"', source)\n        self.assertIn('text="開始同步"', source)\n        self.assertIn('def selected_sync_hwnds(', source)\n        self.assertIn('def remove_selected_sync_windows(', source)\n        self.assertIn('def toggle_current_sync(', source)\n        self.assertIn('group.explicit_launch_bindings[entry_id] = (int(hwnd), lifecycle)', source)\n\n'''
if anchor_test in test:
    test = test.replace(anchor_test, anchor_test + method_body, 1)
else:
    strict_anchor = 'class StrictRegistryTests(unittest.TestCase):\n'
    if test.count(strict_anchor) != 1:
        raise SystemExit(f'test fallback anchor count={test.count(strict_anchor)}')
    test = test.replace(
        strict_anchor,
        'class ExistingWindowReattachTests(unittest.TestCase):\n' + method_body + '\n' + strict_anchor,
        1,
    )
TEST.write_text(test, encoding='utf-8', newline='\n')

# The exact reviewed source intentionally removed this UI. That contract is now
# explicitly superseded by the user's restoration request, so the old negative
# assertion must not make a future full-suite run fail.
local_test = LOCAL_TEST.read_text(encoding='utf-8')
old_contract = '    def test_build_removes_sync_window_section_and_keeps_launch_controls(self):\n'
new_contract = '    @unittest.skip("同步視窗 UI 已依使用者要求恢復；舊移除契約作廢")\n' + old_contract
if local_test.count(old_contract) != 1:
    raise SystemExit(f'legacy removal test anchor count={local_test.count(old_contract)}')
local_test = local_test.replace(old_contract, new_contract, 1)
LOCAL_TEST.write_text(local_test, encoding='utf-8', newline='\n')

print('LIVE_FIX_V10_APPLIED sync-window UI restored + identityless sync lifecycle proof retained')
