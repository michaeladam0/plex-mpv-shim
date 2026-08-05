#!/usr/bin/env python3

import logging
import sys
import time
import os.path
import multiprocessing

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="%(asctime)s [%(levelname)8s] %(name)s: %(message)s")

from . import conffile
from .conf import settings
from .gdm import gdm

HTTP_PORT   = 3000
APP_NAME = 'plex-mpv-shim'

log = logging.getLogger('')

logging.getLogger('requests').setLevel(logging.CRITICAL)

def update_gdm_settings(name=None, value=None):
    gdm.clientDetails(settings.client_uuid, settings.player_name,
        settings.http_port, "Plex MPV Shim", "1.0")

def main():
    conf_file = conffile.get(APP_NAME,'conf.json')
    if os.path.isfile('settings.dat'):
        settings.migrate_config('settings.dat', conf_file)
    settings.load(conf_file)
    settings.add_listener(update_gdm_settings)
    
    log_level_mapping = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL
    }
    app_log_level = log_level_mapping.get(settings.app_log_level.lower(), logging.INFO)
    logging.getLogger().setLevel(app_log_level)
    
    if sys.platform.startswith("darwin"):
        multiprocessing.set_start_method('forkserver')

    use_gui = False
    if settings.enable_gui:
        try:
            from .gui_mgr import userInterface
            use_gui = True
        except Exception:
            log.warning("Cannot load GUI. Falling back to command line interface.", exc_info=1)

    if not use_gui:
        from .cli_mgr import userInterface

    from .player import playerManager
    from .timeline import timelineManager
    from .action_thread import actionThread
    from .proxy import proxyClient, notificationListener
    from .client import HttpServer

    update_gdm_settings()
    gdm.start_all()

    log.info("Started GDM service")

    server = HttpServer(int(settings.http_port))
    server.start()

    timelineManager.start()
    playerManager.timeline_trigger = timelineManager.trigger
    actionThread.start()
    playerManager.action_trigger = actionThread.trigger
    proxyClient.start()
    notificationListener.start()
    userInterface.open_player_menu = playerManager.menu.show_menu

    try:
        userInterface.run()
    except KeyboardInterrupt:
        print("")
        log.info("Stopping services...")
    finally:
        playerManager.terminate()
        # Tell the server we've stopped while the proxy/WebSocket are still up,
        # so it tears the session down instead of leaving it hung.
        timelineManager.send_final_stop()
        server.stop()
        timelineManager.stop()
        actionThread.stop()
        proxyClient.stop()
        notificationListener.stop()
        gdm.stop_all()

    if getattr(userInterface, "restart_requested", False):
        _relaunch()

    # Force-exit: python-mpv-jsonipc's IPC reader thread is non-daemon and can
    # outlive the cleanup above, keeping run.exe alive after the tray app closes.
    os._exit(0)

def _relaunch():
    """
    Re-exec the shim in place after a settings change (or tray "Restart")
    requested it. The teardown in main()'s finally block has already run, so the
    old mpv/server/threads are gone before we replace the process image.
    """
    import subprocess
    if getattr(sys, "frozen", False):
        args = [sys.executable] + sys.argv[1:]
    else:
        args = [sys.executable, "-m", APP_NAME.replace("-", "_")] + sys.argv[1:]
    log.info("Relaunching: %s", " ".join(args))
    try:
        os.execv(args[0], args)
    except Exception:
        # execv can fail (e.g. odd frozen layouts); fall back to a fresh process.
        log.warning("execv relaunch failed; spawning a new process instead.", exc_info=True)
        try:
            subprocess.Popen(args)
        except Exception:
            log.error("Relaunch failed.", exc_info=True)

if __name__ == "__main__":
    main()

