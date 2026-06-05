import logging
import os
import sys
import uuid
import requests
import urllib.parse

from threading import RLock, Lock
from queue import Queue
from collections import OrderedDict

from . import conffile
from .utils import synchronous, Timer, get_resource
from .conf import settings
from .menu import OSDMenu
from .media import MediaType

log = logging.getLogger('player')
mpv_log = logging.getLogger('mpv')

python_mpv_available=True
is_using_ext_mpv=False
if not settings.mpv_ext:
    try:
        import mpv
        log.info("Using libmpv1 playback backend.")
    except OSError:
        log.warning("Could not find libmpv1.")
        python_mpv_available=False

if settings.mpv_ext or not python_mpv_available:
    import python_mpv_jsonipc as mpv
    log.info("Using external mpv playback backend.")
    is_using_ext_mpv=True

APP_NAME = 'plex-mpv-shim'

SUBTITLE_POS = {
    "top": 0,
    "bottom": 100,
    "middle": 80,
}

mpv_log_levels = {
    "fatal": mpv_log.error,
    "error": mpv_log.error,
    "warn": mpv_log.warning,
    "info": mpv_log.info
}

def mpv_log_handler(level, prefix, text):
    if level in mpv_log_levels:
        mpv_log_levels[level]("{0}: {1}".format(prefix, text))
    else:
        mpv_log.debug("{0}: {1}".format(prefix, text))

win_utils = None
if sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
    try:
        from . import win_utils
    except ModuleNotFoundError:
        log.warning("win_utils is not available.")

# Q: What is with the put_task call?
# A: Some calls to python-mpv require event processing.
#    put_task is used to deal with the events originating from
#    the event thread, which would cause deadlock if they run there.

class PlayerManager(object):
    """
    Manages the relationship between a ``Player`` instance and a ``Media``
    item.  This is designed to be used as a singleton via the ``playerManager``
    instance in this module.  All communication between a caller and either the
    current ``player`` or ``media`` instance should be done through this class
    for thread safety reasons as all methods that access the ``player`` or
    ``media`` are thread safe.
    """
    def __init__(self):
        self._media_item = None
        self._lock = RLock()
        self._finished_lock = Lock()
        self.last_update = Timer()
        self.__part = 1
        self.timeline_trigger = None
        self.action_trigger = None
        self.external_subtitles = {}
        self.external_subtitles_rev = {}
        self.url = None
        # Per-playback session identifiers (X-Plex-Playback-*), surfaced on the
        # proxy timeline push.
        self.playback_session_id = None
        self.playback_id = None
        self.evt_queue = Queue()
        self.is_in_intro = False
        self.is_in_credits = False
        self.intro_has_triggered = False
        self.credits_has_triggered = False
        # Set when the mpv core is gone (e.g. the external window was closed,
        # which kills the process). Recreated lazily on the next playback.
        self._player_shutdown = False
        # Identifies the current mpv core. Bumped on every (re)create so a quit
        # from a core we've already replaced or torn down is ignored as stale --
        # we only act on the death of the process we currently own.
        self._mpv_generation = 0

        self._init_mpv()

    def _init_mpv(self):
        """
        (Re)create the mpv instance and (re)register every key/event binding.

        Runs at startup and again to recover when the mpv core goes away:
        closing the external mpv window terminates the mpv process, so the next
        playback has to spin up a fresh one.
        """
        # Bump the generation first so a quit from the core we're discarding --
        # including the terminate() below -- is treated as stale (see
        # _on_mpv_quit), not as the user closing the window.
        self._mpv_generation += 1
        generation = self._mpv_generation

        # Tear down the previous core first. With the external backend start_mpv
        # launches a fresh mpv.exe; without this the old (pipe-broken) one
        # lingers and they accumulate. Best-effort -- the pipe may already be dead.
        old_player = getattr(self, "_player", None)
        if old_player is not None:
            try:
                old_player.terminate()
            except Exception:
                log.debug("PlayerManager::_init_mpv could not terminate previous mpv core",
                          exc_info=True)
            self._player = None

        mpv_options = OrderedDict()

        if is_using_ext_mpv:
            mpv_options.update(
                {
                    "start_mpv": settings.mpv_ext_start,
                    "ipc_socket": settings.mpv_ext_ipc,
                    "mpv_location": settings.mpv_ext_path,
                    "player-operation-mode": "cplayer"
                }
            )

        if settings.menu_mouse:
            if is_using_ext_mpv:
                mpv_options["script"] = get_resource("mouse.lua")
            else:
                mpv_options["scripts"] = get_resource("mouse.lua")

        if not (settings.mpv_ext and settings.mpv_ext_no_ovr):
            mpv_options["include"] = conffile.get(APP_NAME, "mpv.conf", True)
            mpv_options["input_conf"] = conffile.get(APP_NAME, "input.conf", True)

        # quit_callback fires the moment mpv's IPC connection dies (window
        # closed, process exited/killed). Without it the shim only notices by
        # failing an operation later -- which is what wedged it into needing a
        # second play. Bind the generation so a late callback from a superseded
        # core is ignored.
        self._player = mpv.MPV(input_default_bindings=True, input_vo_keyboard=True,
                               input_media_keys=True, log_handler=mpv_log_handler,
                               loglevel=settings.mpv_log_level,
                               quit_callback=lambda: self._on_mpv_quit(generation),
                               **mpv_options)
        self.menu = OSDMenu(self)
        if hasattr(self._player, 'osc'):
            self._player.osc = settings.enable_osc
        else:
            log.warning("This mpv version doesn't support on-screen controller.")

        # Wrapper for on_key_press that ignores None.
        def keypress(key):
            def wrapper(func):
                if key is not None:
                    self._player.on_key_press(key)(func)
                return func
            return wrapper

        @self._player.on_key_press('CLOSE_WIN')
        @self._player.on_key_press('STOP')
        @keypress(settings.kb_stop)
        def handle_stop():
            self.stop()
            self.timeline_handle()

        @keypress(settings.kb_prev)
        def handle_prev():
            self.put_task(self.play_prev)

        @keypress(settings.kb_next)
        def handle_next():
            self.put_task(self.play_next)

        @self._player.on_key_press('PREV')
        @self._player.on_key_press('XF86_PREV')
        def handle_media_prev():
            if settings.media_key_seek:
                self._player.command("seek", -15)
            else:
                self.put_task(self.play_prev)

        @self._player.on_key_press('NEXT')
        @self._player.on_key_press('XF86_NEXT')
        def handle_media_next():
            if settings.media_key_seek:
                if self.is_in_intro:
                    self.skip_intro()
                elif self.is_in_credits:
                    self.skip_credits()
                else:
                    self._player.command("seek", 30)
            else:
                self.put_task(self.play_next)

        @keypress(settings.kb_watched)
        def handle_watched():
            self.put_task(self.watched_skip)

        @keypress(settings.kb_unwatched)
        def handle_unwatched():
            self.put_task(self.unwatched_quit)

        @keypress(settings.kb_menu)
        def menu_open():
            if not self.menu.is_menu_shown:
                self.menu.show_menu()
            else:
                self.menu.hide_menu()

        @keypress(settings.kb_menu_esc)
        def menu_back():
            if self.menu.is_menu_shown:
                self.menu.menu_action('back')
            else:
                self._player.command('set', 'fullscreen', 'no')

        @keypress(settings.kb_menu_ok)
        def menu_ok():
            self.menu.menu_action('ok')

        @keypress(settings.kb_menu_left)
        def menu_left():
            if self.menu.is_menu_shown:
                self.menu.menu_action('left')
            else:
                self._player.command("seek", settings.seek_left)

        @keypress(settings.kb_menu_right)
        def menu_right():
            if self.menu.is_menu_shown:
                self.menu.menu_action('right')
            else:
                if self.is_in_intro:
                    self.skip_intro()
                elif self.is_in_credits:
                    self.skip_credits()
                else:
                    self._player.command("seek", settings.seek_right)

        @keypress(settings.kb_menu_up)
        def menu_up():
            if self.menu.is_menu_shown:
                self.menu.menu_action('up')
            else:
                if self.is_in_intro:
                    self.skip_intro()
                elif self.is_in_credits:
                    self.skip_credits()
                else:
                    self._player.command("seek", settings.seek_up)

        @keypress(settings.kb_menu_down)
        def menu_down():
            if self.menu.is_menu_shown:
                self.menu.menu_action('down')
            else:
                self._player.command("seek", settings.seek_down)

        @keypress(settings.kb_pause)
        def handle_pause():
            if self.menu.is_menu_shown:
                self.menu.menu_action('ok')
            else:
                self.toggle_pause()

        # This gives you an interactive python debugger prompt.
        @keypress(settings.kb_debug)
        def handle_debug():
            import pdb
            pdb.set_trace()

        # Fires between episodes.
        @self._player.property_observer('eof-reached')
        def handle_end(_name, reached_end):
            if self._media_item and reached_end:
                has_lock = self._finished_lock.acquire(False)
                self.put_task(self.finished_callback, has_lock)

        # Fires at the end.
        @self._player.property_observer("playback-abort")
        def handle_end_idle(_name, value):
            if self._media_item and value:
                has_lock = self._finished_lock.acquire(False)
                self.put_task(self.finished_callback, has_lock)

        @self._player.event_callback('client-message')
        def handle_client_message(event):
            try:
                # Python-MPV 1.0 uses a class/struct combination now
                if hasattr(event, "as_dict"):
                    event = event.as_dict()
                    if 'event' in event:
                        event['event'] = event['event'].decode('utf-8')
                    if 'args' in event:
                        event['args'] = [d.decode('utf-8') for d in event['args']]

                if "event_id" in event:
                    args = event["event"]["args"]
                else:
                    args = event["args"]
                if len(args) == 0:
                    return
                if args[0] == "shim-menu-select":
                    # Apparently this can happen...
                    if args[1] == "inf":
                        return
                    self.menu.mouse_select(int(args[1]))
                elif args[0] == "shim-menu-click":
                    self.menu.menu_action("ok")
            except Exception:
                log.warning("Error when processing client-message.", exc_info=True)

    # Put a task to the event queue.
    # This ensures the task executes outside
    # of an event handler, which causes a crash.
    def put_task(self, func, *args):
        self.evt_queue.put([func, args])
        if self.action_trigger:
            self.action_trigger.set()

    def _on_mpv_quit(self, generation):
        """
        Called from mpv's socket thread when a core's IPC connection dies. Only
        react if it's the core we currently own (a superseded generation is a
        core we already replaced/tore down).

        Runs on a foreign thread, so it stays cheap and must not take ``_lock``;
        the teardown is handed to the action thread.
        """
        if generation != self._mpv_generation:
            return
        log.info("PlayerManager: owned mpv core exited; flagging for recreation.")
        self._player_shutdown = True
        self.put_task(self._handle_mpv_gone)

    def _handle_mpv_gone(self):
        """
        Action-thread continuation of _on_mpv_quit: stop and clear the media
        item so the session doesn't wedge. The next play recreates the core (via
        _play_media's _player_shutdown check); we don't respawn it here.
        """
        if self._media_item is not None:
            try:
                self.stop()
            except Exception:
                log.debug("PlayerManager::_handle_mpv_gone stop failed", exc_info=True)
        self.timeline_handle()

    # Trigger the timeline to update all
    # clients immediately.
    def timeline_handle(self):
        if self.timeline_trigger:
            self.timeline_trigger.set()

    def skip_marker(self, end_point):
        if self._media_item.media_type == MediaType.VIDEO:
            self._player.playback_time = end_point
            self.timeline_handle()
            return True
        return False

    def skip_intro(self):
        end_point = self._media_item.intro_end
        if self.skip_marker(end_point):
            self.is_in_intro = False

    def skip_credits(self):
        end_point = self._media_item.credits_end
        if self.skip_marker(end_point):
            self.is_in_credits = False

    def check_intro_or_credits(self):
        if ((settings.skip_intro_always or settings.skip_intro_prompt)
            and self._media_item is not None and self._media_item.media_type == MediaType.VIDEO and self._media_item.intro_start is not None
            and self._player.playback_time is not None
            and self._player.playback_time > self._media_item.intro_start
            and self._player.playback_time < self._media_item.intro_end):

            if not self.is_in_intro:
                if settings.skip_intro_always and not self.intro_has_triggered:
                    self.intro_has_triggered = True
                    self.skip_intro()
                    self._player.show_text("Skipped Intro", 3000, 1)
                elif settings.skip_intro_prompt:
                    self._player.show_text("Seek to Skip Intro", 3000, 1)
            self.is_in_intro = True
        else:
            self.is_in_intro = False

        # TODO de-duplicate this code in some way - it's ugly
        if ((settings.skip_credits_always or settings.skip_credits_prompt)
            and self._media_item is not None and self._media_item.media_type == MediaType.VIDEO and self._media_item.credits_start is not None
            and self._player.playback_time is not None
            and self._player.playback_time > self._media_item.credits_start
            and self._player.playback_time < self._media_item.credits_end):

            if not self.is_in_credits:
                if settings.skip_credits_always and not self.credits_has_triggered:
                    self.credits_has_triggered = True
                    self.skip_credits()
                    self._player.show_text("Skipped Credits", 3000, 1)
                elif settings.skip_credits_prompt:
                    self._player.show_text("Seek to Skip Credits", 3000, 1)
            self.is_in_credits = True
        else:
            self.is_in_credits = False


    @synchronous('_lock')
    def update(self):
        self.check_intro_or_credits()
        while not self.evt_queue.empty():
            func, args = self.evt_queue.get()
            func(*args)
        if self._media_item and not self._player.playback_abort:
            if not self.is_paused():
                self.last_update.restart()

    def play(self, media_item, offset=0):
        url = media_item.get_playback_url()
        if not url:        
            log.error("PlayerManager::play no URL found")
            return

        self._play_media(media_item, url, offset)

    @synchronous('_lock')
    def _play_media(self, media_item, url, offset=0):
        # Recreate the core if it went away (external window closed).
        if self._player_shutdown:
            log.info("PlayerManager::_play_media mpv core was shut down; reinitializing.")
            self._init_mpv()
            self._player_shutdown = False

        self.url = url
        self.menu.hide_menu()

        if settings.log_decisions:
            log.debug("Playing: {0}".format(url))

        try:
            self._player.play(self.url)
            self._player.wait_for_property("duration")
        except Exception:
            # Core was dead but unflagged: recreate and retry once.
            log.warning("PlayerManager::_play_media playback failed; reinitializing mpv and retrying.", exc_info=True)
            self._init_mpv()
            self._player_shutdown = False
            self._player.play(self.url)
            self._player.wait_for_property("duration")
        if settings.fullscreen:
            self._player.fs = True
        self._player.force_media_title = media_item.get_proper_title()
        self._media_item  = media_item
        # Fresh playback session for this item.
        self.playback_session_id = str(uuid.uuid4())
        self.playback_id = str(uuid.uuid4())
        self.is_in_intro = False
        self.is_in_credits = False
        self.intro_has_triggered = False
        self.credits_has_triggered = False
        self.update_subtitle_visuals(False)
        self.upd_player_hide()
        self.external_subtitles = {}
        self.external_subtitles_rev = {}

        if win_utils:
            win_utils.raise_mpv()

        if offset > 0:
            self._player.playback_time = offset

        if media_item.media_type == MediaType.VIDEO and not media_item.is_transcode:
            audio_idx = media_item.get_audio_idx()
            if audio_idx is not None:
                log.debug("PlayerManager::play selecting audio stream index=%s" % audio_idx)
                self._player.audio = audio_idx

            sub_idx = media_item.get_subtitle_idx()
            xsub_id = media_item.get_external_sub_id()
            if sub_idx is not None:
                log.debug("PlayerManager::play selecting subtitle index=%s" % sub_idx)
                self._player.sub = sub_idx
            elif xsub_id is not None:
                log.debug("PlayerManager::play selecting external subtitle id=%s" % xsub_id)
                self.load_external_sub(xsub_id)
            else:
                self._player.sub = 'no'

        self._player.pause = False
        self.timeline_handle()
        if self._finished_lock.locked():
            self._finished_lock.release()

    def exec_stop_cmd(self):
        if settings.stop_cmd:
            os.system(settings.stop_cmd)

    @synchronous('_lock')
    def stop(self, playend=False):
        # Do NOT early-return on playback_abort: closing the window (CLOSE_WIN)
        # sets it before handle_stop() runs, and the old guard then skipped the
        # stop, leaving the session running past EOF. While a media item is
        # active we run the same teardown as the EOF path.
        if not self._media_item:
            self.exec_stop_cmd()
            return

        if not playend:
            log.debug("PlayerManager::stop stopping playback of %s" % self._media_item)

        # Must run before _media_item is cleared below.
        self._send_terminal_stop()

        if self._media_item.media_type == MediaType.VIDEO:
            self._media_item.terminate_transcode()

        self._media_item  = None
        # The core may already be gone (window closed); we've notified Plex above.
        try:
            self._player.command("stop")
            self._player.pause = False
        except Exception:
            # Core is gone (e.g. external window closed); recreate on next play.
            log.debug("PlayerManager::stop player appears to be shut down", exc_info=True)
            self._player_shutdown = True
        self.timeline_handle()

        if not playend:
            self.exec_stop_cmd()

    def _send_terminal_stop(self):
        """
        Hand the final playback position to the timeline manager for an
        authoritative state=stopped. Without it the server extrapolates the
        playhead and the session never clears (the shim appears wedged).
        """
        media_item = self._media_item
        if not media_item:
            return

        # The core may be gone (window closed), so reading playback_time can
        # raise ShutdownError. Fall back to the known duration.
        position_ms = None
        try:
            position = self._player.playback_time
            if position is not None:
                position_ms = int(position * 1e3)
        except Exception:
            pass
        if position_ms is None:
            duration = media_item.get_attr("duration")
            position_ms = int(duration) if duration else 0

        # Imported lazily to avoid a circular import (timeline imports player).
        from .timeline import timelineManager
        timelineManager.notify_stopped(media_item, position_ms)

    @synchronous('_lock')
    def get_volume(self, percent=False):
        if self._player:
            if not percent:
                return self._player.volume / 100
            return self._player.volume

    @synchronous('_lock')
    def toggle_pause(self):
        if not self._player.playback_abort:
            self._player.pause = not self._player.pause
        self.timeline_handle()

    @synchronous('_lock')
    def seek(self, offset):
        """
        Seek to ``offset`` seconds
        """
        if not self._player.playback_abort:
            if self.is_in_intro and offset > self._player.playback_time:
                self.skip_intro()
            elif self.is_in_credits and offset > self._player.playback_time:
                self.skip_credits()
            else:
                self._player.playback_time = offset
        self.timeline_handle()

    @synchronous('_lock')
    def set_volume(self, pct):
        if not self._player.playback_abort:
            self._player.volume = pct
        self.timeline_handle()

    @synchronous('_lock')
    def get_state(self):
        if self._player.playback_abort:
            return "stopped"

        if self._player.pause:
            return "paused"

        return "playing"

    @synchronous('_lock')
    def is_paused(self):
        if not self._player.playback_abort:
            return self._player.pause
        return False

    @synchronous('_lock')
    def finished_callback(self, has_lock):
        if not self._media_item:
            return
       
        self._media_item.set_played()

        if self._media_item.is_multipart():
            if has_lock:
                log.debug("PlayerManager::finished_callback media is multi-part, checking for next part")
                # Try to select the next part
                next_part = self.__part+1
                if self._media_item.select_part(next_part):
                    self.__part = next_part
                    log.debug("PlayerManager::finished_callback starting next part")
                    self.play(self._media_item)
            else:
                log.debug("PlayerManager::finished_callback No lock, skipping...")
        
        elif self._media_item.parent.has_next and settings.auto_play:
            if has_lock:
                log.debug("PlayerManager::finished_callback starting next episode")
                self.play(self._media_item.parent.get_next().get_media_item(0))
            else:
                log.debug("PlayerManager::finished_callback No lock, skipping...")

        else:
            if settings.media_ended_cmd:
                os.system(settings.media_ended_cmd)
            log.debug("PlayerManager::finished_callback reached end")
            self.stop(playend=True)

    @synchronous('_lock')
    def watched_skip(self):
        if not self._media_item:
            return

        self._media_item.set_played()
        self.play_next()

    @synchronous('_lock')
    def unwatched_quit(self):
        if not self._media_item:
            return

        self._media_item.set_played(False)
        self.stop()

    @synchronous('_lock')
    def play_next(self):
        if self._media_item.parent.has_next:
            self.play(self._media_item.parent.get_next().get_media_item(0))
            return True
        return False

    @synchronous('_lock')
    def skip_to(self, key):
        media = self._media_item.parent.get_from_key(key)
        if media:
            self.play(media.get_media_item(0))
            return True
        return False

    @synchronous('_lock')
    def play_prev(self):
        if self._media_item.parent.has_prev:
            self.play(self._media_item.parent.get_prev().get_media_item(0))
            return True
        return False

    @synchronous('_lock')
    def restart_playback(self):
        current_time = self._player.playback_time
        self.play(self._media_item, current_time)
        return True

    @synchronous('_lock')
    def get_media_item_attr(self, attr, default=None):
        if self._media_item:
            return self._media_item.get_media_item_attr(attr, default)
        return default

    @synchronous('_lock')
    def set_streams(self, audio_uid, sub_uid):
        if not self._media_item.is_transcode:
            if audio_uid is not None:
                log.debug("PlayerManager::play selecting audio stream index=%s" % audio_uid)
                self._player.audio = self._media_item.audio_seq[audio_uid]

            if sub_uid == '0':
                log.debug("PlayerManager::play selecting subtitle stream (none)")
                self._player.sub = 'no'
            elif sub_uid is not None:
                log.debug("PlayerManager::play selecting subtitle stream index=%s" % sub_uid)
                if sub_uid in self._media_item.subtitle_seq:
                    self._player.sub = self._media_item.subtitle_seq[sub_uid]
                else:
                    log.debug("PlayerManager::play selecting external subtitle id=%s" % sub_uid)
                    self.load_external_sub(sub_uid)

        self._media_item.set_streams(audio_uid, sub_uid)

        if self._media_item.is_transcode:
            self.restart_playback()
        self.timeline_handle()
    
    @synchronous('_lock')
    def load_external_sub(self, sub_id):
        if sub_id in self.external_subtitles:
            self._player.sub = self.external_subtitles[sub_id]
        else:
            try:
                sub_url = self._media_item.get_external_sub(sub_id)
                if settings.log_decisions:
                    log.debug("Load External Subtitle: {0}".format(sub_url))
                self._player.sub_add(sub_url)
                self.external_subtitles[sub_id] = self._player.sub
                self.external_subtitles_rev[self._player.sub] = sub_id
            except SystemError:
                log.debug("PlayerManager::could not load external subtitle")

    def get_track_ids(self):
        if self._media_item.is_transcode:
            return self._media_item.get_transcode_streams()
        else:
            aid, sid = None, None
            if self._player.sub and self._player.sub != 'no':
                if self._player.sub in self.external_subtitles_rev:
                    sid = self.external_subtitles_rev.get(self._player.sub, '')
                else:
                    sid = self._media_item.subtitle_uid.get(self._player.sub, '')

            if self._player.audio != 'no':
                aid = self._media_item.audio_uid.get(self._player.audio, '')
            return aid, sid

    def update_subtitle_visuals(self, restart_transcode=True):
        if self._media_item.is_transcode:
            if restart_transcode:
                self.restart_playback()
        else:
            self._player.sub_pos = SUBTITLE_POS[settings.subtitle_position]
            self._player.sub_scale = settings.subtitle_size / 100
            self._player.sub_color = settings.subtitle_color
        self.timeline_handle()

    def upd_player_hide(self):
        self._player.keep_open = self._media_item.parent.has_next
    
    def terminate(self):
        self.stop()
        # Invalidate the generation so the quit our terminate() triggers is
        # treated as stale, not as the user closing the window (which would
        # queue a needless restart during shutdown).
        self._mpv_generation += 1
        if is_using_ext_mpv and not self._player_shutdown:
            try:
                self._player.terminate()
            except Exception:
                log.debug("PlayerManager::terminate player already shut down", exc_info=True)

playerManager = PlayerManager()
