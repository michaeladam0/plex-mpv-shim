#!/usr/bin/env python3

# python-mpv loads libmpv from %PATH%. Current libmpv builds ship libmpv-2.dll
# (older builds used mpv-1.dll); python-mpv searches for mpv-2.dll, libmpv-2.dll,
# then mpv-1.dll. We prepend this script's folder so a DLL placed next to run.py
# is found.
import os
import sys
import multiprocessing
if sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
    # Detect if bundled via pyinstaller.
    # From: https://stackoverflow.com/questions/404744/
    if getattr(sys, 'frozen', False):
        application_path = sys._MEIPASS
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))
    os.environ["PATH"] = application_path + os.pathsep + os.environ["PATH"]

from plex_mpv_shim.mpv_shim import main
if __name__ == '__main__':
    # https://stackoverflow.com/questions/24944558/pyinstaller-built-windows-exe-fails-with-multiprocessing
    multiprocessing.freeze_support()
    
    main()
