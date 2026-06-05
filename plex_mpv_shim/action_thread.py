import logging
import threading

from .player import playerManager

log = logging.getLogger("action")

class ActionThread(threading.Thread):
    def __init__(self):
        self.trigger        = threading.Event()
        self.halt           = False

        threading.Thread.__init__(self)
    
    def stop(self):
        self.halt = True
        self.join()

    def run(self):
        force_next = False
        while not self.halt:
            try:
                if (playerManager._player and playerManager._media_item) or force_next:
                    playerManager.update()
            except Exception:
                # Keep the thread alive through transient errors (e.g. the mpv
                # core being torn down).
                log.warning("ActionThread::run error during update", exc_info=True)

            force_next = False
            if self.trigger.wait(1):
                force_next = True
                self.trigger.clear()

actionThread = ActionThread()

