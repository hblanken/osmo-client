"""LiveTap: the camera's own video passed through unchanged (/live.h264) or muxed into MPEG-TS (/live.ts).

    python -m unittest discover tests
"""

from __future__ import annotations

import io
import threading
import time
import unittest

import av

from client.livets import LiveTap

SPS = b"\x00\x00\x00\x01\x67\x64\x00\x28\xac"        # H.264 SPS (High profile): marks a keyframe
IDR = b"\x00\x00\x00\x01\x65\x88\x84"
P = b"\x00\x00\x00\x01\x41\x9a\x02"


def encoded_units(frames: int = 60, size: tuple[int, int] = (320, 240)) -> list[bytes]:
    """Annex-B access units of a short test clip, keyframe every 15 frames, parameter sets in-band (as the
    cameras send them)."""
    out = io.BytesIO()
    container = av.open(out, "w", format="h264")
    stream = container.add_stream("libx264", rate=30)
    stream.width, stream.height = size
    stream.pix_fmt = "yuv420p"
    stream.codec_context.options = {"x264-params": "repeat-headers=1:bframes=0:keyint=15:aud=1"}
    units = []
    for index in range(frames):
        frame = av.VideoFrame(size[0], size[1], "yuv420p")
        for plane in frame.planes:
            plane.update(bytes([index * 4 % 256]) * plane.buffer_size)
        frame.pts = index
        for packet in stream.encode(frame):
            units.append(bytes(packet))
    for packet in stream.encode():
        units.append(bytes(packet))
    container.close()
    return units


class Sink(io.RawIOBase):
    """A response body: collects what is written; fails after `limit` bytes, like a reader going away."""

    def __init__(self, limit: int | None = None) -> None:
        self.data = bytearray()
        self.limit = limit

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        if self.limit is not None and len(self.data) >= self.limit:
            raise BrokenPipeError
        self.data += bytes(b)
        return len(b)


def serve(tap: LiveTap, fmt: str, sink: Sink) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=tap.serve, args=(sink, fmt, 2.0, stop), daemon=True)
    thread.start()
    for _ in range(100):                 # wait until the reader is registered
        if tap.snapshot()["readers"]:
            break
        time.sleep(0.01)
    return thread, stop


class LiveTapTest(unittest.TestCase):
    def test_idle_tap_only_tracks_the_codec(self):
        tap = LiveTap()
        tap.feed(P)
        self.assertIsNone(tap.codec)
        tap.feed(SPS + IDR)
        self.assertEqual(tap.codec, "h264")
        self.assertEqual(tap.snapshot(), {"readers": 0, "codec": "H.264", "kbps": 0, "fps": 0})

    def test_elementary_stream_starts_at_a_keyframe_and_is_unchanged(self):
        tap = LiveTap()
        sink = Sink()
        thread, stop = serve(tap, "es", sink)
        for unit in (P, P, SPS + IDR, P, P):
            tap.feed(unit)
        time.sleep(0.2)
        stop.set()
        thread.join(3)
        self.assertEqual(bytes(sink.data), SPS + IDR + P + P)
        self.assertEqual(tap.snapshot()["readers"], 0)

    def test_reset_waits_for_the_next_keyframe(self):
        tap = LiveTap()
        sink = Sink()
        thread, stop = serve(tap, "es", sink)
        tap.feed(SPS + IDR)
        tap.feed(P)
        time.sleep(0.1)
        tap.reset()                      # a new session: its P-frames belong to nothing we sent
        tap.feed(P)
        tap.feed(SPS + IDR)
        time.sleep(0.2)
        stop.set()
        thread.join(3)
        self.assertEqual(bytes(sink.data), SPS + IDR + P + SPS + IDR)

    def test_a_reader_that_goes_away_is_dropped(self):
        tap = LiveTap()
        thread, _ = serve(tap, "es", Sink(limit=0))
        tap.feed(SPS + IDR)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(tap.snapshot()["readers"], 0)

    def test_codec_change_closes_the_stream(self):
        tap = LiveTap()
        thread, _ = serve(tap, "es", Sink())
        tap.feed(SPS + IDR)
        tap.feed(b"\x00\x00\x00\x01\x40\x01\x0c")   # an H.265 VPS: the camera switched codecs
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_mpegts_holds_the_same_pictures(self):
        units = encoded_units()
        tap = LiveTap()
        sink = Sink()
        thread, stop = serve(tap, "ts", sink)
        for unit in units:
            tap.feed(unit)
            time.sleep(0.002)
        time.sleep(0.3)
        stop.set()                       # the writer sees it when its 2 s wait for video runs out
        thread.join(4)
        with av.open(io.BytesIO(bytes(sink.data)), format="mpegts") as container:
            stream = container.streams.video[0]
            self.assertEqual(stream.codec_context.name, "h264")
            packets = [p for p in container.demux(stream) if p.size]
            self.assertEqual(len(packets), len(units))
            self.assertEqual(bytes(packets[0]), units[0])     # copied, not re-encoded
            pts = [p.pts for p in packets]
            self.assertEqual(pts, sorted(set(pts)))           # strictly increasing
            frames = sum(len(stream.codec_context.decode(p)) for p in packets)
            self.assertGreater(frames, len(units) - 3)


if __name__ == "__main__":
    unittest.main()
