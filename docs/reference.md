# TTS Bridge — Complete System Reference

*For step-by-step installation and configuration instructions, see
[`setup-guide.md`](setup-guide.md). This document
covers how the system works internally - architecture, every endpoint/
entity/service, and known limitations - not how to install it.*

This document explains the entire system, end to end: the Android app that
runs on the TV, the Home Assistant custom_component that talks to it, every
endpoint and entity each side exposes, how they fit together, what's
required for it all to work, and every quirk/limitation discovered along
the way. Nothing here is aspirational — everything described has been
built and, unless explicitly flagged otherwise, tested working.

---

## 1. Big Picture

Two independent pieces, talking over plain HTTP on your LAN:

```
Home Assistant (Proxmox/HAOS)  <──── LAN, port 8098 ────>  Android TV app
      "ttsbridge" custom_component        "TTS Bridge" (dev.local.ttsbridge)
```

- **The Android app** is a persistent background service. It owns a
  priority queue, decides what TTS engine to use, manages audio focus, and
  exposes a small HTTP API. It has **no dependency on Home Assistant at
  all** — it will happily accept announcements from `curl`, from any other
  system, or from nothing at all (it just sits idle).
- **The HA custom_component** is a normal HA integration (`config_flow`,
  entities, services) that acts as a client of the app's HTTP API, plus a
  few pieces of "glue" logic (recovery, push updates, TTS engine
  resolution) layered on top.

Neither side depends on your Windows PC or `adb` at all for day-to-day
operation — `adb`/`curl` from a PC were only ever used for development and
manual debugging. The one exception: HA's *recovery* mechanism uses ADB
(via the separate `androidtv` HA integration, not your PC) to restart the
Android app remotely when it's found unresponsive - see §7.

---

## 2. The Android App (`dev.local.ttsbridge`)

### 2.1 Core concept

Everything is built around **`AnnouncementService`**, a foreground
Android `Service` that:
1. Starts on boot (when it can - see §2.7) or on demand via `adb`/HA.
2. Owns a single **priority queue** of pending announcements.
3. Processes them one at a time, sequentially, on a dedicated worker
   thread.
4. Exposes a raw HTTP server on **port 8098** for control.
5. Pushes state changes to a registered webhook (HA) and/or can be polled.

It calls `startForeground()` as the very first thing in `onCreate()` -
before constructing anything else, including the TTS engine - because
Android kills the process (`ForegroundServiceDidNotStartInTimeException`)
if `startForeground()` isn't called within a few seconds of the service
being started, and binding the TTS engine can occasionally take longer
than that.

### 2.2 The announcement model — `core/Announcement.java`

Every request is built into an `Announcement`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | random UUID | assigned automatically |
| `text` | string | - | text to speak (device TTS or an engine's synthesis) |
| `url` | string | - | a pre-rendered audio URL to play directly instead |
| `priority` | enum | `NORMAL` | `EMERGENCY` / `HIGH` / `NORMAL` / `LOW` |
| `timeoutMs` | long | 15000 | hard ceiling on how long this is allowed to play |
| `interruptible` | bool | `true` | can a *higher*-priority item cut this off? |
| `duck` | bool | `false` | request `MAY_DUCK` vs plain `TRANSIENT` audio focus |
| `category` | string | `"general"` | used for dedup + staleness rules (e.g. `"motion"`) |
| `callbackUrl` | string | - | optional: POSTed to when this finishes/fails |
| `volume` | int | -1 (unchanged) | 0-100, sets stream volume before playing |
| `engine` | string | - | bridge-native engine id to try first (see §2.5) |

**Rule:** if both `text` and `url` are set, `url` always wins for
playback - `text` is still recorded for status/queue display and as a
label. At least one of `text`/`url` is required, or the request is
rejected with HTTP 400.

### 2.3 The queue — `core/AnnouncementQueue.java`

A thread-safe priority queue (lower `Priority.rank` = higher priority,
FIFO within the same rank), with two policies applied at insertion time:

- **Duplicate suppression:** an item is rejected (`"duplicate"` response)
  if another item with the same `dedupeKey()` (category + text/url) was
  *accepted* within the last **5 seconds** - not just "is still sitting in
  the queue." This matters because a short announcement can go from
  queued to already-playing before a near-simultaneous duplicate request
  even arrives; checking only the backlog would miss that.
- **Staleness:** each category has a max age. `"motion"` announcements
  older than **20 seconds** are silently dropped instead of played once
  they reach the front of the queue (a "someone's in the driveway" alert
  from 30 seconds ago isn't useful anymore). All other categories never
  go stale (max age 0 = disabled).

### 2.4 Interruption rules

- **EMERGENCY always interrupts** whatever is currently playing,
  regardless of that item's own `interruptible` flag.
- Any other priority only interrupts the current item if the current
  item's `interruptible` is `true` **and** the new item has strictly
  higher priority (`priority.rank <` current's rank).
- Interruption calls `provider.stop()` on the active provider, which
  notifies the engine's wait-loop *directly* (not via relying on Android's
  own TTS/MediaPlayer callbacks, which don't reliably fire on a manual
  stop - this was a real bug fixed mid-session: without it, a manual stop
  silenced the audio instantly but the queue worker sat blocked for up to
  the full timeout before moving on to the next item).

### 2.5 The engine system — `core/EngineConfig.java` / `EngineRegistry.java` / `AnnouncementEngine.java`

An announcement with an explicit `url` **always** bypasses engine
selection entirely and plays exactly that file via `UrlAudioProvider` - no
fallback chain, no engine chosen.

A `text`-only announcement resolves an ordered **fallback chain**:
1. The announcement's own `engine` field, if set and registered.
2. The registered **default chain** (an ordered list of engine ids, set
   via `POST /engines/default`).
3. **`"device"`** - always appended last, guaranteed, never needs
   registering. This is the on-device Android TTS engine and is always
   available.

Each engine in the chain is tried in order. A genuine failure (timeout,
connection error, bad response) falls through to the next engine. An
**interruption** (something cut it off on purpose) does *not* trigger
fallback - that's not the engine's fault, and burning through the chain
for no reason would be wrong.

Two engine **types** exist:
- **`device`** - the built-in Android TTS (`DeviceTtsProvider`). Implicit,
  never registered.
- **`remote_http`** - a self-hosted HTTP TTS server, called directly by
  the app with no Home Assistant involvement at all
  (`RemoteHttpTtsProvider`). Configurable per-registration:
  - `baseUrl` - where to POST.
  - `jsonRequest` - `true` sends `{"text": "..."}` as JSON;
    `false` sends the raw text as a plain-text body. Different
    self-hosted TTS servers expect one or the other; there's no single
    standard "Piper HTTP API."
  - The response is auto-detected: `Content-Type: audio/*` is read as raw
    bytes and played from a temp file; anything else is parsed as JSON
    and expected to contain a `"url"` field.

  **Known limitation, confirmed this session:** this only works for
  servers that genuinely speak plain HTTP. HA's official **Piper add-on
  speaks the Wyoming protocol** (binary messages over a raw TCP socket),
  which is fundamentally incompatible with `remote_http` - no port number
  or configuration fixes this, it's a protocol mismatch. For real-world
  use with Piper/Google Translate/Homeway Sage etc., see §2.6 below
  instead - HA-side rendering is the practical path for those.

  Registering an engine with an id starting with `"tts."` is rejected -
  that prefix is reserved on the HA side (see §4) to mean "resolve via a
  real Home Assistant TTS entity," and a bridge-native registration with
  that prefix would be silently unreachable.

### 2.6 Audio playback and focus — `core/AudioFocusManager.java`

Two **strategies**, switchable live via HTTP (persisted across restarts):

- **`system`** (default) - the normal, correct behavior. Requests
  `AUDIOFOCUS_GAIN_TRANSIENT` (or `..._MAY_DUCK` if the announcement's
  `duck` flag is set) via Android's official AudioFocus API, so other
  apps get a real notification and can pause/duck appropriately. Also
  manually dips the `STREAM_MUSIC` volume as a belt-and-braces measure for
  apps that don't duck cleanly on their own.
- **`manual_duck_only`** - skips the AudioFocus API entirely. No other
  app ever receives *any* focus-loss notification, duck or pause. The
  bridge just directly lowers and restores `STREAM_MUSIC` volume around
  its own playback. Built specifically to test/work around a suspected
  TV-firmware bug (see §2.9) - **confirmed via testing that it does not
  fix that specific bug**, so it remains available but is not the
  default. It also has a real downside if used as default: well-behaved
  apps (Spotify, YouTube) that rely on a genuine focus notification to
  pause/duck correctly would no longer receive one.

### 2.7 Boot behavior and the TCL "Guard" problem

`BootReceiver` listens for `BOOT_COMPLETED` and starts the service. **On
this specific TCL Android TV box, this does not work** - confirmed via
logcat showing TCL's own `TclAppBootManagerImpl` denying the broadcast
with `reason='callee_does't_have_OP_AUTO_START_permission'`. This is a
proprietary OEM gatekeeper (TCL's "Guard" app, specifically its
`AutoControlBootService`) that blocks third-party apps from auto-starting
on boot, and - confirmed via forum research - **Android 14 removed the
user-facing settings screen that used to let you grant this exception.**
There is no known way to fix this at the app level or via a device
setting on current firmware.

**The practical workaround lives entirely on the HA side** (§7): an
automation starts the service via `androidtv.adb_command` whenever the TV
powers on, and a separate recovery mechanism (§4.5, §7) restarts it if it
ever goes unresponsive mid-session. The app itself cannot solve this - it
requires an external trigger.

### 2.8 Networking details

- The HTTP server (`http/ControlHttpServer.java`) is a **hand-rolled, raw
  socket-based server** - no external library, so the app stays a single
  APK. It binds to all network interfaces on port 8098, so it's directly
  reachable at the TV's LAN IP from any device on the network - no `adb
  forward` required for this (that's a separate, PC-local mechanism only
  needed if you want `localhost:8098` to work on a dev machine).
- **`android:usesCleartextTraffic="true"`** is set in the manifest.
  Android blocks outgoing plaintext HTTP by default since API 28 - this
  broke outgoing webhook pushes and callback URLs (`"Cleartext HTTP
  traffic to <ip> not permitted"`) until this was added. Confirmed
  necessary and sufficient.
- The server has one real, accepted limitation: request bodies are read
  char-by-char against a byte `Content-Length` header, so non-ASCII text
  in an announcement sent via HTTP could theoretically stall until socket
  timeout. Not fixed (low priority, ADB/curl-based English text testing
  never hit it), but documented as a known rough edge.

### 2.9 Known hardware/firmware limitation: dialogue loss on 5.1 audio (TCL-specific)

Thoroughly investigated and **confirmed not to be a bug in this app**.
Summary of the finding: on this TCL Android TV, interrupting **5.1
surround audio** playback (in Netflix, Nuvio, etc.) with *any* audio
interruption - regardless of duck vs. pause, regardless of whether
Android's AudioFocus API is even used at all (confirmed via the
`manual_duck_only` experiment) - causes the center/dialogue channel to be
lost on resume, while background/effects channels continue normally.
Stereo audio is never affected. Disabling the TV's built-in audio
processing (Dolby/enhancement/DSP), or using stereo tracks instead of
5.1, both eliminate the issue completely. This is a TV firmware/hardware
defect in multichannel audio restoration after interruption, sitting
below anything reachable from application code on Android. No further
app-level investigation is planned; see the project's dedicated
investigation report and addendum for the full methodology.

### 2.10 Persisted state (`SharedPreferences`, file `ttsbridge_prefs`)

| Key | Set by | Purpose |
|---|---|---|
| `webhook_url` | `POST /webhook` | where to push state changes |
| `engines_registry` | `POST /engines`, `/engines/default` | registered engines + fallback chain, as one JSON blob |
| `audio_focus_strategy` | `POST /audio-focus-strategy` | `system` or `manual_duck_only` |

All three persist across app restarts and TV reboots - none of this needs
re-sending unless you want to change it, or the app's data is cleared
(fresh install).

---

## 3. Android App — Full HTTP API Reference

Base URL: `http://<tv-ip>:8098`. All bodies are JSON. No authentication -
this is a LAN-only tool, not designed to be exposed to the internet.

| Method | Path | Body | Response | Notes |
|---|---|---|---|---|
| `POST` | `/announce` | `Announcement` fields (see §2.2) | `{id, status, queueSize}` | `status`: `"queued"` or `"duplicate"` |
| `POST` | `/stop` | `?clear=true` (query, optional) | `{stopped, clearedQueue}` | stops current playback; optionally wipes the queue too |
| `GET` | `/status` | - | `{state, current, queueSize, volume}` | `state`: `IDLE`/`SPEAKING`/`BUSY` |
| `GET` | `/queue` | - | `{queue: [...], size}` | snapshot of pending items |
| `POST` | `/volume` | `{level: 0-100}` | `{volume}` | sets stream volume directly |
| `GET` | `/webhook` | - | `{url}` (or `null`) | current push registration |
| `POST` | `/webhook` | `{url}` | `{url, registered}` | registers where to push state; empty/omitted `url` clears it; fires an immediate push on registration |
| `GET` | `/engines` | - | `{engines: [...], defaultChain: [...]}` | all registered `remote_http` engines |
| `POST` | `/engines` | `{id, baseUrl, jsonRequest}` | the registered `EngineConfig` | registers/updates one engine; rejects `"device"` and any `"tts."`-prefixed id |
| `POST` | `/engines/remove` | `{id}` | `{removed: bool}` | |
| `POST` | `/engines/default` | `{chain: [...]}` | `{defaultChain}` | sets fallback order; `"device"` is always appended automatically regardless of what's listed |
| `GET` | `/audio-focus-strategy` | - | `{strategy}` | |
| `POST` | `/audio-focus-strategy` | `{strategy}` | `{strategy}` | `"system"` or `"manual_duck_only"`; persisted |

**Legacy intent-based path (still supported, backward compatible):**
```
adb shell am start-foreground-service -n dev.local.ttsbridge/.AnnouncementService --es text "..." [--es url "..."] [--es priority "..."]
```
Gets funneled into the same queue at `NORMAL` priority. Kept specifically
so nothing that predates the HTTP API breaks.

### 3.1 curl quick reference

Copy-pasteable examples for every endpoint. Replace `192.168.2.100` with
the actual device IP. On Windows `cmd.exe`, inner double quotes in `-d`
need escaping exactly as shown (`\"`) - this bit us more than once during
development.

```
:: Announce
curl -X POST http://192.168.2.100:8098/announce -H "Content-Type: application/json" -d "{\"text\":\"Hello\"}"
curl -X POST http://192.168.2.100:8098/announce -H "Content-Type: application/json" -d "{\"url\":\"http://example.com/clip.mp3\",\"priority\":\"emergency\"}"

:: Stop / status / queue
curl -X POST http://192.168.2.100:8098/stop
curl -X POST "http://192.168.2.100:8098/stop?clear=true"
curl http://192.168.2.100:8098/status
curl http://192.168.2.100:8098/queue

:: Volume
curl -X POST http://192.168.2.100:8098/volume -H "Content-Type: application/json" -d "{\"level\":50}"

:: Webhook
curl http://192.168.2.100:8098/webhook
curl -X POST http://192.168.2.100:8098/webhook -H "Content-Type: application/json" -d "{\"url\":\"http://192.168.2.104:8123/api/webhook/<id>\"}"

:: Engines (bridge-native, remote_http only - see §2.5 for why this is
:: rarely useful for Wyoming/cloud-based engines specifically)
curl http://192.168.2.100:8098/engines
curl -X POST http://192.168.2.100:8098/engines -H "Content-Type: application/json" -d "{\"id\":\"piper_direct\",\"baseUrl\":\"http://host:port/path\",\"jsonRequest\":true}"
curl -X POST http://192.168.2.100:8098/engines/remove -H "Content-Type: application/json" -d "{\"id\":\"piper_direct\"}"
curl -X POST http://192.168.2.100:8098/engines/default -H "Content-Type: application/json" -d "{\"chain\":[\"piper_direct\"]}"

:: Audio focus strategy
curl http://192.168.2.100:8098/audio-focus-strategy
curl -X POST http://192.168.2.100:8098/audio-focus-strategy -H "Content-Type: application/json" -d "{\"strategy\":\"system\"}"
curl -X POST http://192.168.2.100:8098/audio-focus-strategy -H "Content-Type: application/json" -d "{\"strategy\":\"manual_duck_only\"}"
```

---

## 4. The Home Assistant Custom Component (`custom_components/ttsbridge`)

A real HA integration - `config_flow`, entities, a device card, proper
service schemas with validation - rather than the earlier YAML
(`rest_command`/`script`/`template:` sensor) approach it replaced.
Multi-instance from the start (nothing assumes only one TV) - **confirmed
in practice**, not just by design: a second TV was added entirely through
the HA config flow, with zero manual `adb` commands from the user. That
run exercised the full flow end to end for real - the initial
connectivity check, the "not responding, want to start it remotely?"
fallback step (§4.1 `config_flow.py`), `androidtv.adb_command` actually
resurrecting the process, the retry succeeding, and the second device
showing up as a fully independent entry with its own entities, its own
coordinator, its own recovery cycle - which is meaningfully stronger
evidence than the single-device sandbox verification the component was
originally built and tested against.

### 4.1 File-by-file

- **`manifest.json`** - domain `ttsbridge`, `config_flow: true`,
  `iot_class: local_push` (push-primary, poll as backstop - see §4.4).
- **`const.py`** - shared constants (`DOMAIN`, `DEFAULT_PORT`, etc.)
- **`api.py`** — **`BridgeApiClient`**: a thin async `aiohttp` wrapper
  around every endpoint in §3. Capability-oriented method names
  (`announce_audio`, `announce_text`, `set_volume`, `register_webhook`,
  `register_engine`, `set_default_engine_chain`, `cancel`, `status`,
  `queue`, `engines`) rather than 1:1 REST-call mirrors, so callers above
  this layer are insulated from the transport.
- **`bridge.py`** — **`AnnouncementBridge`**: a thin pass-through layer
  over `BridgeApiClient`, deliberately kept simple. Exists as the seam
  other logic (recovery, webhook registration) hangs off of, without
  bloating the API client itself.
- **`coordinator.py`** — **`TtsBridgeCoordinator`** (`DataUpdateCoordinator`):
  polls `/status` every **30 seconds**. This is the backstop, not the
  primary update path (see §4.4). Any failure raises `UpdateFailed`, which
  is what drives `available` to `False` on all entities - confirmed
  directly against HA's own source (`CoordinatorEntity.available` is
  simply `coordinator.last_update_success`).
- **`webhook.py`** — registers a genuine HA-native webhook
  (`homeassistant.components.webhook`) as the **push fast-path**. Details
  in §4.4.
- **`recovery.py`** — **`RecoveryManager`**: watches the coordinator and
  reacts to sustained failure/recovery. Details in §4.5.
- **`config_flow.py`** — setup wizard. Enter host/port, validated live
  against `/status`. If unreachable, offers to start the bridge remotely
  via `androidtv.adb_command` before giving up (an entity picker scoped to
  `domain: media_player, integration: androidtv`). Either way, then offers
  an opt-in "install recommended automations?" step (see §6) before the
  entry is actually created. Also supports `async_step_reconfigure` for
  editing an existing entry's IP in place (important since DHCP can
  reassign it) without creating a duplicate entry - deliberately does
  *not* re-offer automation installation, since that's a one-time setup
  choice, not something reconfiguring an IP should re-prompt for.
- **`sensor.py`** — one entity per device: **status sensor**. State is
  `IDLE`/`SPEAKING`/`BUSY`; attributes are `current`, `queue_size`,
  `volume`. Grouped under a proper Device card.
- **`notify.py`** — **`TtsBridgeNotifyEntity`**: both the standard
  `notify.send_message` action and the custom `ttsbridge.announce`
  service. Details in §4.6.
- **`services.yaml`** - field descriptions/selectors powering
  autocomplete and validation in Developer Tools → Actions.
- **`strings.json`** / **`translations/en.json`** - UI text (both files
  needed identically - HA's runtime translation loading for *custom*
  components reads `translations/<lang>.json` directly, not
  `strings.json`, which is really the core-integration build-pipeline
  source).
- **`automations.py`** - opt-in generation of the power-on/self-heal
  automations described in §6, triggered from `config_flow.py`'s
  `async_step_setup_automations` and cleaned up automatically on device
  removal.
- **`brand/icon.png`** - the integration's icon, shown in the
  Add Integration search results and the device card. Ships directly in
  the component's own folder (`brand/icon.png`, 256×256) - as of HA
  2026.3, custom integrations can self-host their own brand icon this
  way, no submission to the separate `home-assistant/brands` repo
  needed, and no manifest.json changes required to enable it.

### 4.2 What HA data storage looks like

Per config entry, `hass.data[DOMAIN][entry.entry_id]` holds:
```python
{
    "client": BridgeApiClient,
    "bridge": AnnouncementBridge,
    "coordinator": TtsBridgeCoordinator,
    "recovery_manager": RecoveryManager | None,
    "webhook_id": str,
}
```

### 4.3 Setup sequence (`async_setup_entry`)

1. Build `BridgeApiClient` + `AnnouncementBridge` + `TtsBridgeCoordinator`.
2. `coordinator.async_config_entry_first_refresh()` - validates
   connectivity *and* populates initial data in one call; raises
   `ConfigEntryNotReady` automatically on failure (confirmed against real
   HA source), which triggers HA's built-in setup retry-with-backoff.
3. Get-or-create a persisted, random (`secrets.token_hex(32)`) webhook id,
   register the HA-side receiver for it - this always succeeds, it's just
   a token, no network resolution needed.
4. Attempt to resolve that webhook id into a real, LAN-reachable URL
   (`allow_external=False` - the TV can't be assumed to reach an external
   HA URL) and register it with the device via `POST /webhook`. Best-effort:
   if this fails (no internal HA URL configured, or the device is
   unreachable at that exact moment), setup still proceeds - falls back to
   30s polling until the next successful push/recovery cycle registers it.
5. Construct `RecoveryManager` **regardless** of whether step 4 succeeded
   - its event-firing (§4.5) is a separate concern from webhook
   registration and must not be silently disabled by an unrelated failure.
6. Forward to the `sensor` and `notify` platforms.

### 4.4 The push fast-path (`webhook.py`)

The Android app pushes state changes (§2.1, §3 `/webhook`) the instant
they happen - not just on a timer. HA receives these via a real,
registered webhook:

- **Registered with `local_only=True`** - HA rejects any non-LAN request
  to it at the framework level, before any application code runs.
- The unguessable id (confirmed via source: `webhook.async_generate_id()`
  literally uses `secrets.token_hex(32)`) is generated once and persisted
  in the config entry, so it survives HA restarts without needing
  regeneration.
- On receipt, the handler calls
  `coordinator.async_set_updated_data(payload)` - confirmed against real
  HA source that this **also** resets `last_update_success = True` and
  reschedules the next poll, meaning a push is fully equivalent to a
  successful poll, not a separate side channel.
- **The 30-second poll (§4.1) keeps running underneath regardless** - this
  is deliberately push-primary-with-poll-backstop, not push-only. Without
  the poll, a device that stops pushing entirely (crashed, network
  dropped) would never be detected as unavailable.
- **The Android app also heartbeats every 60 seconds even when
  completely idle**, specifically so HA can distinguish "quiet but
  healthy" from "actually dead" - without this, a healthy-but-silent TV
  during a period with no announcements would look identical (from HA's
  side) to one that died hours ago.
- **Volume changes made via the physical remote** are also pushed
  instantly (not just app-controlled changes) - the app registers a
  debounced (400ms) `BroadcastReceiver` for `AudioManager.VOLUME_CHANGED_ACTION`,
  filtered to `STREAM_MUSIC` only, specifically so remote-button volume
  changes get the same instant treatment as everything the app already
  controlled directly.

### 4.5 Recovery (`recovery.py`)

Scope, deliberately bounded - it does **not** reach into `androidtv`'s ADB
connection itself:

- **On sustained failure** (coordinator transitions from available to
  unavailable): fires a real HA event, `ttsbridge_recovery_needed`
  (`hass.bus.async_fire` - not a dispatcher signal, since dispatcher
  signals are Python-internal only and can't be used as an automation
  trigger). Then **keeps re-firing it every 150 seconds** for as long as
  it stays down - not just once. This persistence is deliberate and
  important: a single one-shot event tied to a `state` trigger would only
  fire on the *transition*, and this is what actually recovered a real
  multi-hour outage during development (the TCL boot-block problem, §2.7)
  - a one-shot signal would have missed every retry after the first.
- **On recovery**: re-registers the webhook (URL persisted from setup, or
  re-resolved), fires `ttsbridge_recovered`.
- **The actual "restart the process" action lives in YAML, not Python**
  (§7) - it calls `androidtv.adb_command`, a cross-integration action with
  no formal dependency guarantee, and this was a deliberate scoping
  decision: self-heal is the single highest-consequence piece of this
  whole system, and splitting it (Python owns detection/timing, YAML owns
  the already-proven-working ADB call) avoided rewriting a load-bearing
  safety mechanism in the same pass as everything else.

### 4.6 Services and entities exposed

**`sensor.<device>_status`** - see §4.1.

**`notify.<device>`** (entity domain `notify`) exposes:

1. **`notify.send_message`** (the standard HA notify interface) - kept
   deliberately minimal: `message` only. `NotifyEntity`'s real interface
   (confirmed against source) only supports `message`/`title` - there's
   no arbitrary `data:` passthrough the way the old legacy
   `BaseNotificationService` platform had. This minimalism is what makes
   it interoperable with HA's notify groups and anything else written
   against the standard interface.

2. **`ttsbridge.announce`** (a custom **entity service** registered on
   this platform via `EntityPlatform.async_register_entity_service`,
   which gives free `entity_id`/`device_id` targeting resolution) -
   carries everything richer:

   | Field | Type | Required | Notes |
   |---|---|---|---|
   | `message` | text | one of message/url required | |
   | `url` | text | one of message/url required | pre-rendered audio, bypasses engine chain |
   | `priority` | select | no, default `normal` | `emergency`/`high`/`normal`/`low` |
   | `category` | text | no, default `general` | dedup/staleness grouping |
   | `engine` | text | no | bridge-native engine id (`"device"`, or anything registered via `/engines`); also accepts a manually-typed `tts.*` id for backward compatibility |
   | `tts_engine` | **entity selector**, `domain: tts` | no | a real dropdown of installed HA TTS entities (Piper, Google Translate, Homeway Sage, etc.) |
   | `speak_timeout` | number (ms) | no | overrides the bridge's default playback ceiling |

   Validation: `cv.has_at_least_one_key(message, url)` enforced at the
   **schema level** - a call with neither fails instantly in HA with a
   clear error, before any network round-trip to the device. `message`
   and `url` are **not** mutually exclusive - both can be set together
   (`url` wins for playback, `message` is still recorded as a
   fallback/label, matching `announce_audio`'s existing `text_fallback`
   parameter). Returns the bridge's own JSON response
   (`{id, status, queueSize}`) via `supports_response=OPTIONAL`, so
   callers can check success the same way the old `rest_command` +
   `response_variable` pattern did.

   **If both `engine` and `tts_engine` are given, `tts_engine` wins** -
   picking from a dropdown is treated as a more deliberate choice.

   **How `tts_engine`/a `tts.`-prefixed `engine` actually resolves:**
   rendered *in-process*, no REST round-trip to HA itself, no token
   needed - via `media_source.generate_media_source_id("tts", identifier)`
   + `media_source.async_resolve_media(hass, media_content_id, None)`,
   confirmed against real HA source and empirically tested against
   `tts.piper`, `tts.google_translate_en_com`, and
   `tts.homeway_sage_free_text_to_speech` specifically. **One real gotcha
   found and fixed**: `async_resolve_media()` can return a bare relative
   path (e.g. `/api/tts_proxy/xxx.mp3`) rather than an absolute URL when
   called without a target media_player context - meaningless to the
   bridge, a completely separate device fetching this over the network.
   The result is checked and, if relative, rebuilt into an absolute URL
   via `get_url(hass, prefer_external=False, allow_internal=True)` before
   being handed to the device. This was the cause of a real bug during
   development: HA reported success, the bridge queued the request and
   returned success too, and nothing played, with zero errors logged
   anywhere - because the failure happened entirely on the Android side,
   trying to open a URI that was never valid, well after HA had already
   declared victory.

**Events fired** (not entities, but real HA events - triggerable from
automations via `trigger: event`):
- `ttsbridge_recovery_needed` - `{entry_id}`, re-fires every 150s while down.
- `ttsbridge_recovered` - `{entry_id}`, fires once on recovery.

---

## 5. What's Required For All Of This To Work

**On the Android side:**
- The APK installed and, at minimum, started once (`am
  start-foreground-service` or the boot receiver, if it worked on your
  device - it doesn't on this TCL box, see §2.7).
- Network reachability from HA to the TV's LAN IP on port 8098 (same LAN,
  no VPN/segmentation blocking it).

**On the Home Assistant side:**
- The `androidtv` integration already configured for this specific TV
  (used by both `RecoveryManager`'s YAML automation and the config flow's
  start-service recovery step) - without it, self-heal and the
  first-time "try starting it for me" flow degrade gracefully (clear
  error messages) but don't function.
- **Settings → System → Network → Internal URL** set explicitly. Several
  things silently degrade without it: webhook registration falls back to
  polling-only, and `tts_engine` resolution's relative-URL fix depends on
  it too. Confirmed this matters in practice - a reverse proxy config
  (this setup uses one, for Homeway's Alexa/Google Assistant bridging)
  can interfere with HA's automatic internal/external URL
  auto-detection, making this explicit setting necessary rather than
  optional.
- A `packages/` folder (recommended) or careful manual merging if pasting
  YAML config directly into `configuration.yaml`, to avoid duplicate
  top-level key collisions (`sensor:`, `automation:`, `script:`,
  `rest_command:`, `template:` are all things a typical existing config
  already uses).

---

## 6. What Still Lives in YAML (Deliberately, Not a Gap)

**For new setups, the power-on/self-heal automations are no longer
hand-written** - they're generated automatically by the integration
itself, opt-in, during device setup (config_flow's
`async_step_setup_automations`, implemented in `automations.py`). See the
setup guide for the actual click-through. What follows describes what
that generated file contains and why one piece still can't move into
Python - useful background whether you're looking at an auto-generated
file or one written by hand before this feature existed.

1. **The self-heal / power-on automations**
   (`automations/ttsbridge_<entry_id>.yaml` if auto-generated):
   - Trigger on the associated `media_player` entity turning `"on"` →
     wait 5s → call `androidtv.adb_command` with the start command.
   - Trigger on the `ttsbridge_recovery_needed` **event** (not a `state`
     trigger - a `time_pattern`-based version was tried first and
     rejected: it fired a trace every single tick regardless of whether
     anything was wrong, which blew through HA's default 5-stored-traces
     limit within minutes and left nothing to debug hours later when it
     mattered), filtered to this specific device's `entry_id` so
     multiple devices' automations don't cross-trigger each other → same
     restart sequence, plus a "recovered" announcement via
     `notify.send_message` targeted by `device_id` (not `entity_id` -
     stable across the entity ever being renamed later, confirmed
     necessary after a real rename broke an earlier hardcoded-entity_id
     version during development).
   - Webhook re-registration does **not** appear in the generated
     automation - `RecoveryManager` already handles that entirely in
     Python (§4.5). The generated automation's only job is the one thing
     that genuinely can't move into Python: resurrecting the process via
     `androidtv.adb_command`, a cross-integration action with no formal
     dependency guarantee, which is exactly why it stays in
     user-editable, user-visible YAML rather than being called directly
     from the component (see `automations.py`'s module docstring for the
     full reasoning on why file-writing itself is treated as a bigger,
     more deliberate step than anything else this integration does).
   - Deleting the device through the integration removes this file
     automatically (`async_remove_entry`) and reloads automations.

2. **The Director** (`script.tts_announce`, in some setups) - the
   higher-level "which voice, which fallback order, which TV, phone
   fallback" orchestration logic that decides *what* to announce and
   *where*, calling down into `ttsbridge.announce`. This intentionally
   stays as user-editable automation logic rather than being absorbed
   into the component, since it's house-specific business logic, not
   something the integration should own or need to be modified (and
   released) to change.

---

## 7. Known Gaps / Explicitly Deferred (Not Yet Built)

- **`script.tts_announce` has not yet been rewritten** to call
  `ttsbridge.announce` directly instead of the raw `rest_command` +
  manual `get_tts_url` dance - this was the natural next step once
  `notify.py` landed, not yet done.
- **No `select` entity for audio focus strategy** - `/audio-focus-strategy`
  (§3) is fully functional but curl/HTTP-only right now; a proper
  `select.py` platform (a real dropdown entity, `system` vs
  `manual_duck_only`) was discussed but not built, since the underlying
  investigation concluded the alternate strategy doesn't actually fix the
  TCL dialogue-loss bug it was built to test - low urgency, but the
  hook (`GET`/`POST /audio-focus-strategy`) is ready whenever it's wanted.
- **`EngineRegistry`/`remote_http` engines are largely dormant** in
  practice - none of the three real-world engines in use (Homeway Sage,
  Google Translate, Piper-via-Wyoming) can run as a `remote_http`
  registration; the practical path for all three is HA-side rendering via
  `tts_engine` (§4.6). The bridge-native engine system remains fully
  functional and is a real option for any genuinely HTTP-based
  self-hosted TTS server, if one is ever added.
- **No automated test suite** for the HA component (`pytest-homeassistant-
  custom-component` or similar) - verification throughout development
  relied on installing a real HA instance in a sandbox and directly
  exercising code against actual HA source/behavior (confirming exact
  function signatures, running real schema validation, importing every
  module against the real package) rather than assumption, but this
  wasn't formalized into a repeatable, checked-in test suite.
- **`RemoteHttpTtsProvider` has no auth/header support** - fine for a
  bare self-hosted server, would need extending before pointing it at any
  cloud TTS API that requires an API key in the request.

---

## 8. Quick-Reference: Every Persisted / Configurable Thing

| What | Where it lives | How to change it |
|---|---|---|
| Webhook push target | Android `SharedPreferences` | `POST /webhook` (or automatic, via HA setup/recovery) |
| Registered engines + fallback chain | Android `SharedPreferences` | `POST /engines`, `POST /engines/default` |
| Audio focus strategy | Android `SharedPreferences` | `POST /audio-focus-strategy` |
| HA's known host/port for the device | HA config entry data | Config flow, or the reconfigure step |
| HA's generated webhook id | HA config entry data | Generated once automatically, persists across restarts |
