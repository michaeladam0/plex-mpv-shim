from pystray import Icon, MenuItem, Menu
from PIL import Image
from collections import deque
import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
from multiprocessing import Process, Queue
import threading
import sys
import time
import logging
import queue
import os.path

APP_NAME = "plex-mpv-shim"
from .conffile import confdir
from .conf import settings
from .preferences import PreferencesWindow, RestartPromptWindow, RESTART_KEYS
from .gui_theme import detect_dark


def _enable_win_dark_menus():
    """
    Opt this process into dark mode so the native tray context menu renders
    dark on Windows 10 1903+. The menu is drawn by the OS (pystray uses a
    classic Win32 popup), so this uxtheme call is the only lever; it's
    undocumented, hence best-effort and wrapped.
    """
    if not sys.platform.startswith("win"):
        return
    if not detect_dark():
        return
    try:
        import ctypes
        uxtheme = ctypes.windll.uxtheme
        # Ordinal 135 = SetPreferredAppMode; 2 = ForceDark. 136 = FlushMenuThemes.
        set_preferred_app_mode = uxtheme[135]
        set_preferred_app_mode(2)
        uxtheme[136]()
    except Exception:
        log.debug("Could not enable dark tray menu.", exc_info=True)

if (sys.platform.startswith("win32") or sys.platform.startswith("cygwin")) and getattr(sys, 'frozen', False):
    # Detect if bundled via pyinstaller.
    # From: https://stackoverflow.com/questions/404744/
    icon_file = os.path.join(sys._MEIPASS, "systray.png")
else:
    icon_file = os.path.join(os.path.dirname(__file__), "systray.png")
log = logging.getLogger('gui_mgr')

# From https://stackoverflow.com/questions/6631299/
# This is for opening the config directory.
def _show_file_darwin(path):
    subprocess.Popen(["open", path])

def _show_file_linux(path):
    subprocess.Popen(["xdg-open", path])

def _show_file_win32(path):
    subprocess.Popen(["explorer", path])

_show_file_func = {'darwin': _show_file_darwin, 
                   'linux': _show_file_linux,
                   'win32': _show_file_win32,
                   'cygwin': _show_file_win32}

try:
    show_file = _show_file_func[sys.platform]
    def open_config():
        show_file(confdir(APP_NAME))
except KeyError:
    open_config = None
    log.warning("Platform does not support opening folders.")

# Setup a log handler for log items.
log_cache = deque([], 1000)
root_logger = logging.getLogger('')

class GUILogHandler(logging.Handler):
    def __init__(self):
        self.callback = None
        super().__init__()

    def emit(self, record):
        log_entry = self.format(record)
        log_cache.append(log_entry)

        if self.callback:
            try:
                self.callback(log_entry)
            except Exception:
                pass

guiHandler = GUILogHandler()
guiHandler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)8s] %(message)s"))
root_logger.addHandler(guiHandler)

# Why am I using another process for the GUI windows?
# Because both pystray and tkinter must run
# in the main thread of their respective process.

class LoggerWindow(threading.Thread):
    def __init__(self):
        self.dead = False
        threading.Thread.__init__(self)

    def run(self):
        self.queue = Queue()
        self.r_queue = Queue()
        self.process = LoggerWindowProcess(self.queue, self.r_queue)
    
        def handle(message):
            self.handle("append", message)
        
        self.process.start()
        handle("\n".join(log_cache))
        guiHandler.callback = handle
        while True:
            action, param = self.r_queue.get()
            if action == "die":
                self._die()
                break
    
    def handle(self, action, params=None):
        self.queue.put((action, params))

    def stop(self, is_source=False):
        self.r_queue.put(("die", None))
    
    def _die(self):
        guiHandler.callback = None
        self.handle("die")
        self.process.terminate()
        self.dead = True

class LoggerWindowProcess(Process):
    def __init__(self, queue, r_queue):
        self.queue = queue
        self.r_queue = r_queue
        Process.__init__(self)

    def update(self):
        try:
            self.text.config(state=tk.NORMAL)
            while True:
                action, param = self.queue.get_nowait()
                if action == "append":
                    # Only follow the tail if the view is already at the bottom,
                    # so a periodic append doesn't yank the user back down while
                    # they're scrolled up reading earlier output. yview()[1] is
                    # 1.0 when the last line is visible.
                    at_bottom = self.text.yview()[1] >= 0.999
                    self.text.config(state=tk.NORMAL)
                    self.text.insert(tk.END, "\n")
                    self.text.insert(tk.END, param)
                    self.text.config(state=tk.DISABLED)
                    if at_bottom:
                        self.text.see(tk.END)
                elif action == "die":
                    self.root.destroy()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.text.after(100, self.update)

    def run(self):
        from .utils import set_process_title
        set_process_title("Plex MPV Shim: Log")
        root = tk.Tk()
        self.root = root
        root.title("Plex MPV Shim - Log")
        text = tk.Text(root)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand = tk.YES)
        text.config(wrap=tk.WORD)
        self.text = text
        yscroll = tk.Scrollbar(command=text.yview)
        text['yscrollcommand'] = yscroll.set
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        text.config(state=tk.DISABLED)
        self.update()
        root.mainloop()
        self.r_queue.put(("die", None))

# Q: OK. So you put Tkinter in it's own process.
#    Now why is Pystray in another process too?!
# A: Because if I don't, MPV and GNOME Appindicator
#    try to access the same resources and cause the
#    entire application to segfault.
#
# I suppose this means I can put the Tkinter GUI back
# into the main process. This is true, but then the
# two need to be merged, which is non-trivial.

class UserInterface:
    def __init__(self):
        self.dead = False
        self.open_player_menu = lambda: None
        self.icon_stop = lambda: None
        self.log_window = None
        self.preferences_window = None
        self.restart_prompt_window = None
        # Set true when a change (or the tray "Restart") asks us to relaunch;
        # mpv_shim.main reads it after the run loop exits and re-execs.
        self.restart_requested = False
        # Deferred restart: relaunch once the current playback ends.
        self.restart_after_playback = False

    def run(self):
        self.queue = Queue()
        self.r_queue = Queue()
        self.process = STrayProcess(self.queue, self.r_queue)
        self.process.start()

        watcher = threading.Thread(target=self._watch_playback, daemon=True)
        watcher.start()

        while True:
            try:
                action, param = self.r_queue.get()
                if action == "die":
                    self._die()
                    break
                elif action == "restart_app":
                    self.restart_requested = True
                    self._die()
                    break
                elif action == "restart_requested_ui":
                    self._prompt_restart()
                elif action == "arm_restart_after_playback":
                    self.restart_after_playback = True
                elif hasattr(self, action):
                    getattr(self, action)()
            except KeyboardInterrupt:
                log.info("Stopping due to CTRL+C.")
                self._die()
                break

    def handle(self, action, params=None):
        self.queue.put((action, params))

    def stop(self):
        self.handle("die")

    def _die(self):
        self.process.terminate()
        self.dead = True

        if self.log_window and not self.log_window.dead:
            self.log_window.stop()
        if self.preferences_window and not self.preferences_window.dead:
            self.preferences_window.stop()
        if self.restart_prompt_window and not self.restart_prompt_window.dead:
            self.restart_prompt_window.stop()

    def _is_playing(self):
        try:
            from .player import playerManager
            return playerManager._media_item is not None
        except Exception:
            return False

    def _watch_playback(self):
        """
        When a deferred restart is armed, relaunch as soon as the current
        playback ends.
        """
        while not self.dead:
            if self.restart_after_playback and not self._is_playing():
                self.restart_after_playback = False
                self.r_queue.put(("restart_app", None))
            time.sleep(1)

    def _prompt_restart(self):
        """
        Tray "Restart" clicked. If nothing is playing, relaunch immediately;
        otherwise pop a dialog offering now / after-playback / cancel.
        """
        if not self._is_playing():
            self.r_queue.put(("restart_app", None))
            return
        if self.restart_prompt_window and not self.restart_prompt_window.dead:
            return
        self.restart_prompt_window = RestartPromptWindow(
            is_playing=True,
            on_now=lambda: self.r_queue.put(("restart_app", None)),
            on_after=lambda: self.r_queue.put(("arm_restart_after_playback", None)),
        )
        self.restart_prompt_window.start()

    def show_preferences(self):
        if self.preferences_window and not self.preferences_window.dead:
            return
        self.preferences_window = PreferencesWindow(
            initial=dict(settings._data),
            is_playing=self._is_playing(),
            on_apply=self.apply_settings,
            on_restart_now=lambda: self.r_queue.put(("restart_app", None)),
            on_restart_after=lambda: self.r_queue.put(("arm_restart_after_playback", None)),
        )
        self.preferences_window.start()

    def apply_settings(self, changed):
        """
        Apply changed settings to the live ``settings`` object (which persists
        to disk and fires listeners) and nudge the few things that can update
        without a restart. Runs on the PreferencesWindow thread, in the main
        process, so it can touch playerManager directly.
        """
        for key, value in changed.items():
            try:
                setattr(settings, key, value)
            except Exception:
                log.warning("Failed to apply setting %s", key, exc_info=True)

        if "app_log_level" in changed:
            mapping = {"debug": logging.DEBUG, "info": logging.INFO,
                       "warning": logging.WARNING, "error": logging.ERROR,
                       "critical": logging.CRITICAL}
            logging.getLogger().setLevel(mapping.get(str(settings.app_log_level).lower(),
                                                      logging.INFO))

        try:
            from .player import playerManager
        except Exception:
            playerManager = None

        if playerManager is not None:
            if "enable_osc" in changed and playerManager._player is not None:
                try:
                    playerManager._player.osc = settings.enable_osc
                except Exception:
                    log.debug("Could not set osc live", exc_info=True)
            if playerManager._media_item is not None and (
                    {"subtitle_size", "subtitle_color", "subtitle_position"} & set(changed)):
                playerManager.put_task(playerManager.update_subtitle_visuals)
            if "fullscreen" in changed and playerManager._player is not None:
                try:
                    playerManager._player.fs = settings.fullscreen
                except Exception:
                    log.debug("Could not set fullscreen live", exc_info=True)

    def login_servers(self):
        is_logged_in = clientManager.try_connect()
        if not is_logged_in:
            self.show_preferences()

    def show_console(self):
        if self.log_window is None or self.log_window.dead:
            self.log_window = LoggerWindow()
            self.log_window.start()
    
    def open_config_brs(self):
        if open_config:
            open_config()
        else:
            log.error("Config opening is not available.")

class STrayProcess(Process):
    def __init__(self, queue, r_queue):
        self.queue = queue
        self.r_queue = r_queue
        Process.__init__(self)

    def run(self):
        from .utils import set_process_title
        set_process_title("Plex MPV Shim: Tray")
        _enable_win_dark_menus()

        def get_wrapper(command):
            def wrapper():
                self.r_queue.put((command, None))
            return wrapper

        def die():
            self.icon_stop()

        menu_items = [
            MenuItem("Preferences", get_wrapper("show_preferences")),
            MenuItem("Show Console", get_wrapper("show_console")),
            MenuItem("Application Menu", get_wrapper("open_player_menu")),
            MenuItem("Open Config Folder", get_wrapper("open_config_brs")),
            Menu.SEPARATOR,
            MenuItem("Restart", get_wrapper("restart_requested_ui")),
            MenuItem("Quit", die)
        ]

        icon = Icon(APP_NAME, menu=Menu(*menu_items))
        icon.icon = Image.open(icon_file)
        self.icon_stop = icon.stop

        def setup(icon: Icon):
            icon.visible = True

        icon.run(setup=setup)
        self.r_queue.put(("die", None))

userInterface = UserInterface()
