@echo off
git pull
rd /s /q __pycache__ dist build
pyinstaller -w -n plex-mpv-shim --version-file version_info.txt --add-binary "libmpv-2.dll;." --add-binary "plex_mpv_shim\systray.png;." --add-data "plex_mpv_shim\mouse.lua;plex_mpv_shim" --add-data "plex_mpv_shim\default_shader_pack;plex_mpv_shim\default_shader_pack" --hidden-import pystray._win32 --hidden-import setproctitle --icon media.ico run.py
if %errorlevel% neq 0 exit /b %errorlevel%
del dist\plex-mpv-shim\plex-mpv-shim.exe.manifest
copy hidpi.manifest dist\plex-mpv-shim\plex-mpv-shim.exe.manifest
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "Plex MPV Shim.iss"
if %errorlevel% neq 0 exit /b %errorlevel%