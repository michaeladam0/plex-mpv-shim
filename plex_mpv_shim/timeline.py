import logging
import requests
import threading
import time
import os
from threading import Lock

try:
    from xml.etree import cElementTree as et
except:
    from xml.etree import ElementTree as et

from io import BytesIO
from multiprocessing.dummy import Pool

from .conf import settings
from .media import MediaType
from .player import playerManager
from .subscribers import remoteSubscriberManager
from .utils import Timer, safe_urlopen, mpv_color_to_plex

log = logging.getLogger("timeline")

class TimelineManager(threading.Thread):
    def __init__(self):
        self.currentItems    = {}
        self.currentStates   = {}
        self.idleTimer       = Timer()
        self.subTimer        = Timer()
        self.serverTimer     = Timer()
        self.stopped         = False
        self.halt            = False
        self.trigger         = threading.Event()
        self.is_idle         = True
        self.last_media_item = None
        self.sender_pool     = Pool(5)
        self.sending_to_ps   = Lock()
        self.last_server_url = None

        # End-of-playback terminal stop tracking. When playback ends the
        # active media item is cleared, so we stash the final item/position
        # here and keep re-announcing a state=stopped timeline for a few
        # cycles. Without this the Plex server keeps extrapolating the
        # playhead and the session never terminates.
        self.terminal_stop_item  = None
        self.terminal_stop_time  = 0
        self.terminal_stop_count = 0

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.sender_pool.close()
        self.join()

    def run(self):
        force_next = False
        while not self.halt:
            try:
                if (playerManager._player and playerManager._media_item and (not settings.idle_when_paused
                    or not playerManager.is_paused())) or force_next:
                    if force_next or not playerManager.is_paused():
                        self.SendTimelineToSubscribers()
                    self.delay_idle()
                elif self.terminal_stop_count > 0:
                    # Playback has ended: re-announce the stopped state for a few
                    # cycles so a single dropped/reordered packet doesn't leave
                    # the Plex server session wedged (playhead ticking past EOF).
                    self.SendTimelineToSubscribers()
                    self.terminal_stop_count -= 1
                if self.idleTimer.elapsed() > settings.idle_cmd_delay and not self.is_idle:
                    if settings.idle_when_paused and settings.stop_idle and playerManager._media_item:
                        playerManager.stop()
                    if settings.idle_cmd:
                        os.system(settings.idle_cmd)
                    self.is_idle = True
            except Exception:
                # Never let a transient error (e.g. the mpv core being torn
                # down) permanently kill this thread, or all timeline updates
                # (including the final "stopped") would stop forever.
                log.warning("TimelineManager::run error while updating timeline", exc_info=True)
            force_next = False
            if self.trigger.wait(1):
                force_next = True
                self.trigger.clear()

    def delay_idle(self):
        self.idleTimer.restart()
        self.is_idle = False

    def notify_stopped(self, media_item, position_ms):
        """
        Record the final position of a finished item and schedule several
        authoritative state=stopped timelines to the Plex server. Called
        from PlayerManager.stop() *before* the media item is cleared.
        """
        self.terminal_stop_item  = media_item
        self.terminal_stop_time  = position_ms
        self.terminal_stop_count = 5
        self.trigger.set()

    def SendTimelineToSubscribers(self):
        timeline = self.GetCurrentTimeline()

        # The sender_pool prevents the timeline from freezing
        # if a client times out or takes a while to respond.

        log.debug("TimelineManager::SendTimelineToSubscribers updating all subscribers")
        for sub in list(remoteSubscriberManager.subscribers.values()):
            self.sender_pool.apply_async(self.SendTimelineToSubscriber, (sub, timeline))
        
        # Also send timeline to plex server.
        # Do not send the timeline if the last one if still sending.
        # (Plex servers can get overloaded... We don't want the UI to freeze.)
        # Note that we send anyway if the state is stopped. We don't want that to get lost.
        # We pass whether we actually acquired the lock so the worker only
        # releases what it owns -- otherwise a "stopped" timeline dispatched
        # while a "playing" send is still in flight causes a double-release
        # (RuntimeError) and corrupts the mutual exclusion.
        acquired = self.sending_to_ps.acquire(False)
        if acquired or timeline["state"] == "stopped":
            self.sender_pool.apply_async(self.SendTimelineToPlexServer, (timeline, acquired))

    def SendTimelineToPlexServer(self, timeline, acquired=True):
        try:
            media_item  = playerManager._media_item
            server_url = None
            if media_item:
                server_url = media_item.parent.server_url
                self.last_server_url = media_item.parent.server_url
            elif self.last_server_url:
                server_url = self.last_server_url
            if server_url:
                safe_urlopen("%s/:/timeline" % server_url, timeline, quiet=True)
        finally:
            if acquired:
                self.sending_to_ps.release()

    def SendTimelineToSubscriber(self, subscriber, timeline=None):
        subscriber.set_poll_evt()
        if subscriber.url == "":
            return True

        timelineXML = self.GetCurrentTimeLinesXML(subscriber, timeline)
        url = "%s/:/timeline" % subscriber.url

        log.debug("TimelineManager::SendTimelineToSubscriber sending timeline to %s" % url)

        tree = et.ElementTree(timelineXML)
        tmp  = BytesIO()
        tree.write(tmp, encoding="utf-8", xml_declaration=True)

        tmp.seek(0)
        xmlData = tmp.read()

        # TODO: Abstract this into a utility function and add other X-Plex-XXX fields
        try:
            requests.post(url, data=xmlData, headers={
                "Content-Type":             "application/x-www-form-urlencoded",
                "Connection":               "keep-alive",
                "Content-Range":            "bytes 0-/-1",
                "X-Plex-Client-Identifier": settings.client_uuid
            }, timeout=5)
            return True
        except requests.exceptions.ConnectTimeout:
            log.warning("TimelineManager::SendTimelineToSubscriber timeout sending to %s" % url)
            return False
        except Exception:
            log.warning("TimelineManager::SendTimelineToSubscriber error sending to %s" % url)
            return False

    def WaitForTimeline(self, subscriber):
        subscriber.get_poll_evt().wait(30)
        return self.GetCurrentTimeLinesXML(subscriber)

    def GetCurrentTimeLinesXML(self, subscriber, tlines=None):
        if tlines is None:
            tlines = self.GetCurrentTimeline()

        #
        # Only "video" is supported right now
        #
        mediaContainer = et.Element("MediaContainer")
        if subscriber.commandID is not None:
            mediaContainer.set("commandID", str(subscriber.commandID))
        mediaContainer.set("location", tlines["location"])

        lineEl = et.Element("Timeline")
        for key, value in list(tlines.items()):
            lineEl.set(key, str(value))
        mediaContainer.append(lineEl)

        return mediaContainer

    def GetCurrentTimeline(self):
        # https://github.com/plexinc/plex-home-theater-public/blob/pht-frodo/plex/Client/PlexTimelineManager.cpp#L142
        # Note: location is set to "" to avoid pop-up of navigation menu. This may be abuse of the API.
        options = {
            "location": "",
            "state":    "stopped",
            "type":     "video"
        }
        controllable = []

        media_item  = playerManager._media_item
        player = playerManager._player

        # Only touch the mpv core while a media item is active. Reading core
        # properties when nothing is playing -- or after the user closed the
        # window and the core was torn down -- can raise (ShutdownError on the
        # internal backend, an IPC error on the external one). Equally
        # important: a *transient* read error during active playback must not
        # be mistaken for a stop, or we would spuriously report "stopped" and
        # drop the session. So we read the core only here, and the idle/stopped
        # branch below never touches it.
        playback_time = None
        is_playing = False
        if media_item:
            playback_time = player.playback_time
            is_playing = (not player.playback_abort) and bool(playback_time)

        # The playback_time value can take on the value of none, probably
        # when playback is complete. This avoids the thread crashing.
        if is_playing:
            options["state"]     = playerManager.get_state()
            self.last_media_item = media_item
            # Real playback is active again; drop any pending terminal stop.
            self.terminal_stop_item = None
            self.terminal_stop_count = 0
            media = media_item.parent

            if media_item.media_type == MediaType.VIDEO:
                options["type"]          = "video"
            elif media_item.media_type == MediaType.MUSIC:
                options["type"]          = "video"

            # Real Plex players report the container location as "navigation"
            # while casting (not "fullScreenVideo"); the web player's overlay
            # only tracks the timeline when it sees this value.
            options["location"]          = "navigation"

            options["time"]              = int(playback_time * 1e3)
            # Reported by real players; included so the web player treats this
            # as a normal library playback session.
            options["providerIdentifier"] = "com.plexapp.plugins.library"
            options["repeat"]            = "0"
            options["shuffle"]           = "0"
            
            aid, sid = playerManager.get_track_ids()

            vid = media_item.get_video_stream_id()
            if vid:
                options["videoStreamID"] = vid

            if aid:
                options["audioStreamID"] = aid
            if sid:
                options["subtitleStreamID"] = sid
                options["subtitleSize"] = settings.subtitle_size
                controllable.append("subtitleSize")
                
                if not media_item.is_transcode:
                    options["subtitlePosition"] = settings.subtitle_position
                    options["subtitleColor"] = mpv_color_to_plex(settings.subtitle_color)
                    controllable.append("subtitlePosition")
                    controllable.append("subtitleColor")

            options["ratingKey"]         = media_item.get_attr("ratingKey")
            options["key"]               = media_item.get_attr("key")
            options["containerKey"]      = media_item.get_attr("key")
            options["guid"]              = media_item.get_attr("guid")
            options["duration"]          = media_item.get_attr("duration", "0")
            options["address"]           = media.path.hostname
            options["protocol"]          = media.path.scheme
            options["port"]              = media.path.port
            options["machineIdentifier"] = media.get_machine_identifier()

            if media.play_queue:
                options.update(media.get_queue_info())

            controllable.append("playPause")
            controllable.append("stop")
            controllable.append("stepBack")
            controllable.append("stepForward")
            controllable.append("seekTo")
            controllable.append("skipTo")

            controllable.append("subtitleStream")
            controllable.append("audioStream")
            # Advertised by real players.
            controllable.append("videoStream")
            controllable.append("shuffle")
            controllable.append("repeat")

            if media_item.parent.has_next:
                controllable.append("skipNext")
            
            if media_item.parent.has_prev:
                controllable.append("skipPrevious")

            # If the duration is unknown, disable seeking
            if options["duration"] == "0":
                options.pop("duration")
                controllable.remove("seekTo")

            controllable.append("volume")
            options["volume"] = str(playerManager.get_volume(percent=True) or 0)

            options["controllable"] = ",".join(controllable)
        else:
            media_item = self.terminal_stop_item or self.last_media_item
            if media_item:
                options["ratingKey"]         = media_item.get_attr("ratingKey")
                options["key"]               = media_item.get_attr("key")
                options["containerKey"]      = media_item.get_attr("key")
                duration = media_item.get_attr("duration")
                if duration:
                    options["duration"]      = duration
                if media_item.parent.play_queue:
                    options.update(media_item.parent.get_queue_info())
            if self.terminal_stop_item is not None:
                # Authoritative end-of-playback stop: include the final
                # position so the server terminates the session instead of
                # extrapolating the playhead from wall-clock time.
                options["state"] = "stopped"
                options["time"]  = int(self.terminal_stop_time)
            elif playerManager._media_item is not None:
                # A media item is loaded but not yet producing a playback
                # position -- still buffering. Don't touch the core to decide.
                options["state"] = "buffering"
            else:
                options["state"] = "stopped"

        return options


timelineManager = TimelineManager()
