"""Streaming to Unleashlive with unleash-stream-cli (https://developer.unleashlive.com/stream-cli/).

stream-cli runs as a background service (launchd on macOS, systemd --user on Linux) and publishes a source
URL to an Unleashlive device, retrying with backoff. Its source here is the app's own `/live.h264` (see
livets.py): the camera's original video, which stream-cli copies to RTMP without re-encoding. This module
runs the `unleash-stream-cli` command (add / stop / remove / list / logs) for the page and the launch flags,
and follows the stream's status. The stream's token is never shown or logged: URLs are masked.

Native Windows is not supported by stream-cli (it runs in WSL), so on Windows this reports that.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from camlink.util import log

INSTALL_HINT = ("curl -fsSL https://unleash-stream-cli.s3.ap-southeast-2.amazonaws.com/install.sh "
                "| bash -s -- install")
DEVICE_RE = re.compile(r"^W[0-9a-z]{6,31}$", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
DEFAULT_TIMEOUT = 30


def find_binary() -> str | None:
    """$UNLEASH_STREAM_CLI, then the PATH, then where the installer puts it (~/.local/bin)."""
    candidates = [os.environ.get("UNLEASH_STREAM_CLI"), shutil.which("unleash-stream-cli"),
                  str(Path.home() / ".local" / "bin" / "unleash-stream-cli")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def device_of(url: str) -> str | None:
    """The device ID in a stream URL: rtmp://stream.unleashlive.com/rtmp/<ID>?token=… or the WHIP form
    https://webrtc.unleashlive.com/<ID>/whip?token=…"""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if parts and parts[-1].lower() == "whip":
        parts = parts[:-1]
    return parts[-1] if parts else None


def token_of(url: str) -> str | None:
    return dict(parse_qsl(urlsplit(url).query)).get("token")


def mask(url: str | None) -> str | None:
    """The URL with its token hidden and the device ID shortened to its first 5 and last 4 characters, for the
    page and the log."""
    if not url:
        return url
    parts = urlsplit(url)
    query = urlencode([(k, "***" if k.lower() == "token" else v) for k, v in parse_qsl(parts.query)], safe="*")
    path = parts.path
    device = device_of(url)
    if device and len(device) > 10:
        path = path.replace(device, f"{device[:5]}…{device[-4:]}")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def with_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != key] + [(key, value)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def clean_config(raw: dict) -> dict:
    """Checks and normalises the stream options (from the page or the launch flags). Raises ValueError."""
    url = (raw.get("url") or "").strip()
    device = (raw.get("device") or "").strip()
    token = (raw.get("token") or "").strip()
    if url:
        if not re.match(r"^(rtmps?|https?)://", url, re.IGNORECASE):
            raise ValueError("the stream URL starts with rtmp:// (or https:// for WebRTC)")
        device = device or device_of(url) or ""
        token = token or token_of(url) or ""
    elif not (device and token):
        raise ValueError("enter the stream URL, or the device ID and the token")
    if not DEVICE_RE.match(device):
        raise ValueError(f"'{device}' does not look like an Unleashlive device ID (W followed by letters and "
                         "digits)")

    def number(key: str) -> int | None:
        value = raw.get(key)
        if value in (None, ""):
            return None
        value = int(value)
        if value < 0:
            raise ValueError(f"{key} can't be negative")
        return value

    return {
        "url": url or None, "device": device, "token": token or None,
        "name": (raw.get("name") or "").strip() or None,
        "continuous": bool(raw.get("continuous", True)),
        "webrtc": bool(raw.get("webrtc", False)),
        "encode": bool(raw.get("encode", False)),
        "automated": raw.get("automated"),       # None = stream-cli's default
        "playback": raw.get("playback"),
        "timeout": number("timeout"),
        "max_retries": number("max_retries"),
    }


def add_args(cfg: dict, source: str) -> list[str]:
    """The `unleash-stream-cli add` arguments for a cleaned config (see clean_config)."""
    args = ["add", source]
    if cfg["webrtc"]:
        # WebRTC (WHIP) takes the device and the token, or a WHIP URL; it always encodes.
        if cfg["url"] and urlsplit(cfg["url"]).scheme.startswith("http"):
            args += ["-D", cfg["url"]]
        else:
            args += ["-d", cfg["device"], "-t", cfg["token"]]
        args.append("--webrtc")
    elif cfg["url"]:
        url = with_param(cfg["url"], "encode", "true") if cfg["encode"] else cfg["url"]
        args += ["-D", url]
    else:
        url = f"rtmp://stream.unleashlive.com/rtmp/{cfg['device']}?token={cfg['token']}"
        args += ["-D", with_param(url, "encode", "true") if cfg["encode"] else url]
    if cfg["name"]:
        args += ["-n", cfg["name"]]
    if cfg["continuous"]:
        args.append("-C")
    if cfg["timeout"] is not None:
        args += ["--timeout", str(cfg["timeout"])]
    if cfg["max_retries"] is not None:
        args += ["--max-retries", str(cfg["max_retries"])]
    if cfg["automated"] is not None:
        args += ["--automated", "true" if cfg["automated"] else "false"]
    if cfg["playback"] is not None:
        args += ["-p", "true" if cfg["playback"] else "false"]
    return args


def parse_list(text: str, device: str, name: str | None = None) -> dict | None:
    """Our stream's row in `unleash-stream-cli list -v`: {"state", "retries"}, or None when not listed.
    A row reads `🎥 <name>: <source> -> <destination> [running] 🔄`, then indented details."""
    lines = ANSI_RE.sub("", text).splitlines()
    for index, line in enumerate(lines):
        head = line.split(": ", 1)
        label = head[0].split(" ", 1)[-1].strip() if len(head) == 2 else ""
        if device not in line and (not name or label != name):
            continue
        status = re.search(r"\[(\w+)\]", line)
        found = {"state": status.group(1).lower() if status else "unknown", "retries": None}
        for detail in lines[index + 1:index + 4]:
            retries = re.search(r"Retries:\s*(\d+)", detail)
            if retries:
                found["retries"] = int(retries.group(1))
                break
        return found
    return None


class StreamCli:
    """The app's Unleashlive stream through unleash-stream-cli: one stream at a time, for the active camera."""

    def __init__(self, port: int, source_codec=lambda: None, video_fresh=lambda: False) -> None:
        self.port = port
        self.source_codec = source_codec   # "h264" / "hevc" of the live view, once known
        self.video_fresh = video_fresh     # the camera is sending video (a keyframe in the last seconds)
        self.cfg: dict | None = None       # the stream we started (None: none)
        self.pending: dict | None = None   # from the launch flags: started once the live view runs
        self.keep = False                  # leave the stream to stream-cli when the app quits
        self.state = "idle"                # idle | waiting | starting | running | stopped | failed | unknown | error
        self.retries: int | None = None
        self.error: str | None = None
        self.since: float | None = None
        self._lock = threading.Lock()
        self._binary: str | None = None
        self._version: str | None = None

    # ---- the command -------------------------------------------------------------------------
    def binary(self) -> str | None:
        if self._binary is None or not os.path.exists(self._binary):
            self._binary = find_binary()
        return self._binary

    def supported(self) -> str | None:
        """None when stream-cli can run here, else why not."""
        if sys.platform == "win32":
            return "unleash-stream-cli does not run on Windows itself: run Osmo Client and it in WSL"
        if self.binary() is None:
            return f"unleash-stream-cli is not installed: {INSTALL_HINT}"
        return None

    def run(self, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run([self.binary() or "unleash-stream-cli", *args], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)

    def version(self) -> str | None:
        if self._version is None and self.supported() is None:
            try:
                out = self.run("--version", timeout=5).stdout.strip()
                self._version = out.rsplit(" ", 1)[-1] if out else None
            except (OSError, subprocess.SubprocessError):
                return None
        return self._version

    def source_url(self) -> str:
        ext = "h265" if self.source_codec() == "hevc" else "h264"
        return f"http://127.0.0.1:{self.port}/live.{ext}"

    # ---- actions -----------------------------------------------------------------------------
    def start(self, raw: dict) -> dict:
        why = self.supported()
        if why:
            return {"ok": False, "error": why}
        try:
            cfg = clean_config(raw)
        except (ValueError, TypeError) as error:
            return {"ok": False, "error": str(error)}
        with self._lock:
            if self.cfg and self.cfg["device"] != cfg["device"]:
                self._quiet("remove", self.cfg["device"], "-y")   # one stream at a time
            self._quiet("remove", cfg["device"], "-y")            # stream-cli has no "replace"
            source = self.source_url()
            try:
                done = self.run(*add_args(cfg, source))
            except (OSError, subprocess.SubprocessError) as error:
                self.state, self.error = "error", str(error)
                return {"ok": False, "error": self.error}
            output = ANSI_RE.sub("", (done.stdout + done.stderr)).strip()
            if done.returncode != 0 or "success" not in output.lower():
                self.state, self.error = "error", self._hide(output) or f"exit code {done.returncode}"
                log(f"Unleashlive: could not add the stream: {self.error}")
                return {"ok": False, "error": self.error}
            self.cfg, self.pending = cfg, None
            self.state, self.error, self.retries, self.since = "starting", None, None, time.time()
            target = "WebRTC" if cfg["webrtc"] else mask(cfg["url"]) or cfg["device"]
            log(f"Unleashlive: streaming {source} to {target}"
                + (" (re-encoded)" if cfg["encode"] or cfg["webrtc"] else " (copied, not re-encoded)"))
        self.refresh()
        return {"ok": True}

    def stop(self) -> dict:
        """Stops and forgets the stream (stream-cli would otherwise bring it back after a restart)."""
        with self._lock:
            self.pending = None
            if not self.cfg:
                return {"ok": True}
            if self.supported() is None:
                self._quiet("stop", self.cfg["device"])
                self._quiet("remove", self.cfg["device"], "-y")
            log("Unleashlive: stream stopped")
            self.cfg, self.state, self.retries, self.since = None, "idle", None, None
        return {"ok": True}

    def shutdown(self) -> None:
        if self.cfg and self.keep:
            log("Unleashlive: the stream stays with unleash-stream-cli (--unleash-keep)")
            return
        self.stop()

    def logs(self, lines: int = 60) -> str:
        if not self.cfg or self.supported():
            return ""
        try:
            text = self.run("logs", self.cfg["device"], timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as error:
            return str(error)
        return self._hide("\n".join(ANSI_RE.sub("", text).splitlines()[-lines:]))

    # ---- status ------------------------------------------------------------------------------
    def refresh(self) -> None:
        cfg = self.cfg
        if not cfg or self.supported():
            return
        try:
            text = self.run("list", "-v", timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as error:
            self.state, self.error = "unknown", str(error)
            return
        row = parse_list(text, cfg["device"], cfg["name"])
        if self.cfg is not cfg:
            return                         # stopped meanwhile
        if row is None:
            self.state = "stopped"         # removed from outside (the terminal)
        else:
            self.state, self.retries = row["state"], row["retries"]

    def loop(self, stop: threading.Event) -> None:
        """Every 5 s: start a stream given at launch once the live view runs, and follow its status."""
        while not stop.wait(5):
            if self.pending and self.video_fresh():
                result = self.start(self.pending)
                if not result["ok"]:
                    log(f"Unleashlive: {result['error']}")
                    self.pending = None
            elif self.cfg:
                self.refresh()

    def snapshot(self) -> dict:
        cfg = self.cfg or self.pending
        state = self.state
        if (self.pending and not self.cfg) or (self.cfg and state != "running" and not self.video_fresh()):
            state = "waiting"              # stream-cli reads "stopped" while its source has nothing to send
        return {
            "supported": self.supported(),
            "version": self.version(),
            "state": state,
            "retries": self.retries,
            "error": self.error,
            "since": self.since,
            "source": self.source_url(),
            "destination": None if not cfg else ("WebRTC" if cfg["webrtc"] else mask(cfg["url"]) or cfg["device"]),
            "device": None if not cfg else cfg["device"],
            "name": None if not cfg else cfg["name"],
            "continuous": bool(cfg and cfg["continuous"]),
            "webrtc": bool(cfg and cfg["webrtc"]),
            "encode": bool(cfg and (cfg["encode"] or cfg["webrtc"])),
        }

    # ---- helpers -----------------------------------------------------------------------------
    def _quiet(self, *args: str) -> None:
        try:
            self.run(*args, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass

    def _hide(self, text: str) -> str:
        """stream-cli prints the destination in full: hide the token wherever it appears."""
        token = (self.cfg or self.pending or {}).get("token")
        text = re.sub(r"(token=)[^&\s'\"]+", r"\1***", text)
        return text.replace(token, "***") if token else text
