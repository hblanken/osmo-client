"""The camera's own video, unchanged, as live HTTP streams (`/live.h264` or `/live.h265`, and `/live.ts`).

The live view arrives as Annex-B access units (H.264 or H.265, one picture each) that the app decodes for
the page. This tap hands the same units, before decoding, to every HTTP reader, so the picture is the
camera's own (its full live view size and bitrate) at no CPU cost:

- `/live.h264` (or `/live.h265`): the elementary stream exactly as it came. This is the source for
  unleash-stream-cli, which copies it without re-encoding.
- `/live.ts`: the same packets muxed into MPEG-TS with PyAV (with timestamps), for players such as VLC,
  ffplay and OBS.
"""

from __future__ import annotations

import queue
import threading
import time
from fractions import Fraction

import av

from camlink.nal import CODEC_NAMES, codec_of
from camlink.util import log

TIME_BASE = Fraction(1, 90000)          # MPEG-TS clock
CONTENT_TYPES = {"es": "application/octet-stream", "ts": "video/mp2t"}


class _Client:
    """One HTTP reader: a bounded queue of (data, codec or None, arrival time); None = new session."""

    def __init__(self) -> None:
        self.queue: queue.Queue[tuple[bytes, str | None, float] | None] = queue.Queue(maxsize=120)
        self.dropped = 0

    def offer(self, item: tuple[bytes, str | None, float] | None) -> None:
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            # A slow reader: drop what it has not read and restart it at the next keyframe, rather than
            # letting it fall behind the live picture. The None tells the writer to wait for a keyframe.
            self.dropped += 1
            try:
                while True:
                    self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(None)


class LiveTap:
    """Fans the active camera's access units out to the `/live.*` readers. feed() runs on the datalink
    thread and never blocks; with no reader it costs one check."""

    def __init__(self) -> None:
        self._clients: set[_Client] = set()
        self._lock = threading.Lock()
        self.codec: str | None = None    # nal.H264 / nal.HEVC of the last keyframe seen
        self.last_keyframe = 0.0         # monotonic()
        self.kbps = 0                    # the camera's video bitrate and frame rate, over the last second
        self.fps = 0
        self._window = (0.0, 0, 0)       # start, bytes, units

    def feed(self, data: bytes, _first_packet: float = 0.0) -> None:
        kind = codec_of(data)            # a keyframe (it starts with parameter sets), and which codec
        now = time.monotonic()
        if kind:
            self.codec, self.last_keyframe = kind, now
        start, size, units = self._window
        if now - start >= 1.0:
            if now - start < 2.0:
                self.kbps, self.fps = round(size * 8 / (now - start) / 1000), round(units / (now - start))
            else:
                self.kbps = self.fps = 0
            self._window = (now, len(data), 1)
        else:
            self._window = (start, size + len(data), units + 1)
        if not self._clients:
            return
        item = (data, kind, time.monotonic())
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.offer(item)

    def reset(self) -> None:
        """A new live view session: readers wait for its first keyframe (their timeline goes on)."""
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.offer(None)

    def open(self) -> _Client:
        client = _Client()
        with self._lock:
            self._clients.add(client)
        return client

    def close(self, client: _Client) -> None:
        with self._lock:
            self._clients.discard(client)

    def snapshot(self) -> dict:
        return {"readers": len(self._clients), "codec": CODEC_NAMES.get(self.codec or ""), "kbps": self.kbps,
                "fps": self.fps}

    def serve(self, out, fmt: str = "es", idle_timeout: float = 30.0,
              stop: threading.Event | None = None) -> None:
        """Write the elementary stream (`fmt` "es") or MPEG-TS ("ts") to the file-like `out` (an HTTP
        response) from the next keyframe on, until the reader goes away, the codec changes, no video comes
        for `idle_timeout` seconds, or `stop` is set."""
        client = self.open()
        container = stream = None
        codec: str | None = None
        start = 0.0
        last_pts = -1
        waiting = True                   # for a keyframe (at the start and after a reset or a drop)
        try:
            while stop is None or not stop.is_set():
                try:
                    item = client.queue.get(timeout=idle_timeout)
                except queue.Empty:
                    return               # no video: the reader reconnects later
                if item is None:
                    waiting = True
                    continue
                data, kind, arrived = item
                if waiting:
                    if not kind:
                        continue
                    waiting = False
                if kind and codec and kind != codec:
                    log(f"/live: the camera now sends {CODEC_NAMES[kind]}, closing the stream for the reader")
                    return
                if codec is None:
                    codec, start = kind, arrived
                if fmt == "es":
                    out.write(data)
                    continue
                if container is None:
                    container = av.open(out, "w", format="mpegts", options={"mpegts_flags": "resend_headers"})
                    stream = container.add_stream(codec)
                    stream.time_base = TIME_BASE
                pts = max(int((arrived - start) / TIME_BASE), last_pts + 1)
                last_pts = pts
                packet = av.Packet(data)
                packet.stream = stream
                packet.time_base = TIME_BASE
                packet.pts = packet.dts = pts
                packet.is_keyframe = bool(kind)
                container.mux(packet)
        except (OSError, av.error.FFmpegError, ValueError):
            return                       # the reader went away
        finally:
            self.close(client)
            if container is not None:
                try:
                    container.close()
                except (OSError, av.error.FFmpegError, ValueError):
                    pass
