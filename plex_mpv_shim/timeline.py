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
import urllib.parse

from .utils import Timer, safe_urlopen, mpv_color_to_plex, get_plex_url, get_session

log = logging.getLogger("timeline")

# While idle we push a bare navigation/stopped frame to the proxy channel this
# often. Without it the server ages out our player selection and the next
# playMedia stalls until the controller re-casts. Comfortably under the ~90s
# subscriber TTL, and cheap enough to run indefinitely (matches the HTPC, which
# keeps sending idle timelines).
PROXY_IDLE_KEEPALIVE = 10

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
        # True once a "stopped" frame has gone to the proxy channel for the
        # current stop; suppresses the repeats (matches the HTPC, which sends
        # one then goes quiet). Reset when playback resumes.
        self._proxy_stopped_sent = False

        # Terminal-stop tracking. Playback ending clears the active media item,
        # so stash the final item/position and re-announce state=stopped for a
        # few cycles -- otherwise the server extrapolates the playhead and the
        # session never terminates.
        self.terminal_stop_item  = None
        self.terminal_stop_time  = 0
        self.terminal_stop_count = 0

        # Paces the idle keepalive push to the proxy channel (see run()).
        self.proxy_idle_timer    = Timer()

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
                    self.proxy_idle_timer.restart()
                elif self.terminal_stop_count > 0:
                    # Re-announce the stopped state so a dropped packet can't
                    # leave the server session wedged (playhead ticking past EOF).
                    self.SendTimelineToSubscribers()
                    self.terminal_stop_count -= 1
                    self.proxy_idle_timer.restart()
                elif playerManager._media_item is None:
                    # Fully idle between sessions: nudge the proxy channel with a
                    # bare stopped frame so the server keeps our player selected.
                    # Without this the run loop goes silent after the terminal
                    # stop and the next playMedia stalls until the user re-casts.
                    if self.proxy_idle_timer.elapsed() > PROXY_IDLE_KEEPALIVE:
                        self.proxy_idle_timer.restart()
                        self.sender_pool.apply_async(self.SendTimelineToProxy,
                                                     (self.GetCurrentTimeline(), True))
                if self.idleTimer.elapsed() > settings.idle_cmd_delay and not self.is_idle:
                    if settings.idle_when_paused and settings.stop_idle and playerManager._media_item:
                        playerManager.stop()
                    if settings.idle_cmd:
                        os.system(settings.idle_cmd)
                    self.is_idle = True
            except Exception:
                # Keep the thread alive through transient errors (e.g. the mpv
                # core being torn down), or timeline updates -- including the
                # final "stopped" -- would stop forever.
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
        # Pass whether we acquired the lock so the worker only releases what it
        # owns -- a "stopped" timeline sent while a "playing" send is in flight
        # would otherwise double-release (RuntimeError).
        acquired = self.sending_to_ps.acquire(False)
        if acquired or timeline["state"] == "stopped":
            self.sender_pool.apply_async(self.SendTimelineToPlexServer, (timeline, acquired))

        # Provider-playback: push the timeline to the server so the Plex Web
        # in-page player (which doesn't use the legacy poll reply) updates.
        self.sender_pool.apply_async(self.SendTimelineToProxy, (timeline,))

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

    def _appendTimelines(self, mediaContainer, tlines):
        # Companion controllers expect a Timeline for *each* media type (video,
        # music, photo) in every payload, not just the active one. We only drive
        # "video"; the rest are reported stopped. location lives on the
        # MediaContainer, so don't repeat it onto the active Timeline.
        #
        # When stopped, the frame must be EMPTY -- a bare <Timeline type=...
        # state='stopped' controllable=''/> for every type, as the HTPC sends
        # when idle. Carrying the last item's details makes Plex Web think the
        # player still has it loaded: it snaps the resume dialog to 0 and forces
        # an extra stop before a new play starts. The legacy /:/timeline POST
        # (SendTimelineToPlexServer) is separate and still carries the position.
        active_type = tlines.get("type", "video")
        active_stopped = tlines.get("state") == "stopped"
        for media_type in ("video", "music", "photo"):
            lineEl = et.Element("Timeline")
            if media_type == active_type and not active_stopped:
                for key, value in list(tlines.items()):
                    if key == "location":
                        continue
                    lineEl.set(key, str(value))
            else:
                lineEl.set("type", media_type)
                lineEl.set("state", "stopped")
                lineEl.set("controllable", "")
            mediaContainer.append(lineEl)

    def GetCurrentTimeLinesXML(self, subscriber, tlines=None):
        if tlines is None:
            tlines = self.GetCurrentTimeline()

        mediaContainer = et.Element("MediaContainer")
        if subscriber.commandID is not None:
            mediaContainer.set("commandID", str(subscriber.commandID))
        mediaContainer.set("location", tlines["location"])

        self._appendTimelines(mediaContainer, tlines)

        return mediaContainer

    def SendTimelineToProxy(self, timeline, keepalive=False):
        """
        Push the timeline to /player/proxy/timeline (provider-playback). This
        is the feed the Plex Web in-page player's scrubber uses; the legacy poll
        reply isn't enough for it.

        keepalive marks the slow idle push from run(): it must bypass the
        stopped-frame suppression below (that's there to collapse the terminal
        stop burst, not to silence the keepalive).
        """
        # The server rejects this push (HTTP 400) without an open notifications
        # WebSocket; skip until it's up rather than firing doomed requests.
        from .proxy import notificationListener, proxyClient
        if not notificationListener.connected:
            return

        media_item = playerManager._media_item
        server_url = None
        if media_item:
            server_url = media_item.parent.server_url
        elif self.last_server_url:
            server_url = self.last_server_url
        if not server_url:
            return

        # After a stop, push one "stopped" frame then stay quiet (matches the
        # HTPC). The terminal-stop defense re-announces "stopped" several times;
        # repeating it on the proxy channel floods the controller during the
        # stop->play transition, dismissing the resume dialog and cutting a
        # freshly started stream. Only the proxy push is gated; legacy is not.
        if not keepalive:
            if timeline.get("state", "stopped") == "stopped":
                if self._proxy_stopped_sent:
                    return
                self._proxy_stopped_sent = True
            else:
                self._proxy_stopped_sent = False

        mediaContainer = et.Element("MediaContainer")
        # The HTPC reports location 'navigation' on every provider-playback
        # frame. (The legacy paths keep their own handling, where an empty value
        # guards against a nav-menu popup on legacy controllers.)
        mediaContainer.set("location", "navigation")
        self._appendTimelines(mediaContainer, timeline)

        tree = et.ElementTree(mediaContainer)
        tmp  = BytesIO()
        tree.write(tmp, encoding="utf-8", xml_declaration=True)
        tmp.seek(0)
        body = tmp.read()

        # Echo the proxy channel's latest commandID so the overlay knows the
        # player applied its command (e.g. a seek); if it lags, the scrubber
        # freezes. This is the proxy poll's counter, not the legacy subscriber's.
        command_id = proxyClient.last_command_id

        data = {
            "commandID": str(command_id),
            "deviceClass": "pc",
            "protocolCapabilities": "timeline,playback,navigation,playqueues,provider-playback",
            "protocolVersion": "2",
            "X-Plex-Session-Id": get_session(urllib.parse.urlsplit(server_url).hostname),
        }
        if playerManager.playback_session_id:
            data["X-Plex-Playback-Session-Id"] = playerManager.playback_session_id
        if playerManager.playback_id:
            data["X-Plex-Playback-Id"] = playerManager.playback_id

        url = get_plex_url("%s/player/proxy/timeline" % server_url, data, quiet=True)
        try:
            resp = requests.post(url, data=body, headers={
                "Content-Type":             "application/xml",
                "X-Plex-Client-Identifier": settings.client_uuid,
            }, timeout=5)
            if resp.status_code == 200:
                log.debug("TimelineManager::SendTimelineToProxy %s/player/proxy/timeline -> HTTP 200",
                          server_url)
            else:
                log.warning("TimelineManager::SendTimelineToProxy rejected (HTTP %s): %s",
                            resp.status_code, resp.text[:200])
        except Exception:
            log.warning("TimelineManager::SendTimelineToProxy error pushing proxy timeline", exc_info=True)

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

        # Only read the mpv core while a media item is active: reading it when
        # idle or after the window was closed can raise (ShutdownError / IPC
        # error). Reading only here also keeps a transient read error during
        # playback from being mistaken for a stop and dropping the session.
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

            # "navigation" (not "fullScreenVideo") while casting; the web
            # player's overlay only tracks the timeline when it sees this.
            options["location"]          = "navigation"

            options["time"]              = int(playback_time * 1e3)
            # Marks this as a normal library playback session for the web player.
            options["providerIdentifier"] = "com.plexapp.plugins.library"
            # repeat is player-side; shuffle is overridden by get_queue_info below.
            options["repeat"]            = str(playerManager.repeat)
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
                # Terminal stop: send the final position so the server
                # terminates the session instead of extrapolating the playhead.
                options["state"] = "stopped"
                options["time"]  = int(self.terminal_stop_time)
            elif playerManager._media_item is not None:
                # Loaded but no playback position yet -- still buffering.
                options["state"] = "buffering"
            else:
                options["state"] = "stopped"

        return options


timelineManager = TimelineManager()
