"""
Settings editor reachable from the tray icon.

Like the log window, the tkinter form runs in its own process (tkinter and
pystray each demand their own process/main-thread; see gui_mgr for the gory
details). The child is seeded with a copy of the current settings, the user
edits a form built from SETTINGS_SCHEMA, and on save the child sends the
*changed* keys back to the parent, which owns the live ``settings`` object and
applies them. The child never writes the config file itself -- keeping a single
writer (the main process) avoids the two clobbering each other.
"""

import os
import re
import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from multiprocessing import Process, Queue

from .gui_theme import apply_theme

# ---------------------------------------------------------------------------
# Schema
#
# Each field: key, label, kind, and optional metadata. ``restart`` marks a
# setting the running process only reads at startup / mpv-core init / socket
# bind -- changing it can't take effect live, so we offer a restart. ``warn`` is
# a always-on footgun note rendered under the field (for values we can't
# validate). ``help`` is a short hint. Fields are grouped into tabs by category.
# ---------------------------------------------------------------------------

_LOG_LEVELS      = ["debug", "info", "warning", "error", "critical"]
_MPV_LOG_LEVELS  = ["fatal", "error", "warn", "info", "status", "v", "debug", "trace"]

SETTINGS_SCHEMA = [
    ("General", [
        {"key": "player_name",     "label": "Player name",              "kind": "str",
         "help": "Name shown in the Plex cast menu."},
        {"key": "enable_gui",      "label": "Enable tray GUI",          "kind": "bool", "restart": True,
         "help": "Disable to run headless (command-line only)."},
        {"key": "enable_osc",      "label": "On-screen controls",       "kind": "bool"},
        {"key": "sanitize_output", "label": "Hide tokens in logs",      "kind": "bool"},
        {"key": "menu_mouse",      "label": "Mouse in menu",            "kind": "bool", "restart": True},
        {"key": "client_profile",  "label": "Client profile",           "kind": "str",
         "warn": "Advanced: identifies the client to Plex. Leave as-is unless you know why."},
        {"key": "client_uuid",     "label": "Client UUID",              "kind": "str", "restart": True,
         "warn": "Advanced: changing this makes Plex treat this as a brand-new client. Rarely needed."},
    ]),
    ("Network", [
        {"key": "http_port",       "label": "HTTP port",                "kind": "port", "restart": True,
         "help": "Local control port the Plex apps talk to.",
         "warn": "Must be free and reachable by your Plex clients."},
        {"key": "allow_http",      "label": "Allow plain HTTP",         "kind": "bool", "restart": True},
        {"key": "enable_play_queue", "label": "Enable play queues",     "kind": "bool"},
    ]),
    ("Playback", [
        {"key": "auto_play",         "label": "Auto-play next",         "kind": "bool"},
        {"key": "fullscreen",        "label": "Fullscreen",            "kind": "bool"},
        {"key": "always_transcode",  "label": "Always transcode",      "kind": "bool"},
        {"key": "auto_transcode",    "label": "Auto transcode",        "kind": "bool"},
        {"key": "adaptive_transcode","label": "Adaptive transcode",    "kind": "bool"},
        {"key": "direct_limit",      "label": "Limit direct play",     "kind": "bool"},
        {"key": "transcode_kbps",    "label": "Transcode bitrate (kbps)", "kind": "int", "min": 1},
        {"key": "audio_ac3passthrough", "label": "AC3 passthrough",    "kind": "bool"},
        {"key": "audio_dtspassthrough", "label": "DTS passthrough",    "kind": "bool"},
    ]),
    ("Subtitles", [
        {"key": "subtitle_size",     "label": "Subtitle size",         "kind": "int", "min": 1},
        {"key": "subtitle_color",    "label": "Subtitle color",        "kind": "color",
         "help": "Hex, e.g. #FFFFFFFF (with alpha)."},
        {"key": "subtitle_position", "label": "Subtitle position",     "kind": "choice",
         "values": ["bottom", "top", "middle"]},
    ]),
    ("Skip", [
        {"key": "skip_intro_always",   "label": "Always skip intros",   "kind": "bool"},
        {"key": "skip_intro_prompt",   "label": "Prompt to skip intros","kind": "bool"},
        {"key": "skip_credits_always", "label": "Always skip credits",  "kind": "bool"},
        {"key": "skip_credits_prompt", "label": "Prompt to skip credits","kind": "bool"},
    ]),
    ("Commands", [
        {"key": "pre_media_cmd",   "label": "Pre-media command",   "kind": "str", "nullable": True},
        {"key": "media_ended_cmd", "label": "Media-ended command", "kind": "str", "nullable": True},
        {"key": "stop_cmd",        "label": "Stop command",        "kind": "str", "nullable": True},
        {"key": "idle_cmd",        "label": "Idle command",        "kind": "str", "nullable": True},
        {"key": "idle_cmd_delay",  "label": "Idle delay (s)",      "kind": "int", "min": 0},
        {"key": "idle_when_paused","label": "Idle when paused",    "kind": "bool"},
        {"key": "stop_idle",       "label": "Stop on idle",        "kind": "bool"},
    ]),
    ("Input", [
        {"key": "media_key_seek", "label": "Media keys seek",  "kind": "bool"},
        {"key": "seek_up",    "label": "Seek up (s)",    "kind": "int"},
        {"key": "seek_down",  "label": "Seek down (s)",  "kind": "int"},
        {"key": "seek_left",  "label": "Seek left (s)",  "kind": "int"},
        {"key": "seek_right", "label": "Seek right (s)", "kind": "int"},
        {"key": "kb_stop",       "label": "Key: stop",        "kind": "str", "restart": True},
        {"key": "kb_prev",       "label": "Key: previous",    "kind": "str", "restart": True},
        {"key": "kb_next",       "label": "Key: next",        "kind": "str", "restart": True},
        {"key": "kb_watched",    "label": "Key: watched",     "kind": "str", "restart": True},
        {"key": "kb_unwatched",  "label": "Key: unwatched",   "kind": "str", "restart": True},
        {"key": "kb_menu",       "label": "Key: menu",        "kind": "str", "restart": True},
        {"key": "kb_menu_esc",   "label": "Key: menu escape", "kind": "str", "restart": True},
        {"key": "kb_menu_ok",    "label": "Key: menu ok",     "kind": "str", "restart": True},
        {"key": "kb_menu_left",  "label": "Key: menu left",   "kind": "str", "restart": True},
        {"key": "kb_menu_right", "label": "Key: menu right",  "kind": "str", "restart": True},
        {"key": "kb_menu_up",    "label": "Key: menu up",     "kind": "str", "restart": True},
        {"key": "kb_menu_down",  "label": "Key: menu down",   "kind": "str", "restart": True},
        {"key": "kb_pause",      "label": "Key: pause",       "kind": "str", "restart": True},
        {"key": "kb_debug",      "label": "Key: debug",       "kind": "str", "restart": True},
    ]),
    ("MPV", [
        {"key": "mpv_ext",        "label": "Use external mpv",     "kind": "bool", "restart": True},
        {"key": "mpv_ext_path",   "label": "External mpv path",    "kind": "path", "nullable": True,
         "restart": True, "check_exists": True,
         "help": "Leave blank to use mpv on PATH."},
        {"key": "mpv_ext_ipc",    "label": "External mpv IPC path", "kind": "str", "nullable": True,
         "restart": True,
         "warn": "Advanced: named pipe / socket path for the external mpv IPC."},
        {"key": "mpv_ext_start",  "label": "Start external mpv",   "kind": "bool", "restart": True},
        {"key": "mpv_ext_no_ovr", "label": "No mpv config override","kind": "bool", "restart": True},
        {"key": "mpv_log_level",  "label": "mpv log level",        "kind": "choice",
         "values": _MPV_LOG_LEVELS, "restart": True},
        {"key": "mpv_log_file",   "label": "Log mpv to file",      "kind": "bool", "restart": True},
        {"key": "app_log_level",  "label": "App log level",        "kind": "choice",
         "values": _LOG_LEVELS},
        {"key": "log_decisions",  "label": "Log stream decisions", "kind": "bool"},
    ]),
    ("Video (shaders/SVP)", [
        {"key": "shader_pack_enable",   "label": "Enable shader pack",   "kind": "bool", "restart": True},
        {"key": "shader_pack_custom",   "label": "Custom shader pack",   "kind": "bool", "restart": True},
        {"key": "shader_pack_remember", "label": "Remember shader choice","kind": "bool"},
        {"key": "shader_pack_profile",  "label": "Shader profile",       "kind": "str", "nullable": True},
        {"key": "shader_pack_subtype",  "label": "Shader subtype",       "kind": "str"},
        {"key": "svp_enable",  "label": "Enable SVP",     "kind": "bool", "restart": True},
        {"key": "svp_url",     "label": "SVP URL",        "kind": "str"},
        {"key": "svp_socket",  "label": "SVP socket",     "kind": "str", "nullable": True, "restart": True,
         "warn": "Advanced: SVP IPC socket path."},
    ]),
]

# Flat view for lookups.
ALL_FIELDS = {f["key"]: f for _cat, fields in SETTINGS_SCHEMA for f in fields}

# Keys that only take effect on (re)start.
RESTART_KEYS = {k for k, f in ALL_FIELDS.items() if f.get("restart")}


def _port_free(port):
    """Best-effort check that a TCP port can be bound locally right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def coerce_and_validate(field, raw, current):
    """
    Turn a widget's raw value into the stored type.

    Returns (value, error, warn). ``error`` (a string) blocks the save;
    ``warn`` (a string) is a yes/no confirm the user can override.
    """
    kind = field["kind"]
    label = field["label"]

    if kind == "bool":
        return bool(raw), None, None

    if kind == "int":
        try:
            iv = int(str(raw).strip())
        except ValueError:
            return None, "%s: must be a whole number." % label, None
        mn = field.get("min")
        if mn is not None and iv < mn:
            return None, "%s: must be at least %d." % (label, mn), None
        return iv, None, None

    if kind == "port":
        s = str(raw).strip()
        try:
            iv = int(s)
        except ValueError:
            return None, "%s: must be a number." % label, None
        if not (1 <= iv <= 65535):
            return None, "%s: must be between 1 and 65535." % label, None
        warn = None
        # Only test a *changed* port -- the running server already holds the
        # current one, so it would always look "in use".
        if s != str(current) and not _port_free(iv):
            warn = "%s: port %d appears to be in use. Save anyway?" % (label, iv)
        return s, None, warn

    if kind == "color":
        s = str(raw).strip()
        if not re.match(r"^#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$", s):
            return None, "%s: must be a hex color like #FFFFFFFF." % label, None
        return s, None, None

    if kind == "choice":
        return str(raw).strip(), None, None

    if kind == "path":
        s = str(raw).strip()
        if s == "":
            return None, None, None
        warn = None
        if field.get("check_exists") and not os.path.exists(s):
            warn = "%s: '%s' does not exist. Save anyway?" % (label, s)
        return s, None, warn

    # plain str
    s = str(raw)
    if field.get("nullable") and s.strip() == "":
        return None, None, None
    return s, None, None


class _ScrollFrame(ttk.Frame):
    """A vertically scrollable frame (canvas + inner frame)."""

    def __init__(self, parent, bg=None):
        super().__init__(parent)
        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        if bg:
            canvas.configure(bg=bg)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=self.inner, anchor="nw")

        def _on_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        self.inner.bind("<Configure>", _on_configure)

        def _on_canvas(event):
            canvas.itemconfigure(window, width=event.width)
        canvas.bind("<Configure>", _on_canvas)

        def _on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)


class PreferencesWindowProcess(Process):
    def __init__(self, queue, r_queue, initial, is_playing):
        self.queue = queue
        self.r_queue = r_queue
        self.initial = initial
        self.is_playing = is_playing
        self._vars = {}
        Process.__init__(self)

    def run(self):
        root = tk.Tk()
        self.root = root
        root.title("Plex MPV Shim - Preferences")
        root.geometry("560x620")
        self.palette = apply_theme(root)

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)

        for category, fields in SETTINGS_SCHEMA:
            tab = _ScrollFrame(notebook, bg=self.palette["bg"])
            notebook.add(tab, text=category)
            self._build_fields(tab.inner, fields)

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="Save", command=self._save).pack(side="right", padx=6)

        root.protocol("WM_DELETE_WINDOW", self._cancel)
        # Poll for a "die" instruction from the parent (e.g. app shutting down).
        self._poll_parent()
        root.mainloop()
        self.r_queue.put(("die", None))

    def _poll_parent(self):
        try:
            while True:
                action, _param = self.queue.get_nowait()
                if action == "die":
                    self.root.destroy()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(200, self._poll_parent)

    def _build_fields(self, parent, fields):
        parent.columnconfigure(1, weight=1)
        row = 0
        for field in fields:
            key = field["key"]
            label = field["label"]
            value = self.initial.get(key)
            kind = field["kind"]

            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)

            if kind == "bool":
                var = tk.BooleanVar(value=bool(value))
                ttk.Checkbutton(parent, variable=var).grid(row=row, column=1, sticky="w", padx=8)
            elif kind == "choice":
                var = tk.StringVar(value="" if value is None else str(value))
                combo = ttk.Combobox(parent, textvariable=var, values=field["values"])
                combo.grid(row=row, column=1, sticky="ew", padx=8)
            else:
                var = tk.StringVar(value="" if value is None else str(value))
                ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=8)

            self._vars[key] = var
            row += 1

            note = field.get("help")
            warn = field.get("warn")
            if note:
                ttk.Label(parent, text=note, foreground=self.palette["muted"]).grid(
                    row=row, column=1, sticky="w", padx=8)
                row += 1
            if warn:
                lbl = tk.Label(parent, text="⚠ " + warn, fg=self.palette["warn"],
                               bg=self.palette["bg"],
                               font=("TkDefaultFont", 8, "bold"), wraplength=340, justify="left")
                lbl.grid(row=row, column=1, sticky="w", padx=8)
                row += 1

    def _collect(self):
        """Returns (changed_dict, errors, warnings) after coercing every field."""
        changed = {}
        errors = []
        warnings = []
        for key, var in self._vars.items():
            field = ALL_FIELDS[key]
            current = self.initial.get(key)
            value, err, warn = coerce_and_validate(field, var.get(), current)
            if err:
                errors.append(err)
                continue
            if warn:
                warnings.append(warn)
            if value != current:
                changed[key] = value
        return changed, errors, warnings

    def _save(self):
        changed, errors, warnings = self._collect()
        if errors:
            messagebox.showerror("Invalid settings", "\n".join(errors), parent=self.root)
            return
        for warn in warnings:
            if not messagebox.askyesno("Confirm", warn, parent=self.root):
                return

        if not changed:
            self.root.destroy()
            self.root.quit()
            return

        self.r_queue.put(("apply", changed))

        needs_restart = sorted(k for k in changed if k in RESTART_KEYS)
        if needs_restart:
            self._ask_restart(needs_restart)

        self.root.destroy()
        self.root.quit()

    def _ask_restart(self, keys):
        labels = ", ".join(ALL_FIELDS[k]["label"] for k in keys)
        dlg = tk.Toplevel(self.root)
        dlg.title("Restart required")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.configure(bg=self.palette["bg"])
        msg = ("These changes only take effect after a restart:\n\n  %s\n\n"
               "When would you like to restart?" % labels)
        tk.Label(dlg, text=msg, justify="left", wraplength=380,
                 bg=self.palette["bg"], fg=self.palette["fg"]).pack(padx=16, pady=12)

        choice = {"value": "later"}

        def pick(value):
            choice["value"] = value
            dlg.destroy()

        row = ttk.Frame(dlg)
        row.pack(padx=16, pady=(0, 14))
        ttk.Button(row, text="Restart now", command=lambda: pick("now")).pack(side="left", padx=4)
        if self.is_playing:
            ttk.Button(row, text="After playback",
                       command=lambda: pick("after")).pack(side="left", padx=4)
        ttk.Button(row, text="Later", command=lambda: pick("later")).pack(side="left", padx=4)

        dlg.wait_window()
        self.r_queue.put(("restart_choice", choice["value"]))

    def _cancel(self):
        self.root.destroy()
        self.root.quit()


class PreferencesWindow(threading.Thread):
    """
    Main-process side of the preferences window. Runs the child process and
    dispatches its results to the parent-supplied callbacks (which run here, in
    the main process, and so can touch the live ``settings`` object directly).
    """

    def __init__(self, initial, is_playing, on_apply, on_restart_now, on_restart_after):
        self.dead = False
        self.initial = initial
        self.is_playing = is_playing
        self.on_apply = on_apply
        self.on_restart_now = on_restart_now
        self.on_restart_after = on_restart_after
        threading.Thread.__init__(self)

    def run(self):
        self.queue = Queue()
        self.r_queue = Queue()
        self.process = PreferencesWindowProcess(self.queue, self.r_queue,
                                                self.initial, self.is_playing)
        self.process.start()
        while True:
            action, param = self.r_queue.get()
            if action == "apply":
                try:
                    self.on_apply(param)
                except Exception:
                    pass
            elif action == "restart_choice":
                if param == "now":
                    self.on_restart_now()
                elif param == "after":
                    self.on_restart_after()
            elif action == "die":
                break
        self.dead = True

    def stop(self):
        try:
            self.queue.put(("die", None))
        except Exception:
            pass


class RestartPromptProcess(Process):
    """Small standalone 'when do you want to restart?' dialog."""

    def __init__(self, queue, r_queue, is_playing):
        self.queue = queue
        self.r_queue = r_queue
        self.is_playing = is_playing
        Process.__init__(self)

    def run(self):
        root = tk.Tk()
        self.root = root
        root.title("Restart Plex MPV Shim")
        palette = apply_theme(root)

        if self.is_playing:
            msg = ("Something is playing right now.\n\n"
                   "Restart now (playback will stop) or after the current "
                   "item finishes?")
        else:
            msg = "Restart the shim now?"
        tk.Label(root, text=msg, justify="left", wraplength=360,
                 bg=palette["bg"], fg=palette["fg"]).pack(padx=16, pady=14)

        def pick(value):
            self.r_queue.put(("restart_choice", value))
            root.destroy()
            root.quit()

        row = ttk.Frame(root)
        row.pack(padx=16, pady=(0, 14))
        ttk.Button(row, text="Restart now", command=lambda: pick("now")).pack(side="left", padx=4)
        if self.is_playing:
            ttk.Button(row, text="After playback",
                       command=lambda: pick("after")).pack(side="left", padx=4)
        ttk.Button(row, text="Cancel", command=lambda: pick("cancel")).pack(side="left", padx=4)

        root.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
        root.mainloop()
        self.r_queue.put(("die", None))


class RestartPromptWindow(threading.Thread):
    """Main-process side of the restart prompt; dispatches the user's choice."""

    def __init__(self, is_playing, on_now, on_after, on_cancel=None):
        self.dead = False
        self.is_playing = is_playing
        self.on_now = on_now
        self.on_after = on_after
        self.on_cancel = on_cancel or (lambda: None)
        threading.Thread.__init__(self)

    def run(self):
        self.queue = Queue()
        self.r_queue = Queue()
        self.process = RestartPromptProcess(self.queue, self.r_queue, self.is_playing)
        self.process.start()
        while True:
            action, param = self.r_queue.get()
            if action == "restart_choice":
                if param == "now":
                    self.on_now()
                elif param == "after":
                    self.on_after()
                else:
                    self.on_cancel()
            elif action == "die":
                break
        self.dead = True

    def stop(self):
        try:
            self.queue.put(("die", None))
        except Exception:
            pass
