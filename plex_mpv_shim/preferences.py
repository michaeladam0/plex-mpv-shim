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
import json
import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from multiprocessing import Process, Queue

from .gui_theme import apply_theme
from .shader_guide import SHADER_FAMILIES, summary_for

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
         "tip": "The name this client shows as in the Plex 'Cast'/'Play on' menu."},
        {"key": "enable_gui",      "label": "Enable tray GUI",          "kind": "bool", "restart": True,
         "tip": "Show the tray icon and its menus. Turn off to run headless "
                "(command-line only, no tray)."},
        {"key": "enable_osc",      "label": "On-screen controls",       "kind": "bool",
         "tip": "Show mpv's on-screen playback controls (seek bar and buttons) "
                "when you move the mouse over the video."},
        {"key": "sanitize_output", "label": "Hide tokens in logs",      "kind": "bool",
         "tip": "Redact Plex authentication tokens from the log output so logs "
                "are safe to share."},
        {"key": "menu_mouse",      "label": "Mouse in menu",            "kind": "bool", "restart": True,
         "tip": "Allow the mouse to select and click items in the shim's "
                "on-screen menu (loads mpv's mouse script)."},
        {"key": "client_profile",  "label": "Client profile",           "kind": "str",
         "tip": "The capability profile advertised to Plex; it shapes the "
                "server's transcode/direct-play decisions.",
         "warn": "Advanced: leave as-is unless you know why you're changing it."},
        {"key": "client_uuid",     "label": "Client UUID",              "kind": "str", "restart": True,
         "tip": "The unique identity this client reports to Plex.",
         "warn": "Advanced: changing this makes Plex treat this as a brand-new client. Rarely needed."},
    ]),
    ("Network", [
        {"key": "http_port",       "label": "HTTP port",                "kind": "port", "restart": True,
         "tip": "The local TCP port the Plex apps connect to in order to "
                "control this client.",
         "warn": "Must be free and reachable by your Plex clients."},
        {"key": "allow_http",      "label": "Allow plain HTTP",         "kind": "bool", "restart": True,
         "tip": "Accept control connections over plain HTTP in addition to "
                "HTTPS. Usually only needed for older setups."},
        {"key": "enable_play_queue", "label": "Enable play queues",     "kind": "bool",
         "tip": "Use Plex play queues so next/previous and autoplay work across "
                "a whole list rather than a single item."},
    ]),
    ("Playback", [
        {"key": "auto_play",         "label": "Auto-play next",         "kind": "bool",
         "tip": "Automatically start the next item in the queue when the "
                "current one finishes."},
        {"key": "fullscreen",        "label": "Fullscreen",            "kind": "bool",
         "tip": "Start playback in fullscreen."},
        {"key": "always_transcode",  "label": "Always transcode",      "kind": "bool",
         "tip": "Force the server to transcode every stream instead of ever "
                "direct-playing the original file."},
        {"key": "auto_transcode",    "label": "Auto transcode",        "kind": "bool",
         "tip": "Let the server decide when to transcode based on the client "
                "profile (the normal Plex behaviour)."},
        {"key": "adaptive_transcode","label": "Adaptive transcode",    "kind": "bool",
         "tip": "When transcoding, let the server auto-adjust quality to the "
                "available bandwidth mid-stream."},
        {"key": "direct_limit",      "label": "Limit direct play",     "kind": "bool",
         "tip": "Transcode remote streams whose bitrate exceeds the transcode "
                "bitrate below; direct-play anything under it."},
        {"key": "transcode_kbps",    "label": "Transcode bitrate (kbps)", "kind": "int", "min": 1,
         "tip": "Target/maximum transcode bitrate in kbps. Also the threshold "
                "used by 'Limit direct play'."},
        {"key": "audio_ac3passthrough", "label": "AC3 passthrough",    "kind": "bool",
         "tip": "Advertise AC3 passthrough so the server sends AC3 audio "
                "untouched. Needs a receiver that can decode AC3."},
        {"key": "audio_dtspassthrough", "label": "DTS passthrough",    "kind": "bool",
         "tip": "Advertise DTS passthrough so the server sends DTS audio "
                "untouched. Needs a receiver that can decode DTS."},
        {"key": "audio_atmos_passthrough", "label": "Atmos / lossless passthrough", "kind": "bool",
         "tip": "Advertise E-AC3 (Dolby Digital+/Atmos) and TrueHD (Atmos) as "
                "direct-play so the server sends them untouched, keeping the "
                "Atmos metadata. Atmos is lost if the server transcodes. Also "
                "needs mpv.conf 'audio-spdif=ac3,dts,eac3,truehd' and an "
                "Atmos-capable receiver over HDMI."},
    ]),
    ("Subtitles", [
        {"key": "subtitle_size",     "label": "Subtitle size",         "kind": "int", "min": 1,
         "tip": "Subtitle size as a percentage (100 = mpv's default size)."},
        {"key": "subtitle_color",    "label": "Subtitle color",        "kind": "color",
         "tip": "Subtitle colour as hex #AARRGGBB (alpha first), e.g. "
                "#FFFFFFFF is opaque white."},
        {"key": "subtitle_position", "label": "Subtitle position",     "kind": "choice",
         "values": ["bottom", "top", "middle"],
         "tip": "Where subtitles are anchored on screen."},
    ]),
    ("Skip", [
        {"key": "skip_intro_always",   "label": "Always skip intros",   "kind": "bool",
         "tip": "Automatically jump past intros the server has marked, with no "
                "prompt."},
        {"key": "skip_intro_prompt",   "label": "Prompt to skip intros","kind": "bool",
         "depends_on": "skip_intro_always", "depends_value": False,
         "tip": "Show a 'Skip intro' button instead of skipping automatically. "
                "Ignored while 'Always skip intros' is on."},
        {"key": "skip_credits_always", "label": "Always skip credits",  "kind": "bool",
         "tip": "Automatically jump past end credits the server has marked, "
                "with no prompt."},
        {"key": "skip_credits_prompt", "label": "Prompt to skip credits","kind": "bool",
         "depends_on": "skip_credits_always", "depends_value": False,
         "tip": "Show a 'Skip credits' button instead of skipping "
                "automatically. Ignored while 'Always skip credits' is on."},
    ]),
    ("Commands", [
        {"key": "pre_media_cmd",   "label": "Pre-media command",   "kind": "str", "nullable": True,
         "tip": "Shell command run just before the player displays for each "
                "item. The shim waits for it to finish."},
        {"key": "media_ended_cmd", "label": "Media-ended command", "kind": "str", "nullable": True,
         "tip": "Shell command run when all media has finished playing."},
        {"key": "stop_cmd",        "label": "Stop command",        "kind": "str", "nullable": True,
         "tip": "Shell command run after playback is stopped."},
        {"key": "idle_cmd",        "label": "Idle command",        "kind": "str", "nullable": True,
         "tip": "Shell command run after no activity for the idle delay below."},
        {"key": "idle_cmd_delay",  "label": "Idle delay (s)",      "kind": "int", "min": 0,
         "tip": "Seconds of inactivity before the client is considered idle "
                "(triggers the idle command / stop-on-idle)."},
        {"key": "idle_when_paused","label": "Idle when paused",    "kind": "bool",
         "tip": "Count paused playback as inactivity, so the idle timer runs "
                "while paused."},
        {"key": "stop_idle",       "label": "Stop on idle",        "kind": "bool",
         "depends_on": "idle_when_paused",
         "tip": "When idle-while-paused triggers, stop playback entirely. "
                "Only applies when 'Idle when paused' is on."},
    ]),
    ("Input", [
        {"key": "media_key_seek", "label": "Media keys seek",  "kind": "bool",
         "tip": "Make the Previous/Next media keys seek within the current item "
                "(back 15s / forward 30s) instead of changing item."},
        {"key": "seek_up",    "label": "Seek up (s)",    "kind": "int",
         "tip": "Seconds to seek for the Up key (positive = forward)."},
        {"key": "seek_down",  "label": "Seek down (s)",  "kind": "int",
         "tip": "Seconds to seek for the Down key (negative = backward)."},
        {"key": "seek_left",  "label": "Seek left (s)",  "kind": "int",
         "tip": "Seconds to seek for the Left key (negative = backward)."},
        {"key": "seek_right", "label": "Seek right (s)", "kind": "int",
         "tip": "Seconds to seek for the Right key (positive = forward)."},
        {"key": "kb_stop",       "label": "Key: stop",        "kind": "str", "restart": True,
         "tip": "mpv key name that stops playback (e.g. 'q'). Blank = unbound."},
        {"key": "kb_prev",       "label": "Key: previous",    "kind": "str", "restart": True,
         "tip": "mpv key name that plays the previous item (e.g. '<')."},
        {"key": "kb_next",       "label": "Key: next",        "kind": "str", "restart": True,
         "tip": "mpv key name that plays the next item (e.g. '>')."},
        {"key": "kb_watched",    "label": "Key: watched",     "kind": "str", "restart": True,
         "tip": "mpv key name that marks the item watched and skips it."},
        {"key": "kb_unwatched",  "label": "Key: unwatched",   "kind": "str", "restart": True,
         "tip": "mpv key name that marks the item unwatched and quits it."},
        {"key": "kb_menu",       "label": "Key: menu",        "kind": "str", "restart": True,
         "tip": "mpv key name that opens/closes the shim's on-screen menu."},
        {"key": "kb_menu_esc",   "label": "Key: menu escape", "kind": "str", "restart": True,
         "tip": "mpv key name that goes back / closes the menu (e.g. 'esc')."},
        {"key": "kb_menu_ok",    "label": "Key: menu ok",     "kind": "str", "restart": True,
         "tip": "mpv key name that confirms the menu selection (e.g. 'enter')."},
        {"key": "kb_menu_left",  "label": "Key: menu left",   "kind": "str", "restart": True,
         "tip": "mpv key name for menu navigation left."},
        {"key": "kb_menu_right", "label": "Key: menu right",  "kind": "str", "restart": True,
         "tip": "mpv key name for menu navigation right."},
        {"key": "kb_menu_up",    "label": "Key: menu up",     "kind": "str", "restart": True,
         "tip": "mpv key name for menu navigation up."},
        {"key": "kb_menu_down",  "label": "Key: menu down",   "kind": "str", "restart": True,
         "tip": "mpv key name for menu navigation down."},
        {"key": "kb_pause",      "label": "Key: pause",       "kind": "str", "restart": True,
         "tip": "mpv key name that toggles pause (e.g. 'space')."},
        {"key": "kb_debug",      "label": "Key: debug",       "kind": "str", "restart": True,
         "tip": "mpv key name that toggles the mpv stats/debug overlay (e.g. '~')."},
    ]),
    ("MPV", [
        {"key": "mpv_ext",        "label": "Use external mpv",     "kind": "bool", "restart": True,
         "tip": "Play through a separate external mpv process instead of the "
                "built-in embedded player."},
        {"key": "mpv_ext_path",   "label": "External mpv path",    "kind": "path", "nullable": True,
         "restart": True, "check_exists": True, "depends_on": "mpv_ext",
         "section": "External mpv (only used when “Use external mpv” is on)",
         "tip": "Path to the external mpv executable. Leave blank to use the "
                "mpv found on your PATH."},
        {"key": "mpv_ext_ipc",    "label": "External mpv IPC path", "kind": "str", "nullable": True,
         "restart": True, "depends_on": "mpv_ext",
         "tip": "The IPC endpoint the shim uses to control the external mpv. "
                "On Windows this is a named pipe (e.g. mpvpipe or "
                "\\\\.\\pipe\\mpvpipe), not a file on disk; on Linux/macOS it's "
                "a Unix socket path (e.g. /tmp/mpvsocket) mpv creates at "
                "runtime. Not a browsable file.",
         "warn": "Advanced: named pipe (Windows) / Unix socket (Linux/macOS), not a regular file."},
        {"key": "mpv_ext_start",  "label": "Start external mpv",   "kind": "bool", "restart": True,
         "depends_on": "mpv_ext",
         "tip": "Have the shim launch the mpv process itself. Turn off to "
                "attach to an mpv you already started at the IPC path above "
                "(the shim won't spawn one)."},
        {"key": "mpv_ext_no_ovr", "label": "No mpv config override","kind": "bool", "restart": True,
         "depends_on": "mpv_ext",
         "tip": "Don't apply the shim's bundled mpv.conf/input.conf to the "
                "external mpv; use mpv's own user config instead."},
        {"key": "mpv_log_level",  "label": "mpv log level",        "kind": "choice",
         "values": _MPV_LOG_LEVELS, "restart": True,
         "tip": "Verbosity of mpv's messages forwarded into the shim log."},
        {"key": "mpv_log_file",   "label": "Log mpv to file",      "kind": "bool", "restart": True,
         "tip": "Also write mpv's own log from launch to mpv.log in the config "
                "folder (captures startup before the shim attaches)."},
        {"key": "app_log_level",  "label": "App log level",        "kind": "choice",
         "values": _LOG_LEVELS,
         "tip": "Verbosity of the shim's own log."},
        {"key": "log_decisions",  "label": "Log stream decisions", "kind": "bool",
         "tip": "Log the chosen play URL and transcode decision for each item "
                "(useful for debugging playback)."},
    ]),
    ("Video", [
        {"key": "shader_pack_enable",   "label": "Enable shader pack",   "kind": "bool", "restart": True,
         "tip": "Enable the video shader/profile system (quality presets and "
                "custom mpv shaders)."},
        {"key": "shader_pack_custom",   "label": "Custom shader pack",   "kind": "bool", "restart": True,
         "depends_on": "shader_pack_enable",
         "section": "Shader pack (only used when “Enable shader pack” is on)",
         "tip": "Use your own editable copy of the shader pack in the config "
                "folder instead of the bundled one."},
        {"key": "shader_pack_remember", "label": "Remember shader choice","kind": "bool",
         "depends_on": "shader_pack_enable",
         "tip": "Remember the last shader profile you picked and re-apply it "
                "next time."},
        {"key": "shader_pack_profile",  "label": "Shader profile",       "kind": "choice",
         "nullable": True, "values_from": "profiles", "depends_on": "shader_pack_enable",
         "guide": "shaders",
         "tip": "Shader profile to load on startup, from the active shader "
                "pack. Blank = none. See the Shader guide button for a "
                "comparison of the options."},
        {"key": "shader_pack_subtype",  "label": "Shader subtype",       "kind": "choice",
         "values_from": "subtypes", "depends_on": "shader_pack_enable",
         "tip": "Which variant of the shader profiles to use (the quality tier "
                "offered by the pack, e.g. lq/hq)."},
        {"key": "svp_enable",  "label": "Enable SVP",     "kind": "bool", "restart": True,
         "tip": "Integrate with SmoothVideo Project (SVP) for motion "
                "interpolation / frame smoothing."},
        {"key": "svp_url",     "label": "SVP URL",        "kind": "str", "depends_on": "svp_enable",
         "section": "SVP (only used when “Enable SVP” is on)",
         "tip": "Base URL of the SVP web API the shim talks to."},
        {"key": "svp_socket",  "label": "SVP socket",     "kind": "str", "nullable": True, "restart": True,
         "depends_on": "svp_enable",
         "tip": "The IPC endpoint SVP uses to talk to mpv. On Windows this is a "
                "named pipe (default mpvpipe), not a file on disk; on "
                "Linux/macOS a Unix socket path (default /tmp/mpvsocket). Blank "
                "uses the platform default. Not a browsable file.",
         "warn": "Advanced: named pipe (Windows) / Unix socket (Linux/macOS), not a regular file."},
    ]),
]

# Flat view for lookups.
ALL_FIELDS = {f["key"]: f for _cat, fields in SETTINGS_SCHEMA for f in fields}

# Keys that only take effect on (re)start.
RESTART_KEYS = {k for k, f in ALL_FIELDS.items() if f.get("restart")}


APP_NAME = "plex-mpv-shim"


def _read_pack_options(pack_dir):
    """
    Return (profile_pairs, subtypes) from a shader pack dir, or None.
    ``profile_pairs`` is a list of (key, display_name) -- the dropdown shows the
    display name (e.g. "FSRCNNX") but stores the key (e.g. "generic").
    """
    for name in ("pack-next.json", "pack.json"):
        pack_json = os.path.join(pack_dir, name)
        if os.path.exists(pack_json):
            try:
                with open(pack_json, encoding="utf-8") as fh:
                    pack = json.load(fh)
            except Exception:
                return None
            profiles = pack.get("profiles") or {}
            pairs = [(key, profile.get("displayname") or key)
                     for key, profile in profiles.items()]
            subtypes = set()
            for profile in profiles.values():
                for subtype in profile.get("subtype", []):
                    subtypes.add(subtype)
            return pairs, sorted(subtypes)
    return None


def load_shader_options(initial):
    """
    Discover the available shader profiles/subtypes for the dropdowns, reading
    the same pack the player would (custom pack from the config dir when that's
    enabled and present, otherwise the built-in one).

    Returns (profile_pairs, subtypes, custom_available), where profile_pairs is
    a list of (key, display_name).
    """
    try:
        from .utils import get_resource
        from . import conffile
        builtin_dir = get_resource("default_shader_pack")
        custom_dir = conffile.get(APP_NAME, "shader_pack")
    except Exception:
        return [], [], False

    custom_available = _read_pack_options(custom_dir) is not None
    use_custom = bool(initial.get("shader_pack_custom")) and custom_available
    active = _read_pack_options(custom_dir if use_custom else builtin_dir)
    if active is None:
        active = _read_pack_options(builtin_dir) or ([], [])
    return active[0], active[1], custom_available


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
        s = str(raw).strip()
        if field.get("nullable") and s == "":
            return None, None, None
        return s, None, None

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


class _Tooltip:
    """A hover tooltip for a widget, themed to match the window."""

    def __init__(self, widget, text, palette, delay=450):
        self.widget = widget
        self.text = text
        self.palette = palette
        self.delay = delay
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
        except Exception:
            return
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry("+%d+%d" % (x, y))
        tk.Label(tw, text=self.text, justify="left", wraplength=320,
                 bg=self.palette["entry_bg"], fg=self.palette["fg"],
                 relief="solid", borderwidth=1, padx=6, pady=4).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class _ScrollFrame(ttk.Frame):
    """A vertically scrollable frame (canvas + inner frame)."""

    def __init__(self, parent, bg=None):
        super().__init__(parent)
        self.wrap_labels = []
        self._last_width = -1
        self._pending = None

        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, height=1)
        if bg:
            canvas.configure(bg=bg)
        vsb = ttk.Scrollbar(self, orient="vertical")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.canvas = canvas

        self.inner = ttk.Frame(canvas)
        self.window = canvas.create_window((0, 0), window=self.inner, anchor="nw")

        # Clamp the scrollbar: when the content fits, a drag would otherwise
        # slide it down and leave blank space at the top, because Tk's yview
        # doesn't pin the region to the viewport top in that case.
        def _yview(*args):
            if self.inner.winfo_height() <= canvas.winfo_height():
                canvas.yview_moveto(0)
            else:
                canvas.yview(*args)
        vsb.configure(command=_yview)

        # Keep the inner width matched immediately (cheap), but debounce the
        # expensive relayout (re-wrapping labels + recomputing the scroll
        # region). Doing that work synchronously on every <Configure> made
        # resizing crawl, because each wrap change reflowed the content and
        # fired more <Configure> events.
        def _on_canvas(event):
            canvas.itemconfigure(self.window, width=event.width)
            self._schedule()
        canvas.bind("<Configure>", _on_canvas)
        self.inner.bind("<Configure>", lambda _e: self._schedule())

        # Bind the wheel only while the pointer is over this canvas, so the
        # panes don't fight over one global binding, and only scroll when the
        # content actually overflows.
        def _on_wheel(event):
            if self.inner.winfo_height() > canvas.winfo_height():
                canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    def register_wrap(self, label):
        """Register a full-width label to re-wrap to the pane width."""
        self.wrap_labels.append(label)

    def _schedule(self):
        if self._pending is not None:
            try:
                self.canvas.after_cancel(self._pending)
            except Exception:
                pass
        self._pending = self.canvas.after(60, self._apply_layout)

    def _apply_layout(self):
        self._pending = None
        width = self.canvas.winfo_width()
        # Only re-wrap when the width actually changed -- otherwise a height
        # change (from wrapping) would loop back here and re-wrap endlessly.
        if width != self._last_width:
            self._last_width = width
            wrap = max(120, width - 24)
            for label in self.wrap_labels:
                label.configure(wraplength=wrap)
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))


class PreferencesWindowProcess(Process):
    def __init__(self, queue, r_queue, initial, is_playing):
        self.queue = queue
        self.r_queue = r_queue
        self.initial = initial
        self.is_playing = is_playing
        self._vars = {}
        self._widgets = {}
        self._dynamic_values = {}
        self._field_notes = {}
        self._info_photo = None
        # For fields whose dropdown shows a display label but stores a different
        # value (the shader profile): display_label -> stored_value.
        self._value_maps = {}
        # key -> display name, for cross-referencing in the shader guide.
        self._profile_displaynames = {}
        Process.__init__(self)

    def run(self):
        from .utils import set_process_title
        set_process_title("Plex MPV Shim: Preferences")
        root = tk.Tk()
        self.root = root
        root.title("Plex MPV Shim - Preferences")
        # Scale the initial size with the display DPI so it isn't cramped on
        # high-DPI screens (tk scaling is pixels-per-point; 1.333 at 96 dpi).
        try:
            factor = max(1.0, float(root.tk.call("tk", "scaling")) * 0.75)
        except Exception:
            factor = 1.0
        root.geometry("%dx%d" % (int(620 * factor), int(620 * factor)))
        self.palette = apply_theme(root)
        p = self.palette
        self._info_photo = self._build_info_photo()

        # Populate the shader dropdowns from the pack that would actually be
        # used, and note whether a custom pack exists in the config dir. The
        # profile dropdown shows friendly display names ("FSRCNNX") but stores
        # the profile key ("generic").
        profile_pairs, subtypes, custom_available = load_shader_options(self.initial)
        profile_values = [""]
        disp_to_key = {"": ""}
        for key, display in profile_pairs:
            profile_values.append(display)
            disp_to_key[display] = key
            self._profile_displaynames[key] = display
        cur = self.initial.get("shader_pack_profile")
        if cur and cur not in self._profile_displaynames:
            # A stored profile not in the current pack: keep it selectable.
            profile_values.append(cur)
            disp_to_key[cur] = cur
        self._value_maps["shader_pack_profile"] = disp_to_key

        subtype_values = list(subtypes)
        cur = self.initial.get("shader_pack_subtype")
        if cur and cur not in subtype_values:
            subtype_values.append(cur)
        self._dynamic_values = {
            "profiles":  profile_values,
            "subtypes":  subtype_values,
        }
        if custom_available:
            self._field_notes["shader_pack_custom"] = (
                "A custom shader pack was found in the config folder — turn this "
                "on to use it.")
        else:
            self._field_notes["shader_pack_custom"] = (
                "No custom shader pack in the config folder yet; turning this on "
                "copies the built-in one there so you can edit it.")

        # Left-hand category list + stacked content panes. A vertical list never
        # truncates horizontally the way a row of notebook tabs does when the
        # window is narrow, and it scales to any number of categories.
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True, padx=6, pady=6)

        categories = [c for c, _f in SETTINGS_SCHEMA]
        selector = tk.Listbox(body, exportselection=False, activestyle="none",
                              width=16, highlightthickness=0, borderwidth=0)
        selector.configure(bg=p["entry_bg"], fg=p["fg"],
                           selectbackground=p["select"], selectforeground="#ffffff")
        selector.pack(side="left", fill="y")
        for category in categories:
            selector.insert(tk.END, "  " + category)

        content = ttk.Frame(body)
        content.pack(side="left", fill="both", expand=True, padx=(6, 0))
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        self._panes = {}
        for category, fields in SETTINGS_SCHEMA:
            pane = _ScrollFrame(content, bg=p["bg"])
            self._build_fields(pane, fields)
            self._panes[category] = pane

        # Show only the selected pane. Keeping every pane gridded (stacked) made
        # all nine resize and reflow their content on every resize event, which
        # is what made dragging choppy; grid_remove leaves the hidden ones
        # unmanaged so only the visible pane does layout work.
        self._current_pane = None

        def _show(category):
            if self._current_pane is not None:
                self._current_pane.grid_remove()
            pane = self._panes[category]
            pane.grid(row=0, column=0, sticky="nsew")
            self._current_pane = pane

        def _on_select(_event=None):
            sel = selector.curselection()
            if sel:
                _show(categories[sel[0]])
        selector.bind("<<ListboxSelect>>", _on_select)
        selector.selection_set(0)
        _show(categories[0])

        # Grey out settings that don't apply to the selected backend (e.g. the
        # external-mpv options when the built-in player is in use).
        self._wire_dependencies()

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

    def _build_info_photo(self):
        """
        Render the info badge once as an antialiased image sized to the current
        font's line height, so it stays crisp and scales with the display DPI
        (a fixed-pixel Canvas stayed tiny and jagged on high-DPI screens).
        Returns a PhotoImage, or None to fall back to a text glyph.
        """
        try:
            import tkinter.font as tkfont
            from PIL import Image, ImageDraw, ImageTk
        except Exception:
            return None
        try:
            line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        except Exception:
            line = 16
        size = max(12, int(line * 0.95))

        p = self.palette
        scale = 4  # supersample, then downscale for smooth edges
        big = size * scale
        img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([scale, scale, big - scale, big - scale], fill=p["muted"])

        # Knock out a centred "i": dot + stem, both in the background colour.
        cx = big / 2
        dot_r = big * 0.085
        dot_cy = big * 0.31
        draw.ellipse([cx - dot_r, dot_cy - dot_r, cx + dot_r, dot_cy + dot_r], fill=p["bg"])
        stem_w = big * 0.075
        draw.rectangle([cx - stem_w, big * 0.44, cx + stem_w, big * 0.73], fill=p["bg"])

        img = img.resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(img)

    def _info_icon(self, parent):
        """A small info badge that reveals the setting's tooltip on hover."""
        if self._info_photo is not None:
            return ttk.Label(parent, image=self._info_photo, cursor="question_arrow")
        # Fallback if imaging isn't available: a plain glyph.
        return ttk.Label(parent, text="ⓘ", foreground=self.palette["muted"],
                         cursor="question_arrow")

    def _build_fields(self, pane, fields):
        parent = pane.inner
        parent.columnconfigure(1, weight=1)
        row = 0
        for field in fields:
            key = field["key"]
            label = field["label"]
            value = self.initial.get(key)
            kind = field["kind"]

            section = field.get("section")
            if section:
                hdr = tk.Label(parent, text=section, fg=self.palette["fg"],
                               bg=self.palette["bg"], font=("TkDefaultFont", 9, "bold"),
                               justify="left", anchor="w")
                hdr.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=(12, 2))
                pane.register_wrap(hdr)
                row += 1

            # Label plus a small drawn info badge that hints at the tooltip.
            cell = ttk.Frame(parent)
            cell.grid(row=row, column=0, sticky="w", padx=8, pady=4)
            row_label = ttk.Label(cell, text=label)
            row_label.pack(side="left")
            tip = field.get("tip")
            icon = self._info_icon(cell) if tip else None
            if icon is not None:
                icon.pack(side="left", padx=(5, 0))

            if kind == "bool":
                var = tk.BooleanVar(value=bool(value))
                widget = ttk.Checkbutton(parent, variable=var)
                widget.grid(row=row, column=1, sticky="w", padx=8)
            elif kind == "choice":
                # If the field stores a value different from its display label
                # (shader profile), show the label for the stored value.
                vmap = self._value_maps.get(key)
                if vmap:
                    target = value if value is not None else ""
                    initial_str = next((d for d, k in vmap.items() if k == target), None)
                    if initial_str is None:
                        initial_str = "" if value is None else str(value)
                else:
                    initial_str = "" if value is None else str(value)
                var = tk.StringVar(value=initial_str)
                values = field.get("values") or self._dynamic_values.get(
                    field.get("values_from"), [])
                if field.get("guide") == "shaders":
                    # Combobox + a button opening the shader comparison dialog.
                    box = ttk.Frame(parent)
                    box.grid(row=row, column=1, sticky="ew", padx=8)
                    box.columnconfigure(0, weight=1)
                    widget = ttk.Combobox(box, textvariable=var, values=values)
                    widget.grid(row=0, column=0, sticky="ew")
                    guide_btn = ttk.Button(box, text="Shader guide", width=13,
                                           command=self._open_shader_guide)
                    guide_btn.grid(row=0, column=1, padx=(5, 0))
                    self._widgets[key + "__guide"] = guide_btn
                else:
                    widget = ttk.Combobox(parent, textvariable=var, values=values)
                    widget.grid(row=row, column=1, sticky="ew", padx=8)
            elif kind == "path":
                # Entry + a Browse button that opens the OS file/folder picker.
                var = tk.StringVar(value="" if value is None else str(value))
                pathbox = ttk.Frame(parent)
                pathbox.grid(row=row, column=1, sticky="ew", padx=8)
                pathbox.columnconfigure(0, weight=1)
                widget = ttk.Entry(pathbox, textvariable=var)
                widget.grid(row=0, column=0, sticky="ew")
                browse = ttk.Button(pathbox, text="Browse…", width=9,
                                    command=lambda f=field, v=var: self._browse_path(f, v))
                browse.grid(row=0, column=1, padx=(5, 0))
                self._widgets[key + "__browse"] = browse
            else:
                var = tk.StringVar(value="" if value is None else str(value))
                widget = ttk.Entry(parent, textvariable=var)
                widget.grid(row=row, column=1, sticky="ew", padx=8)

            if tip:
                for target in (cell, row_label, icon, widget):
                    if target is not None:
                        _Tooltip(target, tip, self.palette)

            self._vars[key] = var
            self._widgets[key] = widget
            row += 1

            if field.get("guide") == "shaders":
                # A one-line summary of the selected profile, updated live.
                summ = tk.Label(parent, fg=self.palette["muted"], bg=self.palette["bg"],
                                anchor="w", justify="left", font=("TkDefaultFont", 8))
                summ.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8)
                pane.register_wrap(summ)
                row += 1

                def _update_summary(*_args, _var=var, _lbl=summ, _key=key):
                    raw = _var.get()
                    vmap = self._value_maps.get(_key)
                    profile_key = vmap.get(raw, raw) if vmap else raw
                    text = summary_for(profile_key)
                    if text:
                        _lbl.configure(text=text)
                        _lbl.grid()
                    else:
                        _lbl.grid_remove()
                var.trace_add("write", _update_summary)
                _update_summary()

            warn = field.get("warn")
            if warn:
                lbl = tk.Label(parent, text="⚠ " + warn, fg=self.palette["warn"],
                               bg=self.palette["bg"], anchor="w",
                               font=("TkDefaultFont", 8, "bold"), justify="left")
                lbl.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8)
                pane.register_wrap(lbl)
                row += 1

            note = self._field_notes.get(key)
            if note:
                nlbl = tk.Label(parent, text=note, fg=self.palette["muted"],
                                bg=self.palette["bg"], anchor="w", justify="left",
                                font=("TkDefaultFont", 8))
                nlbl.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8)
                pane.register_wrap(nlbl)
                row += 1

    @staticmethod
    def _perf_color(perf):
        low = perf.lower()
        if "light" in low and "heavy" not in low:
            return "#3fa34d"   # green
        if "very heavy" in low and "moderate" not in low:
            return "#d9534f"   # red
        return "#d0932e"       # amber

    def _open_shader_guide(self):
        # Single instance: focus the existing window if it's already open.
        existing = getattr(self, "_guide_win", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass

        p = self.palette
        win = tk.Toplevel(self.root)
        self._guide_win = win
        win.title("Shader Guide")
        win.configure(bg=p["bg"])
        try:
            factor = max(1.0, float(self.root.tk.call("tk", "scaling")) * 0.75)
        except Exception:
            factor = 1.0
        win.geometry("%dx%d" % (int(560 * factor), int(640 * factor)))

        sf = _ScrollFrame(win, bg=p["bg"])
        sf.pack(fill="both", expand=True)
        inner = sf.inner
        inner.columnconfigure(0, weight=1)

        def add(text, *, font=None, fg=None, pady=(0, 0), bold=False, wrap=True):
            lbl = tk.Label(inner, text=text, bg=p["bg"],
                           fg=fg or p["fg"], anchor="w", justify="left",
                           wraplength=460,
                           font=font or ("TkDefaultFont", 9, "bold" if bold else "normal"))
            lbl.grid(sticky="ew", padx=12, pady=pady)
            if wrap:
                sf.register_wrap(lbl)
            return lbl

        add("Base shader profiles compared. Ratings are rough and partly "
            "subjective — use them to steer a choice, then judge by eye on your "
            "own content and GPU.", fg=p["muted"], pady=(12, 4))

        for fam in SHADER_FAMILIES:
            ttk.Separator(inner, orient="horizontal").grid(
                sticky="ew", padx=12, pady=(10, 6))
            add(fam["name"], font=("TkDefaultFont", 11, "bold"), pady=(0, 2))

            perf = tk.Label(inner, bg=p["bg"], anchor="w", justify="left",
                            font=("TkDefaultFont", 9))
            perf.grid(sticky="ew", padx=12, pady=(0, 2))
            perf.configure(text="Performance: %s      Visual impact: %s"
                           % (fam["perf"], fam["impact"]), fg=self._perf_color(fam["perf"]))

            # Map the family's profile keys to what the dropdown shows, so the
            # reader can connect a card to a selectable option.
            labels = [self._profile_displaynames.get(k) for k in fam["profiles"]]
            labels = [l for l in labels if l]
            if labels:
                add("In the dropdown: " + ", ".join(labels), fg=p["muted"],
                    font=("TkDefaultFont", 8), pady=(0, 3))

            add("Best for: " + fam["best_for"], fg=p["muted"], pady=(0, 4))
            add("Pros\n" + "\n".join("  •  " + x for x in fam["pros"]),
                fg="#4c9a5d", pady=(0, 2))
            add("Cons\n" + "\n".join("  •  " + x for x in fam["cons"]),
                fg="#c9645f", pady=(0, 2))
            if fam.get("note"):
                add("Note: " + fam["note"], fg=p["muted"],
                    font=("TkDefaultFont", 8), pady=(0, 4))

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")

        win.protocol("WM_DELETE_WINDOW", win.destroy)
        # Lay out now so the wrap labels size to the window immediately.
        win.after(30, sf._apply_layout)

    def _browse_path(self, field, var):
        """Open the OS file/folder picker for a path field and store the result."""
        from tkinter import filedialog
        current = var.get().strip()
        initial_dir = ""
        if current:
            initial_dir = current if os.path.isdir(current) else os.path.dirname(current)
        if field.get("browse") == "folder":
            chosen = filedialog.askdirectory(
                parent=self.root, title="Select folder", mustexist=False,
                initialdir=initial_dir or None)
        else:
            chosen = filedialog.askopenfilename(
                parent=self.root, title="Select file",
                initialdir=initial_dir or None)
        if chosen:
            # Tk returns forward slashes; store the native form the rest of the
            # app expects (e.g. backslashes for a Windows mpv path).
            var.set(os.path.normpath(chosen))

    def _wire_dependencies(self):
        """
        Enable/disable fields that only apply in a given mode. Each dependent
        field names a controlling boolean (``depends_on``) and the value that
        controller must hold for the field to apply (``depends_value``, default
        True). When the controller doesn't match, the dependent widgets are
        greyed out (their unchanged values are left as-is on save).
        """
        deps = {}
        for key, field in ALL_FIELDS.items():
            controller = field.get("depends_on")
            if controller:
                deps.setdefault(controller, []).append(
                    (key, field.get("depends_value", True)))

        for controller, dependents in deps.items():
            var = self._vars.get(controller)
            if var is None:
                continue

            def make_cb(dep_specs, ctrl_var):
                def cb(*_args):
                    on = bool(ctrl_var.get())
                    for dep_key, want in dep_specs:
                        state = "normal" if on == want else "disabled"
                        # The field widget plus its Browse / guide button, if any.
                        for wkey in (dep_key, dep_key + "__browse", dep_key + "__guide"):
                            widget = self._widgets.get(wkey)
                            if widget is not None:
                                try:
                                    widget.configure(state=state)
                                except Exception:
                                    pass
                return cb

            callback = make_cb(dependents, var)
            var.trace_add("write", callback)
            callback()

    def _collect(self):
        """Returns (changed_dict, errors, warnings) after coercing every field."""
        changed = {}
        errors = []
        warnings = []
        for key, var in self._vars.items():
            field = ALL_FIELDS[key]
            current = self.initial.get(key)
            raw = var.get()
            # Translate a display label back to the stored value (shader profile).
            vmap = self._value_maps.get(key)
            if vmap is not None:
                raw = vmap.get(raw, raw)
            value, err, warn = coerce_and_validate(field, raw, current)
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
        from .utils import set_process_title
        set_process_title("Plex MPV Shim: Restart")
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
