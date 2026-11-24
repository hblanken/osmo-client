# Streaming to Unleashlive

Osmo Client can send the camera's **original** live video (H.264 from the Osmo Nano and Pocket 3, H.265
from the Pocket 4/4P and Action 4) to an [Unleashlive](https://cloud.unleashlive.com) device without
re-encoding it. It does this through [unleash-stream-cli](https://developer.unleashlive.com/stream-cli/),
which brings retries with backoff, continuous mode, WebRTC (WHIP), persistence and per-stream logs.

## How it works

```
camera ──UDP datalink──▶ Osmo Client ──▶ decoder ──▶ page (MJPEG /stream), --vcam
                              │
                              └─ LiveTap (client/livets.py): the access units before decoding
                                   ├─ /live.h264 (or /live.h265): the elementary stream, as it came
                                   └─ /live.ts: the same packets in MPEG-TS, for players (VLC, ffplay, OBS)

unleash-stream-cli service ── pulls http://127.0.0.1:8091/live.h264 ──▶ ffmpeg -c:v copy -f flv ──▶ rtmp://stream.unleashlive.com/rtmp/<device>?token=…
```

- `client/livets.py`: `LiveTap` fans the camera's Annex-B access units out to HTTP readers. Each reader
  starts at a keyframe, gets a bounded queue (a slow reader is dropped back to the next keyframe, so it
  never lags behind) and waits for a keyframe again after a session restart. Nothing is decoded or encoded.
  The cost with no reader is one keyframe check per frame.
- `client/unleash.py`: `StreamCli` runs `unleash-stream-cli add / stop / remove / list -v / logs` and
  follows the stream's state every 5 s. Tokens are masked everywhere (status, log, page) except the input.
- `osmo_client.py`: the `--unleash-*` launch flags. A stream given at launch starts once the live view
  runs. On quit the stream is stopped and removed unless `--unleash-keep` is set.
- Page: the **Unleashlive stream** card, with the URL (or device ID + token), name, Continuous,
  Re-encode, WebRTC, Playback and AI analysis, plus timeout and max retries under More. It has Start and
  Stop, and the stream-cli log.
- API: `POST /api/unleash/start` (JSON options), `POST /api/unleash/stop`, `GET /api/unleash/log`;
  `GET /api/status` has `unleash` and `live`.

| Launch flag | stream-cli `add` | Page |
|---|---|---|
| `--unleash-url URL` / `--url URL` | `-D URL` | Stream URL |
| `--unleash-device ID` + `--unleash-token TOKEN` (or `$UNLEASH_TOKEN`) | `-d` / `-t` | More → Device ID, Token |
| `--unleash-name NAME` | `-n` | Name |
| continuous by default, `--unleash-no-continuous` | `-C` | Continuous |
| `--unleash-encode` | `encode=true` on the destination | Re-encode |
| `--unleash-webrtc` | `--webrtc` | WebRTC |
| `--unleash-timeout SECS` | `--timeout` | More → Timeout |
| `--unleash-max-retries N` | `--max-retries` | More → Max retries |
| `--unleash-automated` | `--automated true` | AI analysis |
| `--unleash-no-playback` | `-p false` | Playback |
| `--unleash-keep` | (stream left running on quit) | |

## Setup notes

- Install: `curl -fsSL https://unleash-stream-cli.s3.ap-southeast-2.amazonaws.com/install.sh | bash -s -- install`.
  Update with `… | bash -s -- update`. Tested with **v0.5.5**.
- stream-cli uses the system `ffmpeg`/`ffprobe` only when they are on **its service's** PATH and the
  ffmpeg has the `whip` muxer (FFmpeg ≥ 8). Otherwise it downloads its own ffmpeg-static build into
  `~/.unleash-stream/`. On macOS with Homebrew, add `/opt/homebrew/bin` to the `PATH` under
  `EnvironmentVariables` in `~/Library/LaunchAgents/com.unleashlive.unleash-stream.plist`, then reload the
  service (`launchctl bootout gui/$(id -u)/com.unleashlive.unleash-stream; launchctl bootstrap gui/$(id -u) <plist>`).
- macOS: start Osmo Client from a terminal that has Bluetooth permission. A process started by an app
  without it (an IDE, an agent) is killed with SIGABRT when it first uses Bluetooth.
- Native Windows: stream-cli runs only in WSL; the card says so.

## Resolution

What gets streamed is the camera's **live view**, copied as it is. On the Osmo Nano that's a fixed
1280×720 at 30 fps (960×720 when recording 4:3), whatever the Format card says: Format is what the
camera records. Varying every byte of the live view start command didn't change that (docs/PROTOCOL.md,
5.8). The page's Unleashlive card shows the live view's actual size, bitrate and frame rate.

Routes to stream above 720p go around this app:

- the camera's own RTMP livestream, where the model offers one, set up in DJI Mimo with the Unleashlive
  Stream URL;
- USB webcam mode, where the model has it: unleash-stream-cli can stream a local camera (re-encoded).
