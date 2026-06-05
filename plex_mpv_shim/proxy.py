"""
Plex Companion "provider-playback" client.

Modern Plex controllers (e.g. the Plex Web in-page player) don't pull the
timeline from us via the legacy /player/timeline/poll proxy -- they expect the
*player* to drive the conversation against the server:

  * long-poll  GET  {server}/player/proxy/poll      to receive <Command>s, and
  * POST            {server}/player/proxy/timeline   to push our timeline.

This module owns the command (poll) side; the timeline push itself lives in
timeline.py (SendTimelineToProxy). Registration/presence is handled by
NotificationListener (below), which holds a /:/websockets/notifications
WebSocket open -- the server rejects the proxy timeline push (HTTP 400) unless
that connection is held.

The server URL + token are learned from playMedia. The poll loop keeps running
across stops (see ProxyClient.run) to match the real Plex HTPC.
"""
import logging
import os
import threading
import time
import urllib.parse
from collections import deque

try:
    from xml.etree import cElementTree as et
except:
    from xml.etree import ElementTree as et

import certifi
import requests

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

from .conf import settings
from .media import Media, MediaType
from .player import playerManager
from .timeline import timelineManager
from .utils import get_plex_url, upd_token, sanitize_msg, plex_color_to_mpv, get_session, plex_eph_tokens

log = logging.getLogger("proxy")

NAVIGATION_DICT = {
    "/player/navigation/moveDown": "down",
    "/player/navigation/moveUp": "up",
    "/player/navigation/select": "ok",
    "/player/navigation/moveLeft": "left",
    "/player/navigation/moveRight": "right",
    "/player/navigation/home": "home",
    "/player/navigation/back": "back",
}


def _play_media(arguments):
    # Mirrors HttpHandler.playMedia; duplicated so the legacy HTTP path is untouched.
    address   = arguments.get("address", None)
    protocol  = arguments.get("protocol", "http")
    port      = arguments.get("port", "32400")
    key       = arguments.get("key", None)
    offset    = int(int(arguments.get("offset", 0)) / 1e3)
    url       = urllib.parse.urljoin("%s://%s:%s" % (protocol, address, port), key)
    playQueue = arguments.get("containerKey", None)
    mediaType = arguments.get("type", "video")

    parsed_media_type = MediaType.MUSIC if mediaType == "music" else MediaType.VIDEO

    token = arguments.get("token", None)
    if token:
        upd_token(address, token)

    if settings.enable_play_queue and playQueue and playQueue.startswith("/playQueue"):
        media = Media(url, media_type=parsed_media_type, play_queue=playQueue)
    else:
        media = Media(url, media_type=parsed_media_type)

    media_item = media.get_media_item(0)
    if media_item:
        if settings.pre_media_cmd:
            os.system(settings.pre_media_cmd)
        playerManager.play(media_item, offset)
        timelineManager.SendTimelineToSubscribers()


def _set_parameters(arguments):
    # Mirrors HttpHandler.set.
    if "volume" in arguments:
        playerManager.set_volume(int(arguments["volume"]))
    if "autoPlay" in arguments:
        settings.auto_play = arguments["autoPlay"] == "1"
        settings.save()
    subtitle_settings_upd = False
    if "subtitleSize" in arguments:
        subtitle_settings_upd = True
        settings.subtitle_size = int(arguments["subtitleSize"])
    if "subtitlePosition" in arguments:
        subtitle_settings_upd = True
        settings.subtitle_position = arguments["subtitlePosition"]
    if "subtitleColor" in arguments:
        subtitle_settings_upd = True
        settings.subtitle_color = plex_color_to_mpv(arguments["subtitleColor"])
    if subtitle_settings_upd:
        settings.save()
        playerManager.update_subtitle_visuals()


def process_command(path, arguments):
    """Execute a Companion command received over the proxy poll channel."""
    if path in ("/player/playback/playMedia", "/player/application/playMedia"):
        _play_media(arguments)
    elif path == "/player/playback/stop":
        playerManager.stop()
        timelineManager.SendTimelineToSubscribers()
    elif path in ("/player/playback/pause", "/player/playback/play"):
        playerManager.toggle_pause()
        timelineManager.SendTimelineToSubscribers()
    elif path == "/player/playback/skipNext":
        playerManager.play_next()
    elif path == "/player/playback/skipPrevious":
        playerManager.play_prev()
    elif path == "/player/playback/seekTo":
        playerManager.seek(int(int(arguments.get("offset", 0)) * 1e-3))
    elif path == "/player/playback/skipTo":
        playerManager.skip_to(arguments["key"])
    elif path == "/player/playback/setStreams":
        playerManager.set_streams(arguments.get("audioStreamID"),
                                  arguments.get("subtitleStreamID"))
    elif path == "/player/playback/setParameters":
        _set_parameters(arguments)
    elif path == "/player/playback/refreshPlayQueue":
        if playerManager._media_item:
            playerManager._media_item.parent.upd_play_queue()
            playerManager.upd_player_hide()
            timelineManager.SendTimelineToSubscribers()
    elif path == "/player/mirror/details":
        timelineManager.delay_idle()
    elif path.startswith("/player/navigation"):
        if path in NAVIGATION_DICT:
            playerManager.menu.menu_action(NAVIGATION_DICT[path])
    else:
        log.debug("ProxyClient::process_command unhandled command %s", path)


class ProxyClient(threading.Thread):
    def __init__(self):
        self.halt = False
        # Recently processed command IDs, to drop duplicates the server repeats.
        self.processed_ids = deque(maxlen=200)
        # Highest commandID seen on the proxy poll channel. The timeline push
        # echoes it back so the Plex Web overlay knows the player applied the
        # command (e.g. a seek); otherwise its scrubber freezes. Distinct from
        # the legacy /player/timeline/poll subscriber commandID, and advances
        # continuously since we never restart the poll loop.
        self.last_command_id = 0
        super().__init__(name="Proxy Poll")
        self.daemon = True

    def stop(self):
        self.halt = True

    def run(self):
        while not self.halt:
            try:
                # Poll continuously across stops, like the real HTPC: if we stop
                # polling when idle the server ends the proxy session and routes
                # the next playMedia over legacy HTTP, breaking the resume dialog
                # and start flow.
                server_url, token = _provider_target()
                if server_url and token:
                    start = time.time()
                    self._poll(server_url)
                    # Guard against a server that returns instantly instead of
                    # holding the long-poll, so we don't busy-loop.
                    elapsed = time.time() - start
                    if elapsed < 0.5:
                        time.sleep(0.5 - elapsed)
                    continue
            except Exception:
                log.warning("ProxyClient::run error", exc_info=True)
            # Nothing cast yet: idle.
            time.sleep(1)

    def _poll(self, server_url):
        domain = urllib.parse.urlsplit(server_url).hostname
        data = {
            "timeout": "1",  # long-poll duration, as the HTPC sends
            "deviceClass": "pc",
            "protocolCapabilities": "timeline,playback,navigation,playqueues,provider-playback",
            "protocolVersion": "2",
            "X-Plex-Session-Id": get_session(domain),
        }
        # Only attach per-playback IDs while playing. When idle, the previous
        # session's IDs would tie this poll to a finished session and stop the
        # server routing the next playMedia to us.
        if playerManager._media_item is not None:
            if playerManager.playback_session_id:
                data["X-Plex-Playback-Session-Id"] = playerManager.playback_session_id
            if playerManager.playback_id:
                data["X-Plex-Playback-Id"] = playerManager.playback_id

        url = get_plex_url("%s/player/proxy/poll" % server_url, data, quiet=True)

        try:
            resp = requests.get(url, timeout=10, headers={
                "Accept":                   "application/xml",
                "X-Plex-Client-Identifier": settings.client_uuid,
            })
        except requests.exceptions.Timeout:
            # Expected: the long-poll is held open until our read timeout when
            # there's no command. Just poll again.
            log.debug("ProxyClient::_poll long-poll timed out; re-polling")
            return
        except Exception:
            log.warning("ProxyClient::_poll error polling %s/player/proxy/poll", server_url, exc_info=True)
            time.sleep(1)
            return

        if resp.status_code != 200:
            log.warning("ProxyClient::_poll %s/player/proxy/poll -> HTTP %s: %s",
                        server_url, resp.status_code, resp.text[:500])
            # Back off so we don't hammer the server on persistent errors.
            time.sleep(2)
            return

        if not resp.content:
            return

        log.debug("ProxyClient::_poll response: %s", resp.text[:1000])
        try:
            root = et.fromstring(resp.content)
        except Exception:
            log.warning("ProxyClient::_poll could not parse poll response: %s", resp.text[:500])
            return

        for cmd in root.findall("./Command"):
            self._dispatch(cmd, server_url)

    def _send_response(self, server_url, command_id):
        """
        Ack a command via POST /player/proxy/response?commandID=N, as the HTPC
        does. Without it the server treats the command stream as unconfirmed:
        the controller can eat the next click or hang on a spinner.
        """
        data = {
            "commandID":         str(command_id),
            "X-Plex-Session-Id": get_session(urllib.parse.urlsplit(server_url).hostname),
        }
        if playerManager.playback_session_id:
            data["X-Plex-Playback-Session-Id"] = playerManager.playback_session_id
        if playerManager.playback_id:
            data["X-Plex-Playback-Id"] = playerManager.playback_id

        url = get_plex_url("%s/player/proxy/response" % server_url, data, quiet=True)
        try:
            resp = requests.post(url, data=b'<Response code="200" status="OK" />',
                                 timeout=5, headers={
                                     "Content-Type":             "application/xml",
                                     "X-Plex-Client-Identifier": settings.client_uuid,
                                 })
            if resp.status_code != 200:
                log.debug("ProxyClient::_send_response commandID=%s -> HTTP %s",
                          command_id, resp.status_code)
        except Exception:
            log.debug("ProxyClient::_send_response error acking commandID=%s",
                      command_id, exc_info=True)

    def _dispatch(self, cmd, server_url):
        command_id = cmd.get("commandID")
        if command_id is not None:
            # Ack first, before doing anything else (mirrors the HTPC).
            self._send_response(server_url, command_id)
            # Track the latest commandID for the timeline push to echo back.
            try:
                if int(command_id) > self.last_command_id:
                    self.last_command_id = int(command_id)
            except (TypeError, ValueError):
                pass
            if command_id in self.processed_ids:
                return
            self.processed_ids.append(command_id)

        raw_path = cmd.get("path") or ""
        parsed = urllib.parse.urlparse(raw_path)
        arguments = dict(urllib.parse.parse_qsl(parsed.query))
        # The command's original query params arrive as attributes prefixed with
        # "query" and CamelCased (offset -> queryOffset). Un-prefix them to the
        # names the handlers expect; without this seekTo has no "offset" and
        # seeks to 0. setdefault so an attribute never clobbers the path query.
        for key, value in cmd.attrib.items():
            if key == "path":
                continue
            if key.startswith("query") and len(key) > 5:
                unprefixed = key[5:]
                arguments.setdefault(unprefixed[0].lower() + unprefixed[1:], value)
            else:
                arguments.setdefault(key, value)

        log.info("ProxyClient command: %s %s", parsed.path, sanitize_msg(urllib.parse.urlencode(arguments)))
        try:
            process_command(parsed.path, arguments)
        except Exception:
            log.warning("ProxyClient::_dispatch error processing %s", parsed.path, exc_info=True)


proxyClient = ProxyClient()


def _provider_target():
    """Return (server_url, token) for the active provider-playback session, or
    (None, None) if we shouldn't be connected right now."""
    server_url = None
    media_item = playerManager._media_item
    if media_item is not None:
        try:
            server_url = media_item.parent.server_url
        except Exception:
            server_url = None
    if not server_url:
        server_url = getattr(timelineManager, "last_server_url", None)
    if not server_url:
        return None, None
    domain = urllib.parse.urlsplit(server_url).hostname
    token = plex_eph_tokens.get(domain)
    if not token:
        return None, None
    return server_url, token


class NotificationListener(threading.Thread):
    """
    Holds the /:/websockets/notifications WebSocket open.

    This is the presence channel for provider-playback: the server rejects
    /player/proxy/timeline (HTTP 400) unless the player holds it open. We don't
    use the notifications -- holding the connection is what registers us -- but
    we drain incoming frames to keep the socket healthy.
    """
    def __init__(self):
        super().__init__(name="WS Notifications")
        self.daemon = True
        self.halt = False
        self._ws = None
        # True while the WebSocket is open; the proxy timeline push checks this
        # so it doesn't fire (and 400) before we're registered.
        self.connected = False

    def stop(self):
        self.halt = True
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def run(self):
        if websocket is None:
            log.warning("NotificationListener: websocket-client is not installed; "
                        "provider-playback timeline updates will be rejected by the "
                        "server. Install it with 'pip install websocket-client'.")
            return

        while not self.halt:
            server_url, token = _provider_target()
            if not server_url:
                time.sleep(2)
                continue
            self._connect_and_hold(server_url, token)
            # Brief pause before reconnect attempts so we don't hammer the server.
            time.sleep(1)

    def _connect_and_hold(self, server_url, token):
        ws_url = "%s/:/websockets/notifications?X-Plex-Token=%s" % (
            server_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1),
            urllib.parse.quote(token),
        )
        try:
            self._ws = websocket.create_connection(
                ws_url,
                timeout=10,
                header=["X-Plex-Client-Identifier: %s" % settings.client_uuid],
                sslopt={"ca_certs": certifi.where()},
            )
        except Exception:
            log.debug("NotificationListener: connect failed for %s", server_url, exc_info=True)
            self._ws = None
            time.sleep(2)
            return

        log.info("NotificationListener: connected to %s", sanitize_msg(server_url))
        self.connected = True
        try:
            self._ws.settimeout(5)
            while not self.halt:
                # Drop the connection if the session ends or the server changes.
                current_url, _ = _provider_target()
                if current_url != server_url:
                    break
                try:
                    self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:
                    break
        finally:
            self.connected = False
            ws = self._ws
            self._ws = None
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            log.info("NotificationListener: disconnected from %s", sanitize_msg(server_url))


notificationListener = NotificationListener()
