import ipaddress
import os
import platform
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

from autotune import (
    STARTUP_STAGGER_SECONDS,
    get_miner_status,
    get_system_info,
    monitor_and_adjust,
    reset_miners_to_baseline,
    restart_bitaxe,
)
from config import (
    GAMMA601_LIMITS,
    HARD_MAX_FREQ,
    HARD_MAX_VOLT,
    HARD_MIN_FREQ,
    HARD_MIN_VOLT,
    STOCK_FREQ,
    STOCK_VOLT,
    add_miner,
    adopted_hostname,
    detect_miners,
    get_miner_defaults,
    get_miners,
    is_gamma_601,
    load_config,
    miner_name_from_info,
    miner_type_from_info,
    save_config,
    update_miner,
)

BG = "#141414"
PANEL = "#1c1c1c"
EDGE = "#3a3a3a"
TEXT = "#f2f2f2"
MUTED = "#9a9a9a"
ACCENT = "#e0a100"
ACCENT_TEXT = "#141414"
SELECTION = "#8a5a00"
DANGER = "#6e3030"
QUIET = "#2a2a2a"
FIELD_BG = "#2a2a2a"
HOLD = "#1a4a32"
CLIMB = "#1a3d5c"
TRIM = "#4a3c18"
ALERT = "#4a2222"

FONT = ("Segoe UI", 11)
FONT_SMALL = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 11, "bold")
FONT_TITLE = ("Segoe UI", 18, "bold")
FONT_SECTION = ("Segoe UI", 12, "bold")

IDLE_POLL_SECONDS = 10
TREE_COLUMNS = ("Name", "IP", "Freq", "mV", "ASIC", "VR", "GH/s", "W", "Phase", "Error", "Setpoint")
COL_NAME, COL_IP, COL_FREQ, COL_MV, COL_ASIC, COL_VR, COL_HASH, COL_WATTS, COL_PHASE, COL_ERROR, COL_SETPOINT = range(11)

FREQ_FIELDS = (("min_freq", "Min"), ("start_freq", "Start"), ("max_freq", "Max"))
VOLT_FIELDS = (("min_volt", "Min"), ("start_volt", "Start"), ("max_volt", "Max"))
LIMIT_FIELDS = (
    ("max_temp", "ASIC temp (°C)"),
    ("max_watts", "Watts"),
    ("max_vr_temp", "VR temp (°C)"),
    ("min_input_voltage", "Input voltage (V)"),
    ("max_error_percentage", "Error %"),
)
ALL_AUTOTUNE_FIELDS = tuple(field for field, _label in (*FREQ_FIELDS, *VOLT_FIELDS, *LIMIT_FIELDS))


def enable_windows_dpi_awareness():
    """Make Tk honor the tablet's per-monitor scale factor."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def fit_window_to_work_area(root):
    """Size the window to the screen instead of a fixed 1300x700."""
    root.update_idletasks()
    screen_w = max(320, int(root.winfo_screenwidth()) - 16)
    screen_h = max(240, int(root.winfo_screenheight()) - 72)
    root.geometry(f"{screen_w}x{screen_h}+0+0")
    root.minsize(min(640, screen_w), min(480, screen_h))


def blank_miner_row(nickname, ip):
    """Tree row: name, address, then live readings. Board type is kept beside the row."""
    return (nickname, ip, "-", "-", "-", "-", "-", "-", "-", "-", "-")


def replace_ips_with_names(message, names):
    """Swap known miner IPs for nicknames. Longer addresses are replaced first.

    A nickname that already contains its IP is left alone so Miner-192.168.8.10
    does not become Miner-Miner-192.168.8.10.
    """
    text = "" if message is None else str(message)
    if not text or not names:
        return text
    for ip in sorted(names, key=len, reverse=True):
        name = str(names.get(ip) or "").strip()
        if not ip or not name or name == ip or ip in name:
            continue
        text = re.sub(rf"\b{re.escape(str(ip))}\b", lambda _match, replacement=name: replacement, text)
    return text


def parse_autotuner_value(field, raw):
    """Parse one AutoTuner cell. Frequency and voltage are clamped to the Gamma 601 range."""
    text = str(raw).strip()
    if text == "":
        return ""
    if field in ("min_input_voltage", "max_error_percentage"):
        return float(text)
    number = int(float(text))
    if field in ("min_freq", "max_freq", "start_freq"):
        return max(HARD_MIN_FREQ, min(HARD_MAX_FREQ, number))
    if field in ("min_volt", "max_volt", "start_volt"):
        return max(HARD_MIN_VOLT, min(HARD_MAX_VOLT, number))
    return number


def format_learned_wall(status, stored):
    wall = (status or {}).get("wall_type") or (stored or {}).get("wall_type") or ""
    freq = (status or {}).get("last_good_freq")
    volt = (status or {}).get("last_good_volt")
    if freq in ("", None):
        freq = (stored or {}).get("last_good_freq") or ""
    if volt in ("", None):
        volt = (stored or {}).get("last_good_volt") or ""
    if wall and freq not in ("", None) and volt not in ("", None):
        return f"{wall} {freq}/{volt}"
    if freq not in ("", None) and volt not in ("", None):
        return f"{freq}/{volt}"
    return wall or "-"


def parse_display_number(value):
    if value in (None, "", "-"):
        return None
    cleaned = str(value).replace("°C", "").replace("°", "").replace("%", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_number(value, digits):
    number = parse_display_number(value)
    if number is None:
        return "-"
    if digits == 0:
        return str(int(round(number)))
    return f"{number:.{digits}f}"


def over_limit(value, limit):
    limit_number = parse_display_number(limit)
    if value is None or limit_number is None:
        return False
    return value > limit_number


def row_state_tag(phase, asic_text, error_text, max_temp, max_error):
    """Color a miner row from its phase, or red when it is offline or past a limit."""
    phase_name = str(phase or "").strip().lower()
    if phase_name == "offline":
        return "alert"
    if over_limit(parse_display_number(asic_text), max_temp):
        return "alert"
    if over_limit(parse_display_number(error_text), max_error):
        return "alert"
    if phase_name == "hold":
        return "hold"
    if phase_name == "climb":
        return "climb"
    if phase_name == "trim":
        return "trim"
    return "idle"


def resource_path(relative_path):
    """Get absolute path to resource (for PyInstaller compatibility)."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


class WrappingButtonRow(tk.Frame):
    """A row of controls that moves onto the next line when the window is narrow."""

    def __init__(self, master, bg):
        super().__init__(master, bg=bg)
        self._widgets = []
        self._last_width = 0
        self.bind("<Configure>", self._reflow)

    def add(self, widget):
        self._widgets.append(widget)

    def reflow(self):
        self._last_width = 0
        self._reflow()

    def _reflow(self, event=None):
        if event is not None and event.widget is not self:
            return
        width = event.width if event is not None and event.width > 1 else self.winfo_width()
        if width <= 1:
            width = self.winfo_screenwidth()
        if width == self._last_width:
            return
        self._last_width = width
        for widget in self._widgets:
            widget.grid_forget()
        columns = self.grid_size()[0]
        for column in range(columns):
            self.columnconfigure(column, minsize=0, weight=0)
        x = 0
        row = 0
        column = 0
        for widget in self._widgets:
            widget.update_idletasks()
            needed = widget.winfo_reqwidth() + 8
            if column and x + needed > width:
                row += 1
                column = 0
                x = 0
            widget.grid(row=row, column=column, padx=(0, 8), pady=(0, 8), sticky="w")
            x += needed
            column += 1


class BitaxeAutotuningApp:
    def __init__(self):
        enable_windows_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("Bitaxe Gamma 601")
        fit_window_to_work_area(self.root)
        self._apply_icon(self.root)
        self.root.configure(bg=BG)
        self.root.resizable(True, True)

        self.running = False
        self.threads = []
        self.stop_event = None
        self._status_refresh_running = False
        self._display_after_id = None
        self._display_pending = False
        self._reset_watcher_started = False
        self._stop_in_progress = False
        self._baseline_reset_running = False
        self._start_pending = False
        self.global_settings_window = None
        self.autotuner_window = None
        self.tree_items_by_ip = {}
        self.miner_type_by_ip = {}

        self._apply_theme()
        self._build_header()
        self._build_toolbar()
        self._build_split()
        self._build_menu()

        self.root.bind_all("<F11>", self.toggle_fullscreen)
        self.root.bind_all("<Escape>", self.exit_fullscreen)

        self._sync_run_buttons()
        self.load_miners_from_config()
        self.root.after_idle(self._reflow_toolbars)
        self.root.after_idle(self._place_split)

    def _apply_icon(self, window):
        if platform.system() != "Windows":
            return
        try:
            window.iconbitmap(resource_path("bitaxe_icon.ico"))
        except Exception:
            pass

    def _apply_theme(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        try:
            style.layout("Miner.Treeview", style.layout("Treeview"))
        except tk.TclError:
            pass
        style.configure(
            "Miner.Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=34,
            font=FONT,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Miner.Treeview.Heading",
            background="#242424",
            foreground=MUTED,
            font=FONT_BOLD,
            relief="flat",
        )
        style.map(
            "Miner.Treeview",
            background=[("selected", SELECTION)],
            foreground=[("selected", TEXT)],
        )
        style.map("Miner.Treeview.Heading", background=[("active", "#242424")])
        style.configure("Vertical.TScrollbar", background=QUIET, troughcolor=BG, borderwidth=0, arrowsize=14)

    def _button(self, parent, text, command, kind="quiet"):
        styles = {
            "accent": (ACCENT, ACCENT_TEXT, "#f0b429"),
            "danger": (DANGER, TEXT, "#8a3c3c"),
            "quiet": (QUIET, TEXT, "#3a3a3a"),
        }
        bg, fg, active = styles[kind]
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=FONT,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            disabledforeground=fg,
            relief=tk.FLAT,
            bd=0,
            padx=14,
            pady=10,
            highlightthickness=1,
            highlightbackground=EDGE,
            highlightcolor=ACCENT,
            cursor="hand2",
        )

    def _entry(self, parent, width=16):
        return tk.Entry(
            parent,
            width=width,
            font=FONT,
            bg=FIELD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#3a3a3a",
            highlightcolor=ACCENT,
        )

    def _checkbutton(self, parent, text, variable):
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            font=FONT,
            bg=BG,
            fg=TEXT,
            selectcolor=FIELD_BG,
            activebackground=BG,
            activeforeground=TEXT,
            highlightthickness=0,
        )

    def _dialog(self, title):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.configure(bg=BG)
        window.transient(self.root)
        self._apply_icon(window)
        return window

    def _fit_dialog(self, window, min_width, min_height):
        window.update_idletasks()
        width = max(window.winfo_reqwidth() + 12, min_width)
        height = max(window.winfo_reqheight() + 12, min_height)
        screen_w = max(320, window.winfo_screenwidth() - 40)
        screen_h = max(240, window.winfo_screenheight() - 80)
        width = min(width, screen_w)
        height = min(height, screen_h)
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        window.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
        window.minsize(min(min_width, int(width)), min(min_height, int(height)))

    def _section(self, parent, title):
        tk.Label(parent, text=title, bg=BG, fg=TEXT, font=FONT_SECTION).pack(anchor="w", pady=(12, 4))

    def _hint(self, parent, text):
        tk.Label(
            parent, text=text, bg=BG, fg=MUTED, font=FONT_SMALL, wraplength=460, justify=tk.LEFT
        ).pack(anchor="w", pady=(0, 6))

    def _build_header(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill=tk.X, padx=16, pady=(14, 8))
        self.fullscreen_button = self._button(header, "Fullscreen", self.toggle_fullscreen, "quiet")
        self.fullscreen_button.pack(side=tk.RIGHT)
        tk.Label(header, text="Bitaxe Gamma 601", bg=BG, fg=TEXT, font=FONT_TITLE).pack(side=tk.LEFT)
        self.status_pill = tk.Label(header, text="Idle", bg=QUIET, fg=MUTED, font=FONT_BOLD, padx=10, pady=4)
        self.status_pill.pack(side=tk.LEFT, padx=(16, 8))
        self.updated_label = tk.Label(header, text="Updated --:--:--", bg=BG, fg=MUTED, font=FONT)
        self.updated_label.pack(side=tk.LEFT)

    def _build_split(self):
        self.split = tk.PanedWindow(
            self.root,
            orient=tk.VERTICAL,
            bg=BG,
            bd=0,
            sashwidth=8,
            sashrelief=tk.FLAT,
            sashpad=0,
            opaqueresize=True,
            showhandle=False,
        )
        self.split.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))
        self._split_placed = False
        self.split.bind("<Map>", lambda _event: self.root.after_idle(self._place_split))
        self._build_table()
        self._build_log()

    def _build_table(self):
        table_frame = tk.Frame(
            self.split,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=EDGE,
            highlightcolor=EDGE,
        )
        self.table_frame = table_frame
        self.split.add(table_frame, minsize=120, stretch="never")
        self.tree = ttk.Treeview(
            table_frame,
            columns=TREE_COLUMNS,
            show="headings",
            style="Miner.Treeview",
            selectmode="browse",
        )
        widths = {
            "Name": 130, "IP": 130, "Freq": 70, "mV": 70, "ASIC": 70, "VR": 70,
            "GH/s": 80, "W": 70, "Phase": 80, "Error": 70, "Setpoint": 150,
        }
        for column in TREE_COLUMNS:
            self.tree.heading(column, text=column, anchor="center")
            anchor = "w" if column in ("Name", "Setpoint") else "center"
            self.tree.column(column, width=widths[column], minwidth=56, anchor=anchor, stretch=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        for name, color in (
            ("idle", PANEL),
            ("hold", HOLD),
            ("climb", CLIMB),
            ("trim", TRIM),
            ("alert", ALERT),
        ):
            self.tree.tag_configure(name, background=color, foreground=TEXT)
        self.tree.tag_configure("selected", background=SELECTION, foreground=TEXT)
        self.empty_label = tk.Label(
            table_frame,
            text="No miners yet. Scan the network or add an IP.",
            bg=PANEL,
            fg=MUTED,
            font=FONT,
        )
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-Button-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self.show_tree_menu)

    def _build_toolbar(self):
        self.toolbar = tk.Frame(self.root, bg=BG)
        self.toolbar.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.miners_row = WrappingButtonRow(self.toolbar, BG)
        self.miners_row.pack(fill=tk.X)
        self.scan_button = self._button(self.miners_row, "Scan Network", self.scan_network)
        self.add_button = self._button(self.miners_row, "Add Miner", self.add_miner)
        self.delete_button = self._button(self.miners_row, "Remove Miner", self.delete_miner, "danger")
        self.global_settings_button = self._button(self.miners_row, "Global Settings", self.open_global_settings)
        self.autotuner_settings_button = self._button(
            self.miners_row, "AutoTuner Settings", self.open_autotuner_settings
        )
        for button in (
            self.scan_button,
            self.add_button,
            self.delete_button,
            self.global_settings_button,
            self.autotuner_settings_button,
        ):
            self.miners_row.add(button)

        self.tuner_row = WrappingButtonRow(self.toolbar, BG)
        self.tuner_row.pack(fill=tk.X)
        self.start_button = self._button(self.tuner_row, "Start Autotuner", self.start_autotuning, "accent")
        self.stop_button = self._button(self.tuner_row, "Stop Autotuner", self.stop_autotuning)
        self.reset_baseline_button = self._button(
            self.tuner_row, "Reset All to Baseline", self.reset_to_baseline, "danger"
        )
        for button in (self.start_button, self.stop_button, self.reset_baseline_button):
            self.tuner_row.add(button)

    def _build_log(self):
        log_frame = tk.Frame(
            self.split,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=EDGE,
            highlightcolor=EDGE,
        )
        self.log_frame = log_frame
        tk.Label(log_frame, text="Activity", bg=PANEL, fg=MUTED, font=FONT_BOLD).pack(
            anchor="w", padx=10, pady=(8, 0)
        )
        self.log_output = scrolledtext.ScrolledText(
            log_frame,
            height=4,
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            highlightthickness=0,
            wrap=tk.WORD,
            padx=8,
            pady=8,
        )
        self.log_output.pack(fill=tk.BOTH, expand=True)
        self.log_output.tag_configure("success", foreground="#8fd19a")
        self.log_output.tag_configure("warning", foreground="#e0b15a")
        self.log_output.tag_configure("error", foreground="#f0a0a0")
        self.log_output.tag_configure("info", foreground=TEXT)
        self.log_output.bind("<Key>", self._log_key)
        self.log_output.bind("<<Paste>>", lambda _event: "break")
        self.split.add(log_frame, minsize=140, stretch="always")

    def _build_menu(self):
        self.tree_menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=PANEL,
            fg=TEXT,
            activebackground=SELECTION,
            activeforeground=TEXT,
        )
        self.tree_menu.add_command(label="Edit Miner Settings", command=self.edit_miner_settings)
        self.tree_menu.add_command(label="Refresh", command=self.refresh_selected_miner)
        self.tree_menu.add_command(label="Restart Miner", command=self.restart_selected_miner)
        self.tree_menu.add_command(label="Open Miner Web UI", command=self.open_miner_webpage)
        self.tree_menu.add_separator()
        self.tree_menu.add_command(label="Remove Miner", command=self.delete_miner)

    def _reflow_toolbars(self):
        for row in (self.miners_row, self.tuner_row):
            row.reflow()

    def _place_split(self, tries=0):
        """Size the table to the current rows and give the rest of the window to the log."""
        if self._split_placed or not self.root.winfo_exists():
            return
        self.root.update_idletasks()
        height = self.split.winfo_height()
        if height <= 1:
            if tries < 10:
                self.root.after(50, lambda: self._place_split(tries + 1))
            return
        rows = max(1, len(self.tree.get_children()))
        table_height = min(max(56 + rows * 34, 140), int(height * 0.55))
        if height - table_height < 160:
            table_height = max(120, height - 160)
        try:
            self.split.sash_place(0, 0, int(table_height))
        except tk.TclError:
            return
        self._split_placed = True

    def _set_status(self, text, kind):
        colors = {
            "idle": (QUIET, MUTED),
            "running": (HOLD, "#8fd19a"),
            "stopping": (TRIM, "#e0b15a"),
        }
        bg, fg = colors[kind]
        self.status_pill.configure(text=text, bg=bg, fg=fg)

    def _sync_run_buttons(self):
        if self._stop_in_progress:
            self._set_status("Stopping", "stopping")
        elif self.running:
            self._set_status("Running", "running")
        else:
            self._set_status("Idle", "idle")

        start_locked = self.running or self._stop_in_progress or self._baseline_reset_running or self._start_pending
        if self.running and not self._stop_in_progress:
            self.start_button.configure(
                text="Autotuner Running", state=tk.DISABLED, bg=HOLD, fg=TEXT, disabledforeground=TEXT
            )
            self.stop_button.configure(state=tk.NORMAL, fg=TEXT, disabledforeground=TEXT)
        else:
            self.start_button.configure(
                text="Start Autotuner",
                state=tk.DISABLED if start_locked else tk.NORMAL,
                bg=QUIET if start_locked else ACCENT,
                fg=MUTED if start_locked else ACCENT_TEXT,
                disabledforeground=MUTED if start_locked else ACCENT_TEXT,
            )
            self.stop_button.configure(state=tk.DISABLED, fg=MUTED, disabledforeground=MUTED)
        self.reset_baseline_button.configure(state=tk.DISABLED if self._baseline_reset_running else tk.NORMAL)
        if hasattr(self, "tuner_row"):
            self.tuner_row.reflow()

    def _show_empty(self, show):
        if show:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
            self.empty_label.lift()
        else:
            self.empty_label.place_forget()

    def _set_row_tag(self, item, tag):
        tags = [tag]
        if item in self.tree.selection():
            tags.append("selected")
        self.tree.item(item, tags=tuple(tags))

    def _on_tree_select(self, _event=None):
        selected = set(self.tree.selection())
        for item in self.tree.get_children():
            current = [tag for tag in self.tree.item(item, "tags") if tag != "selected"] or ["idle"]
            if item in selected:
                current.append("selected")
            self.tree.item(item, tags=tuple(current))

    def _on_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        self.edit_miner_settings()

    def _log_key(self, event):
        control = (event.state & 0x4) != 0
        navigation = {
            "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Caps_Lock", "Alt_L", "Alt_R",
        }
        if event.keysym in navigation:
            return None
        if control and event.keysym.lower() in ("c", "a", "left", "right", "up", "down"):
            return None
        return "break"

    def _touch_updated(self):
        self.updated_label.configure(text=f"Updated {datetime.now().strftime('%H:%M:%S')}")

    def _selected_miner(self, warning="Please select a miner first."):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("No Selection", warning, parent=self.root)
            return None, None
        item = selected[0]
        return item, self.tree.item(item, "values")

    def _miner_type(self, ip, fallback="Unknown"):
        return self.miner_type_by_ip.get(ip) or get_miner_defaults(ip).get("type") or fallback

    def open_miner_webpage(self):
        """Opens the selected miner's IP address in the default web browser."""
        _item, values = self._selected_miner("Please select a miner to open.")
        if not values:
            return
        ip = values[COL_IP]
        self.log_message(f"Opening web UI for miner at {ip}", "info")
        webbrowser.open(f"http://{ip}")

    def scan_network(self):
        """Scan an IP range. The window stays open and shows how far the scan has gotten."""
        if str(self.scan_button.cget("state")) == str(tk.DISABLED):
            return
        window = self._dialog("Scan Network")
        tk.Label(window, text="Scan Network", bg=BG, fg=TEXT, font=FONT_SECTION).pack(anchor="w", padx=16, pady=(16, 4))
        self._hint(window, "Only a Gamma 601 is added. Leave this window open to watch the scan.")
        body = tk.Frame(window, bg=BG)
        body.pack(fill=tk.X, padx=16)
        start_entry = self._labeled_entry(body, "Starting IP")
        end_entry = self._labeled_entry(body, "Ending IP")
        progress_label = tk.Label(window, text="", bg=BG, fg=TEXT, font=FONT)
        progress_label.pack(anchor="w", padx=16, pady=(8, 0))

        cancel_event = threading.Event()
        scanning = {"on": False}
        self.scan_button.configure(state=tk.DISABLED)

        def finish(found, cancelled):
            scanning["on"] = False
            if self.root.winfo_exists():
                self.scan_button.configure(state=tk.NORMAL)
                self.load_miners_from_config()
                if cancelled:
                    self.log_message("Scan cancelled.", "warning")
                else:
                    self.log_message(f"Scan finished. Added {len(found)} miner(s).", "success")
            if window.winfo_exists():
                window.destroy()

        def on_close():
            cancel_event.set()
            if scanning["on"]:
                progress_label.configure(text="Stopping scan...")
                return
            self.scan_button.configure(state=tk.NORMAL)
            window.destroy()

        def start_scan():
            start_ip = start_entry.get().strip()
            end_ip = end_entry.get().strip()
            try:
                start = ipaddress.IPv4Address(start_ip)
                end = ipaddress.IPv4Address(end_ip)
            except ipaddress.AddressValueError:
                messagebox.showerror("Error", "Enter a valid starting IP and ending IP.", parent=window)
                return
            if int(end) < int(start):
                messagebox.showerror("Error", "Ending IP must be the same as or after the starting IP.", parent=window)
                return

            scanning["on"] = True
            cancel_event.clear()
            start_button.configure(state=tk.DISABLED)
            start_entry.configure(state=tk.DISABLED)
            end_entry.configure(state=tk.DISABLED)
            progress_label.configure(text="Starting scan...")
            self.log_message(f"Scanning network from {start_ip} to {end_ip}...", "info")

            def on_progress(index, total, _ip):
                def update(index=index, total=total):
                    if progress_label.winfo_exists():
                        progress_label.configure(text=f"Checking {index} of {total}")
                try:
                    self.root.after(0, update)
                except tk.TclError:
                    cancel_event.set()

            def scan_task():
                found = detect_miners(start_ip, end_ip, on_progress=on_progress, should_cancel=cancel_event.is_set)
                cancelled = cancel_event.is_set()
                try:
                    self.root.after(0, lambda: finish(found, cancelled))
                except tk.TclError:
                    return

            threading.Thread(target=scan_task, daemon=True).start()

        footer = tk.Frame(window, bg=BG)
        footer.pack(fill=tk.X, padx=16, pady=16)
        self._button(footer, "Cancel", on_close).pack(side=tk.RIGHT, padx=(8, 0))
        start_button = self._button(footer, "Start Scan", start_scan, "accent")
        start_button.pack(side=tk.RIGHT)
        window.protocol("WM_DELETE_WINDOW", on_close)
        self._fit_dialog(window, 420, 230)

    def _labeled_entry(self, parent, label, width=18):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill=tk.X, pady=4)
        tk.Label(row, text=label, bg=BG, fg=MUTED, font=FONT).pack(side=tk.LEFT)
        entry = self._entry(row, width)
        entry.pack(side=tk.RIGHT)
        return entry

    def load_miners_from_config(self):
        """Loads miners from config.json into the UI."""
        self.tree.delete(*self.tree.get_children())
        self.tree_items_by_ip = {}
        self.miner_type_by_ip = {}
        miners = get_miners()
        for miner in miners:
            ip = miner["ip"]
            nickname = miner.get("nickname", f"Miner-{ip}")
            self.miner_type_by_ip[ip] = miner.get("type", "Unknown")
            item_id = self.tree.insert("", "end", values=blank_miner_row(nickname, ip), tags=("idle",))
            self.tree_items_by_ip[ip] = item_id
        self._show_empty(not miners)
        self.log_message(f"Loaded {len(miners)} miners.", "success")
        self._kick_miner_display()

    def add_miner(self):
        """Opens a window to manually add a miner."""
        window = self._dialog("Add Miner")
        tk.Label(window, text="Add Miner", bg=BG, fg=TEXT, font=FONT_SECTION).pack(anchor="w", padx=16, pady=(16, 4))
        hint = tk.Label(
            window,
            text="Leave the nickname blank to use the miner's hostname.",
            bg=BG,
            fg=MUTED,
            font=FONT_SMALL,
            wraplength=380,
            justify=tk.LEFT,
        )
        hint.pack(anchor="w", padx=16, pady=(0, 8))
        body = tk.Frame(window, bg=BG)
        body.pack(fill=tk.X, padx=16)
        nickname_entry = self._labeled_entry(body, "Nickname")
        ip_entry = self._labeled_entry(body, "IP Address")
        add_button = None

        def finish_error(text):
            if not window.winfo_exists():
                return
            add_button.configure(state=tk.NORMAL, text="Add")
            messagebox.showerror("Error", text, parent=window)

        def add_entry():
            nickname = nickname_entry.get().strip()
            ip = ip_entry.get().strip()
            if not ip:
                messagebox.showerror("Error", "IP Address is required.", parent=window)
                return
            if any(miner["ip"] == ip for miner in get_miners()):
                messagebox.showerror("Error", f"Miner with IP {ip} already exists.", parent=window)
                return
            add_button.configure(state=tk.DISABLED, text="Checking board...")

            def check_and_add():
                info = get_system_info(ip)

                def finish():
                    if not window.winfo_exists():
                        return
                    if isinstance(info, str):
                        finish_error(info)
                        return
                    if not is_gamma_601(info):
                        finish_error(f"{ip} is not a Bitaxe Gamma 601 (BM1370, board 601).")
                        return
                    miner_type = miner_type_from_info(info)
                    name = miner_name_from_info(info, ip, nickname)
                    add_miner(miner_type, ip, name)
                    if not any(miner["ip"] == ip for miner in get_miners()):
                        finish_error(f"Could not add miner at {ip}.")
                        return
                    self.miner_type_by_ip[ip] = miner_type
                    item_id = self.tree.insert(
                        "", "end", values=blank_miner_row(name, ip), tags=("idle",)
                    )
                    self.tree_items_by_ip[ip] = item_id
                    self._show_empty(False)
                    self._kick_miner_display()
                    self.log_message(f"Miner {name} added.", "success")
                    messagebox.showinfo("Success", f"Miner {name} added successfully.", parent=window)
                    window.destroy()

                try:
                    self.root.after(0, finish)
                except tk.TclError:
                    return

            threading.Thread(target=check_and_add, daemon=True).start()

        footer = tk.Frame(window, bg=BG)
        footer.pack(fill=tk.X, padx=16, pady=16)
        self._button(footer, "Cancel", window.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        add_button = self._button(footer, "Add", add_entry, "accent")
        add_button.pack(side=tk.RIGHT)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        self._fit_dialog(window, 420, 220)

    def delete_miner(self):
        """Deletes the selected miner from the UI and config.json."""
        selected_items = self.tree.selection()
        if not selected_items:
            messagebox.showwarning("No Selection", "Please select a miner to delete.", parent=self.root)
            return

        picked = []
        for item in selected_items:
            values = self.tree.item(item, "values")
            picked.append((item, values[COL_NAME] or values[COL_IP], values[COL_IP]))
        if len(picked) == 1:
            prompt = f"Remove {picked[0][1]} ({picked[0][2]})?"
        else:
            lines = "\n".join(f"- {name} ({ip})" for _item, name, ip in picked)
            prompt = f"Remove these miners?\n\n{lines}"
        if not messagebox.askyesno("Delete Miner", prompt, parent=self.root):
            return

        config = load_config()
        miners = config.get("miners", [])
        for item, _name, ip in picked:
            self.tree.delete(item)
            self.tree_items_by_ip.pop(ip, None)
            self.miner_type_by_ip.pop(ip, None)
            miners = [miner for miner in miners if miner["ip"] != ip]
        config["miners"] = miners
        save_config(config)
        self._show_empty(not self.tree.get_children())
        self.log_message("Miner(s) removed successfully.", "success")

    def refresh_selected_miner(self):
        """Fetches and updates real-time data for the selected miner."""
        item, values = self._selected_miner()
        if not values:
            return
        ip = values[COL_IP]
        self.log_message(f"Refreshing data for miner at {ip}...", "info")

        def fetch():
            miner_data = get_system_info(ip)
            try:
                self.root.after(0, lambda: self._apply_selected_miner(item, ip, miner_data))
            except tk.TclError:
                return

        threading.Thread(target=fetch, daemon=True).start()

    def _live_row_values(self, ip, values, miner_data):
        updated = list(values)
        while len(updated) < len(TREE_COLUMNS):
            updated.append("-")
        updated[COL_FREQ] = format_number(miner_data.get("frequency"), 0)
        updated[COL_MV] = format_number(miner_data.get("coreVoltage"), 0)
        updated[COL_ASIC] = format_number(miner_data.get("temp"), 1)
        updated[COL_VR] = format_number(miner_data.get("vrTemp"), 1)
        updated[COL_HASH] = format_number(miner_data.get("hashRate"), 2)
        updated[COL_WATTS] = format_number(miner_data.get("power"), 2)
        status = get_miner_status(ip)
        updated[COL_PHASE] = status.get("phase") or "-"
        error = miner_data.get("errorPercentage", status.get("error_percentage"))
        updated[COL_ERROR] = "-" if error in (None, "") else f"{format_number(error, 2)}%"
        updated[COL_SETPOINT] = format_learned_wall(status, get_miner_defaults(ip))
        return updated

    def _tag_for_values(self, ip, values):
        stored = get_miner_defaults(ip)
        return row_state_tag(
            values[COL_PHASE],
            values[COL_ASIC],
            values[COL_ERROR],
            stored.get("max_temp"),
            stored.get("max_error_percentage"),
        )

    def _mark_offline(self, item):
        if item not in self.tree.get_children():
            return
        values = list(self.tree.item(item, "values"))
        while len(values) < len(TREE_COLUMNS):
            values.append("-")
        values[COL_PHASE] = "offline"
        self.tree.item(item, values=values)
        self._set_row_tag(item, "alert")

    def _apply_selected_miner(self, item, ip, miner_data):
        if not self.root.winfo_exists() or item not in self.tree.get_children():
            return
        if isinstance(miner_data, str) or not isinstance(miner_data, dict):
            self._mark_offline(item)
            self.log_message(f"Error fetching miner data from {ip}: {miner_data}", "error")
            self._touch_updated()
            return
        values = self._maybe_adopt_hostname(ip, miner_data, self.tree.item(item, "values"))
        updated = self._live_row_values(ip, values, miner_data)
        self.tree.item(item, values=updated)
        self._set_row_tag(item, self._tag_for_values(ip, updated))
        self._touch_updated()
        self.log_message(f"Refreshed data for miner at {ip}.", "success")

    def edit_miner_settings(self):
        """Opens a window to edit a miner's nickname and IP address."""
        _item, values = self._selected_miner()
        if not values:
            return
        miner_nickname = values[COL_NAME]
        miner_ip = values[COL_IP]
        miner_type = self._miner_type(miner_ip)

        window = self._dialog("Edit Miner Settings")
        tk.Label(window, text="Edit Miner", bg=BG, fg=TEXT, font=FONT_SECTION).pack(anchor="w", padx=16, pady=(16, 8))
        body = tk.Frame(window, bg=BG)
        body.pack(fill=tk.X, padx=16)
        nickname_entry = self._labeled_entry(body, "Nickname", 24)
        nickname_entry.insert(0, miner_nickname)
        ip_entry = self._labeled_entry(body, "IP Address", 24)
        ip_entry.insert(0, miner_ip)

        def apply_miner_settings(new_nickname, new_ip, new_type):
            config = load_config()
            for miner in config["miners"]:
                if miner["ip"] == miner_ip:
                    miner["nickname"] = new_nickname
                    miner["type"] = new_type
                    miner["ip"] = new_ip
                    break
            save_config(config)
            self.log_message(f"Updated miner settings: {new_nickname} ({new_type}) at {new_ip}", "success")
            window.destroy()
            self.load_miners_from_config()

        def save_miner_settings():
            new_nickname = nickname_entry.get().strip()
            new_ip = ip_entry.get().strip()
            if not new_ip:
                messagebox.showerror("Error", "IP Address is required.", parent=window)
                return
            if new_ip != miner_ip and any(miner["ip"] == new_ip for miner in get_miners()):
                messagebox.showerror("Error", f"Miner with IP {new_ip} already exists.", parent=window)
                return
            if new_ip == miner_ip:
                apply_miner_settings(new_nickname, new_ip, miner_type)
                return

            save_button.configure(state=tk.DISABLED, text="Checking board...")

            def check_and_save():
                info = get_system_info(new_ip)

                def finish():
                    if not window.winfo_exists():
                        return
                    save_button.configure(state=tk.NORMAL, text="Save")
                    if isinstance(info, str):
                        messagebox.showerror("Error", info, parent=window)
                        return
                    if not is_gamma_601(info):
                        messagebox.showerror(
                            "Not a Gamma 601",
                            f"{new_ip} is not a Bitaxe Gamma 601 (BM1370, board 601).",
                            parent=window,
                        )
                        return
                    apply_miner_settings(new_nickname, new_ip, miner_type_from_info(info))

                try:
                    self.root.after(0, finish)
                except tk.TclError:
                    return

            threading.Thread(target=check_and_save, daemon=True).start()

        footer = tk.Frame(window, bg=BG)
        footer.pack(fill=tk.X, padx=16, pady=16)
        self._button(footer, "Cancel", window.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        save_button = self._button(footer, "Save", save_miner_settings, "accent")
        save_button.pack(side=tk.RIGHT)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        self._fit_dialog(window, 440, 230)

    def _singleton(self, attr):
        window = getattr(self, attr)
        if window is None:
            return False
        try:
            if window.winfo_exists():
                window.lift()
                return True
        except tk.TclError:
            pass
        setattr(self, attr, None)
        return False

    def open_global_settings(self):
        """Opens a settings window for modifying global autotuner parameters."""
        if self._singleton("global_settings_window"):
            return

        window = self._dialog("Global Settings")
        self.global_settings_window = window
        config = load_config()

        def on_close():
            self.global_settings_window = None
            if window.winfo_exists():
                window.destroy()

        tk.Label(window, text="Global Settings", bg=BG, fg=TEXT, font=FONT_SECTION).pack(
            anchor="w", padx=16, pady=(16, 8)
        )
        body = tk.Frame(window, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=16)

        settings_entries = {}
        groups = (
            (
                "Steps",
                "How far each step moves, and how often the tuner checks the miner.",
                (
                    ("voltage_step", "Voltage step (mV)"),
                    ("frequency_step", "Frequency step (MHz)"),
                    ("monitor_interval", "Monitor interval (sec)"),
                    ("refresh_interval", "Tune interval (sec)"),
                ),
            ),
            (
                "Temperature",
                "Copied onto a miner when it is added.",
                (
                    ("default_target_temp", "Default max ASIC temp (°C)"),
                    ("temp_tolerance", "Temp tolerance (°C)"),
                ),
            ),
        )
        for title, hint, fields in groups:
            group = tk.LabelFrame(body, text=title, bg=BG, fg=TEXT, font=FONT_BOLD, padx=12, pady=8)
            group.pack(fill=tk.X, pady=(0, 10))
            self._hint(group, hint)
            for key, label in fields:
                entry = self._labeled_entry(group, label, 10)
                entry.insert(0, str(config.get(key, "")))
                settings_entries[key] = entry

        schedule = tk.LabelFrame(body, text="Daily reset and flatline", bg=BG, fg=TEXT, font=FONT_BOLD, padx=12, pady=8)
        schedule.pack(fill=tk.X, pady=(0, 10))
        self._hint(schedule, "Restart every miner at a set time, or restart one whose hashrate stops changing.")

        reset_var = tk.BooleanVar(value=config.get("daily_reset_enabled", False))
        self._checkbutton(schedule, "Enable daily miner reset", reset_var).pack(anchor="w", pady=2)
        time_entry = self._labeled_entry(schedule, "Daily reset time (HH:MM)", 10)
        time_entry.insert(0, config.get("daily_reset_time", "03:00"))

        flatline_var = tk.BooleanVar(value=config.get("flatline_detection_enabled", True))
        self._checkbutton(schedule, "Enable flatline hashrate detection", flatline_var).pack(anchor="w", pady=2)
        flatline_entry = self._labeled_entry(schedule, "Flatline repeat count", 10)
        flatline_entry.insert(0, str(config.get("flatline_hashrate_repeat_count", 5)))

        def save_global_settings():
            try:
                new_settings = {key: int(entry.get()) for key, entry in settings_entries.items()}
                new_settings["daily_reset_enabled"] = reset_var.get()
                new_settings["daily_reset_time"] = time_entry.get().strip()
                new_settings["flatline_detection_enabled"] = flatline_var.get()
                new_settings["flatline_hashrate_repeat_count"] = int(flatline_entry.get())
            except ValueError:
                messagebox.showerror("Error", "Please enter valid integer values.", parent=window)
                return
            config.update(new_settings)
            config.pop("enforce_safe_pairing", None)
            save_config(config)
            self.log_message("Global settings updated.", "success")
            on_close()

        footer = tk.Frame(window, bg=BG)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=16)
        self._button(footer, "Cancel", on_close).pack(side=tk.RIGHT, padx=(8, 0))
        self._button(footer, "Save", save_global_settings, "accent").pack(side=tk.RIGHT)
        body.pack_forget()
        body.pack(fill=tk.BOTH, expand=True, padx=16)
        window.protocol("WM_DELETE_WINDOW", on_close)
        self._fit_dialog(window, 520, 560)

    def open_autotuner_settings(self):
        """Edit one miner's limits at a time. Copy and Paste still move a whole form."""
        config = load_config()
        miners = config.get("miners", [])
        if not miners:
            messagebox.showwarning(
                "No Miners Found",
                "Please add a miner first before modifying AutoTuner settings.",
                parent=self.root,
            )
            return
        if self._singleton("autotuner_window"):
            return

        window = self._dialog("AutoTuner Settings")
        self.autotuner_window = window

        def on_close():
            self.autotuner_window = None
            if window.winfo_exists():
                window.destroy()

        tk.Label(window, text="AutoTuner Settings", bg=BG, fg=TEXT, font=FONT_SECTION).pack(
            anchor="w", padx=16, pady=(16, 8)
        )
        body = tk.Frame(window, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=16)

        labels = [f"{miner.get('nickname') or miner['ip']} ({miner['ip']})" for miner in miners]
        working = []
        for miner in miners:
            row = {"ip": miner["ip"], "enabled": bool(miner.get("enabled", False))}
            for field in ALL_AUTOTUNE_FIELDS:
                display = miner.get(field, "")
                if display in ("", None) and field in GAMMA601_LIMITS:
                    display = GAMMA601_LIMITS[field]
                row[field] = "" if display is None else str(display)
            working.append(row)

        list_frame = tk.Frame(body, bg=BG)
        list_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 16))
        listbox = tk.Listbox(
            list_frame,
            bg=PANEL,
            fg=TEXT,
            selectbackground=SELECTION,
            selectforeground=TEXT,
            font=FONT,
            activestyle="none",
            highlightthickness=0,
            relief=tk.FLAT,
            width=28,
            height=16,
            exportselection=False,
        )
        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=list_scroll.set)
        listbox.pack(side=tk.LEFT, fill=tk.Y)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        for label in labels:
            listbox.insert(tk.END, label)

        form = tk.Frame(body, bg=BG)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        form_title = tk.Label(form, text="", bg=BG, fg=TEXT, font=FONT_BOLD)
        form_title.pack(anchor="w")
        self._hint(form, "Limits for the selected miner.")

        enable_var = tk.BooleanVar(value=False)
        enable_check = self._checkbutton(form, "Tune this miner", enable_var)
        enable_check.pack(anchor="w", pady=(4, 0))
        entries = {}

        def add_trio(title, fields):
            self._section(form, title)
            row = tk.Frame(form, bg=BG)
            row.pack(anchor="w")
            for field, label in fields:
                cell = tk.Frame(row, bg=BG)
                cell.pack(side=tk.LEFT, padx=(0, 16))
                tk.Label(cell, text=label, bg=BG, fg=MUTED, font=FONT_SMALL).pack(anchor="w")
                entry = self._entry(cell, 8)
                entry.pack(anchor="w", pady=(2, 0))
                entries[field] = entry

        add_trio("Frequency", FREQ_FIELDS)
        add_trio("Voltage", VOLT_FIELDS)
        self._section(form, "Limits")
        for field, label in LIMIT_FIELDS:
            entries[field] = self._labeled_entry(form, label, 10)

        current = {"index": -1}
        clipboard = {}
        guard = {"on": False}

        def validate_current():
            empty = any(entry.get().strip() == "" for entry in entries.values())
            if empty:
                enable_var.set(False)
                enable_check.configure(state=tk.DISABLED)
            else:
                enable_check.configure(state=tk.NORMAL)

        def store_form():
            index = current["index"]
            if index < 0:
                return
            row = working[index]
            row["enabled"] = bool(enable_var.get()) and str(enable_check.cget("state")) != str(tk.DISABLED)
            for field, entry in entries.items():
                row[field] = entry.get().strip()

        def show_miner(index):
            current["index"] = index
            row = working[index]
            form_title.configure(text=labels[index])
            enable_var.set(bool(row["enabled"]))
            for field, entry in entries.items():
                entry.delete(0, tk.END)
                entry.insert(0, row[field])
            validate_current()

        def on_select(_event=None):
            if guard["on"]:
                return
            selection = listbox.curselection()
            if not selection:
                return
            index = selection[0]
            if index == current["index"]:
                return
            store_form()
            show_miner(index)

        def copy_row():
            clipboard.clear()
            clipboard.update({field: entry.get() for field, entry in entries.items()})

        def paste_row():
            if not clipboard:
                messagebox.showwarning("No Data", "No row has been copied yet.", parent=window)
                return
            for field, entry in entries.items():
                if field in clipboard:
                    entry.delete(0, tk.END)
                    entry.insert(0, clipboard[field])
            validate_current()
            store_form()

        for entry in entries.values():
            entry.bind("<KeyRelease>", lambda _event: validate_current())
        listbox.bind("<<ListboxSelect>>", on_select)
        guard["on"] = True
        listbox.selection_set(0)
        listbox.activate(0)
        guard["on"] = False
        show_miner(0)

        def save_autotuner_settings():
            store_form()
            by_ip = {miner["ip"]: miner for miner in config["miners"]}
            for row in working:
                miner = by_ip.get(row["ip"])
                if miner is None:
                    continue
                for field in ALL_AUTOTUNE_FIELDS:
                    try:
                        miner[field] = parse_autotuner_value(field, row[field])
                    except ValueError:
                        messagebox.showerror(
                            "Error",
                            f"Enter a number for {field.replace('_', ' ')} on {row['ip']}.",
                            parent=window,
                        )
                        return
                if any(miner[field] == "" for field in ALL_AUTOTUNE_FIELDS):
                    miner["enabled"] = False
                else:
                    miner["enabled"] = bool(row["enabled"])
            save_config(config)
            self.log_message("Updated AutoTuner settings for all miners.", "success")
            on_close()

        footer = tk.Frame(window, bg=BG)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=16)
        self._button(footer, "Copy", copy_row).pack(side=tk.LEFT)
        self._button(footer, "Paste", paste_row).pack(side=tk.LEFT, padx=(8, 0))
        self._button(footer, "Cancel", on_close).pack(side=tk.RIGHT, padx=(8, 0))
        self._button(footer, "Save", save_autotuner_settings, "accent").pack(side=tk.RIGHT)
        body.pack_forget()
        body.pack(fill=tk.BOTH, expand=True, padx=16)
        window.protocol("WM_DELETE_WINDOW", on_close)
        self._fit_dialog(window, 760, 560)

    def toggle_fullscreen(self, event=None):
        """Toggle full-screen mode. The header, table, and log stay visible."""
        self._set_fullscreen(not bool(self.root.attributes("-fullscreen")))
        return "break"

    def exit_fullscreen(self, event=None):
        """Exit full-screen mode."""
        self._set_fullscreen(False)
        return "break"

    def _set_fullscreen(self, enabled):
        enabled = bool(enabled)
        self.root.attributes("-fullscreen", enabled)
        self.fullscreen_button.configure(text="Exit fullscreen" if enabled else "Fullscreen")
        if enabled:
            if self.toolbar.winfo_ismapped():
                self.toolbar.pack_forget()
        elif not self.toolbar.winfo_ismapped():
            self.toolbar.pack(fill=tk.X, padx=16, pady=(0, 8), before=self.split)
            self.root.after_idle(self._reflow_toolbars)

    def reset_to_baseline(self):
        """Write factory clocks to every saved miner and forget the learned setpoint."""
        if self._baseline_reset_running:
            self.log_message("A baseline reset is already in progress.", "warning")
            messagebox.showwarning("Reset in Progress", "A baseline reset is already in progress.", parent=self.root)
            return
        if self._autotuner_busy():
            self.log_message("Stop the autotuner before resetting to baseline.", "warning")
            messagebox.showwarning(
                "Autotuner Running",
                "Stop the autotuner before resetting miners to baseline.",
                parent=self.root,
            )
            return

        miners = list(get_miners())
        if not miners:
            messagebox.showwarning(
                "No Miners Found",
                "Please add a miner first before resetting to baseline.",
                parent=self.root,
            )
            return

        confirmed = messagebox.askyesno(
            "Reset All to Baseline",
            f"Set every miner to the Gamma 601 stock clocks ({STOCK_FREQ} MHz / {STOCK_VOLT} mV) "
            "and forget the saved setpoint?\n\n"
            "The next Start Autotuner will climb or step down from there.",
            parent=self.root,
        )
        if not confirmed:
            return

        self._baseline_reset_running = True
        self._sync_run_buttons()
        self.log_message(f"Resetting miners to {STOCK_FREQ} MHz / {STOCK_VOLT} mV.", "info")

        def finish():
            self._baseline_reset_running = False
            if not self.root.winfo_exists():
                return
            self._sync_run_buttons()
            self.log_message(
                f"Baseline reset finished. Start Autotuner to tune from {STOCK_FREQ} MHz / {STOCK_VOLT} mV.",
                "success",
            )

        def work():
            try:
                reset_miners_to_baseline(miners, self.log_message, parallel=True)
            finally:
                try:
                    self.root.after(0, finish)
                except tk.TclError:
                    self._baseline_reset_running = False

        threading.Thread(target=work, daemon=True).start()

    def _autotuner_busy(self):
        return self.running or self._stop_in_progress or any(thread.is_alive() for thread in self.threads)

    def start_autotuning(self):
        """Starts autotuning miners using the latest saved AutoTuner settings."""
        if self._baseline_reset_running:
            self.log_message("Wait for the baseline reset to finish before starting.", "warning")
            return
        if self._start_pending or self.running or self._stop_in_progress or any(
            thread.is_alive() for thread in self.threads
        ):
            self.log_message("Autotuner is already running.", "warning")
            return

        self._start_pending = True
        self._sync_run_buttons()
        try:
            config = load_config()
            interval = config.get("monitor_interval", 5)
            self.log_message("Checking AutoTuner settings before starting...", "info")

            required_fields = ["min_freq", "max_freq", "min_volt", "max_volt", "max_temp", "max_watts", "max_vr_temp"]
            enabled_miners = [miner for miner in config.get("miners", []) if miner.get("enabled", False)]
            ready_miners = []
            missing_settings = []

            for miner in enabled_miners:
                missing = [
                    field for field in required_fields
                    if field not in miner or miner[field] == "" or miner[field] is None
                ]
                if missing:
                    for field in missing:
                        missing_settings.append((miner["ip"], field))
                else:
                    ready_miners.append(miner)

            if not enabled_miners:
                self.log_message("No miners are enabled for AutoTuning. Please enable at least one miner.", "error")
                messagebox.showwarning(
                    "No Miners Enabled",
                    "No miners are enabled for AutoTuning. Please enable at least one miner in settings.",
                    parent=self.root,
                )
                return

            if missing_settings:
                error_message = "These miners are missing AutoTuner settings and will be skipped:\n\n"
                for ip, field in missing_settings:
                    error_message += f"- Miner {ip}: Missing {field}\n"
                self.log_message(error_message, "error")
                if not ready_miners:
                    messagebox.showerror("Incomplete Settings", error_message, parent=self.root)
                    return
                messagebox.showwarning("Some miners skipped", error_message, parent=self.root)

            self.stop_event = threading.Event()
            self.running = True
            self.threads = []
            self.log_message("Starting autotuning for selected miners...", "success")

            for index, miner in enumerate(ready_miners):
                thread = threading.Thread(
                    target=monitor_and_adjust,
                    args=(
                        miner["ip"],
                        miner.get("type", "Unknown"),
                        interval,
                        self.log_message,
                        miner.get("min_freq"),
                        miner.get("max_freq"),
                        miner.get("min_volt"),
                        miner.get("max_volt"),
                        miner.get("max_temp"),
                        miner.get("max_watts"),
                        miner.get("start_freq", ""),
                        miner.get("start_volt", ""),
                        miner.get("max_vr_temp"),
                    ),
                    kwargs={
                        "stop_event": self.stop_event,
                        "startup_delay": index * STARTUP_STAGGER_SECONDS,
                    },
                    daemon=True,
                )
                thread.start()
                self.threads.append(thread)

            self._kick_miner_display()
            if not self._reset_watcher_started:
                self._reset_watcher_started = True
                threading.Thread(target=self.daily_reset_watcher, daemon=True).start()
        finally:
            self._start_pending = False
            self._sync_run_buttons()

    def stop_autotuning(self):
        """Stops all autotuning processes and waits until those threads leave."""
        if self._stop_in_progress:
            return
        if self.stop_event is None and not self.threads:
            self.running = False
            self._sync_run_buttons()
            return

        self._stop_in_progress = True
        self.running = False
        if self.stop_event is not None:
            self.stop_event.set()
        self._sync_run_buttons()
        self.log_message("Stopping autotuning...", "warning")
        threads = list(self.threads)

        def join_threads():
            for thread in threads:
                thread.join(timeout=12)
            try:
                self.root.after(0, self._finish_stop)
            except tk.TclError:
                self._stop_in_progress = False

        threading.Thread(target=join_threads, daemon=True).start()

    def _finish_stop(self):
        self.threads = [thread for thread in self.threads if thread.is_alive()]
        self._stop_in_progress = False
        self.running = False
        self._sync_run_buttons()
        if self.threads:
            self.log_message("Some tuner threads are still finishing a request.", "warning")
        else:
            self.log_message("Autotuning stopped.", "warning")

    def show_tree_menu(self, event):
        """Displays the right-click menu when a miner is selected."""
        selected_item = self.tree.identify_row(event.y)
        if selected_item:
            self.tree.selection_set(selected_item)
            self.tree_menu.post(event.x_root, event.y_root)

    def update_miner_display(self):
        """Refresh miner status off the Tk thread, then apply the rows on the UI thread."""
        self._display_after_id = None
        if not self.root.winfo_exists():
            return
        if self._status_refresh_running:
            self._display_pending = True
            return
        if not self.tree_items_by_ip:
            self._show_empty(True)
            self._schedule_next_poll()
            return

        self._status_refresh_running = True
        snapshot = list(self.tree_items_by_ip.items())

        def fetch():
            results = []
            try:
                for ip, item in snapshot:
                    results.append((ip, item, get_system_info(ip)))
            finally:
                try:
                    self.root.after(0, lambda: self._apply_miner_display(results))
                except tk.TclError:
                    self._status_refresh_running = False

        threading.Thread(target=fetch, daemon=True).start()

    def _apply_miner_display(self, results):
        self._status_refresh_running = False
        if not self.root.winfo_exists():
            return
        try:
            for ip, item, miner_data in results:
                if item not in self.tree.get_children():
                    continue
                if isinstance(miner_data, str) or not isinstance(miner_data, dict):
                    self._mark_offline(item)
                    continue
                values = self._maybe_adopt_hostname(ip, miner_data, self.tree.item(item, "values"))
                updated = self._live_row_values(ip, values, miner_data)
                self.tree.item(item, values=updated)
                self._set_row_tag(item, self._tag_for_values(ip, updated))
            self._touch_updated()
        finally:
            if self.root.winfo_exists():
                self._continue_display()

    def _continue_display(self):
        if self._display_pending:
            self._display_pending = False
            self.update_miner_display()
            return
        self._schedule_next_poll()

    def _schedule_next_poll(self):
        if not self.root.winfo_exists():
            return
        if self.running:
            try:
                interval = float(load_config().get("monitor_interval", 5))
            except (TypeError, ValueError):
                interval = 5
        else:
            interval = IDLE_POLL_SECONDS
        interval = max(1.0, interval)
        self._display_after_id = self.root.after(int(interval * 1000), self.update_miner_display)

    def _kick_miner_display(self):
        if self._display_after_id is not None:
            try:
                self.root.after_cancel(self._display_after_id)
            except tk.TclError:
                pass
            self._display_after_id = None
        self.update_miner_display()

    def _miner_names(self):
        names = {}
        for miner in get_miners():
            ip = str(miner.get("ip") or "").strip()
            name = str(miner.get("nickname") or "").strip()
            if ip and name:
                names[ip] = name
        return names

    def _maybe_adopt_hostname(self, ip, miner_data, values):
        """Save the AxeOS hostname when this miner still has a placeholder name."""
        current = ""
        if values and len(values) > COL_NAME:
            current = values[COL_NAME]
        stored = str(get_miner_defaults(ip).get("nickname") or current)
        hostname = adopted_hostname(stored, ip, miner_data)
        if not hostname:
            return values
        update_miner(ip, {"nickname": hostname})
        updated = list(values)
        while len(updated) < len(TREE_COLUMNS):
            updated.append("-")
        updated[COL_NAME] = hostname
        return updated

    def log_message(self, message, level="info"):
        """Logs messages to the UI, ensuring updates run on the main thread."""
        message = replace_ips_with_names(message, self._miner_names())
        timestamp = datetime.now().strftime("%H:%M:%S")
        message = f"[{timestamp}] {message}"
        if level not in ("success", "warning", "error", "info"):
            level = "info"

        def _update_log():
            if not self.root.winfo_exists():
                return
            pinned = self.log_output.yview()[1] >= 0.98
            self.log_output.insert(tk.END, message + "\n", level)
            line_count = int(self.log_output.index("end-1c").split(".")[0])
            if line_count > 500:
                self.log_output.delete("1.0", f"{line_count - 500 + 1}.0")
            if pinned:
                self.log_output.see(tk.END)

        try:
            self.root.after(0, _update_log)
        except tk.TclError:
            return

    def daily_reset_watcher(self):
        while True:
            config = load_config()
            if config.get("daily_reset_enabled", False):
                now = datetime.now().strftime("%H:%M")
                if now == config.get("daily_reset_time", "03:00"):
                    self.log_message("Daily reset triggered. Restarting all miners...", "warning")
                    for miner in get_miners():
                        ip = miner["ip"]
                        msg = restart_bitaxe(ip)
                        self.log_message(msg, "warning")
                    time.sleep(60)
            time.sleep(10)

    def restart_selected_miner(self):
        """Restarts the selected miner via API."""
        _item, values = self._selected_miner()
        if not values:
            return
        name = values[COL_NAME] or values[COL_IP]
        ip = values[COL_IP]
        if not messagebox.askyesno("Restart Miner", f"Restart {name} ({ip})?", parent=self.root):
            return

        self.log_message(f"Restarting miner at {ip}...", "warning")

        def restart():
            msg = restart_bitaxe(ip)
            self.log_message(msg, "warning")

            def show_result():
                if self.root.winfo_exists():
                    messagebox.showinfo("Restart Triggered", msg, parent=self.root)

            try:
                self.root.after(0, show_result)
            except tk.TclError:
                return

        threading.Thread(target=restart, daemon=True).start()

    def run(self):
        """Runs the Tkinter event loop."""
        self.root.mainloop()


if __name__ == "__main__":
    try:
        app = BitaxeAutotuningApp()
        app.run()
    except KeyboardInterrupt:
        print("Program interrupted and exiting cleanly...")
