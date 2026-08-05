"""
Light/dark theming for the tkinter windows.

tkinter/ttk don't follow the OS colour scheme on their own, so we detect it and
restyle. ``apply_theme`` returns a palette dict the caller uses for raw tk
widgets (Text, Canvas, tk.Label) that ttk styling doesn't reach; it returns the
light palette unchanged when the system is in light mode.

The pystray tray menu is drawn by the OS (native menu) and already follows the
system theme on Windows/macOS, so there's nothing to do there.
"""

import sys
import logging
import subprocess

log = logging.getLogger("gui_theme")

_DARK = {
    "bg":       "#2b2b2b",
    "fg":       "#e0e0e0",
    "entry_bg": "#3c3f41",
    "select":   "#4b6eaf",
    "muted":    "#9a9a9a",
    "warn":     "#ff6b6b",
    "border":   "#555555",
}

_LIGHT = {
    "bg":       "#f0f0f0",
    "fg":       "#000000",
    "entry_bg": "#ffffff",
    "select":   "#3465a4",
    "muted":    "#666666",
    "warn":     "#b00020",
    "border":   "#c0c0c0",
}


def detect_dark():
    """Best-effort system dark-mode detection. Falls back to light."""
    try:
        if sys.platform.startswith("win"):
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
        elif sys.platform == "darwin":
            res = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True)
            return res.stdout.strip() == "Dark"
        else:
            res = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True)
            return "dark" in res.stdout.lower()
    except Exception:
        log.debug("Dark-mode detection failed; assuming light.", exc_info=True)
        return False


def apply_theme(root):
    """
    Style ``root`` (and its ttk widgets) for the current system theme. Returns
    the palette dict so callers can colour raw tk widgets to match.
    """
    dark = detect_dark()
    palette = _DARK if dark else _LIGHT
    if not dark:
        return palette

    from tkinter import ttk
    p = palette
    root.configure(bg=p["bg"])

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=p["bg"], foreground=p["fg"],
                    fieldbackground=p["entry_bg"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["entry_bg"])
    for widget in ("TFrame", "TLabel", "TCheckbutton", "TNotebook", "TButton"):
        style.configure(widget, background=p["bg"], foreground=p["fg"])
    style.configure("TButton", bordercolor=p["border"])
    style.map("TButton",
              background=[("active", p["select"])],
              foreground=[("active", "#ffffff")])
    style.map("TCheckbutton",
              background=[("active", p["bg"])])
    style.configure("TNotebook.Tab", background=p["entry_bg"], foreground=p["fg"])
    style.map("TNotebook.Tab",
              background=[("selected", p["bg"])],
              foreground=[("selected", p["fg"])])
    for widget in ("TEntry", "TCombobox"):
        style.configure(widget, foreground=p["fg"], fieldbackground=p["entry_bg"],
                        insertcolor=p["fg"])
        style.map(widget, fieldbackground=[("readonly", p["entry_bg"])])
    style.configure("Vertical.TScrollbar", background=p["entry_bg"],
                    troughcolor=p["bg"], bordercolor=p["border"], arrowcolor=p["fg"])

    return palette
