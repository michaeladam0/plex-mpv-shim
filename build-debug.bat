@echo off
git pull
rd /s /q __pycache__ dist build
pyinstaller -cF -n plex-mpv-shim --version-file version_info.txt --add-binary "libmpv-2.dll;." --add-binary "plex_mpv_shim\systray.png;." --add-data "plex_mpv_shim\mouse.lua;plex_mpv_shim" --icon media.ico run.py --hidden-import pystray._win32 --hidden-import setproctitle
