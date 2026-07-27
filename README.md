# TTS Bridge

A self-hosted announcement/TTS system for Android TV boxes, built to be
controlled from Home Assistant — with its own priority queue, pluggable
TTS engines, audio-focus handling, and a full HA integration (not just a
handful of YAML `rest_command`s).

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=YOUR_GITHUB_USERNAME&repository=ttsbridge&category=integration)

*(Replace `YOUR_GITHUB_USERNAME` in that link once the repo is actually
on GitHub — it won't resolve correctly until then.)*

Two halves, talking over plain HTTP on your LAN:

```
Home Assistant  <──── LAN, port 8098 ────>  Android TV app
 (custom_component)                          (foreground service)
```

Neither side hard-depends on the other. The Android app runs standalone —
it'll happily take announcements from `curl` or anything else that can
make an HTTP request. Home Assistant adds the integration layer on top:
entities, a proper setup wizard, push-based instant state updates, and
automatic recovery if the app ever stops responding.

---

## What it does

- **Priority queue** — emergency announcements interrupt whatever's
  playing; lower-priority ones queue up; duplicates and stale items get
  dropped automatically.
- **Pluggable engines** — on-device Android TTS as a guaranteed fallback,
  self-hosted HTTP TTS servers as a direct option, or render through any
  installed Home Assistant TTS entity (Piper, Google Translate, cloud
  engines, etc.) via a real dropdown in the HA action.
- **A real Home Assistant integration** — `config_flow` setup wizard
  (with an entity picker and a remote "start it for me" fallback if the
  app isn't running yet), a status sensor, `notify.send_message` for
  simple cases, `ttsbridge.announce` for everything richer, and optional
  auto-generated recovery automations.
- **Self-healing** — push-based instant state updates with a polling
  backstop, and automatic recovery if the Android process ever stops
  responding (works around boot-autostart restrictions some Android TV
  boxes impose on third-party apps).
- **No day-to-day dependency on adb or a PC** — once set up, Home
  Assistant talks to the TV directly over the LAN.

## Quick start

1. **[Read the setup guide](docs/setup-guide.md)** — step-by-step,
   prerequisites through your first working announcement, with a
   troubleshooting section built from real issues hit during development.
2. Build/install the Android app (`android/`) on your TV box.
3. Install the Home Assistant integration, either:
   - **Via HACS** (recommended): HACS → ⋮ → Custom repositories → add
     this repo's URL as an "Integration" → install `TTS Bridge` → restart
     HA. Update notifications come for free on future releases.
   - **Manually**: copy `custom_components/ttsbridge/` into your HA
     config's `custom_components/` folder and restart HA.
4. Settings → Devices & Services → Add Integration → **TTS Bridge**.

## Repo structure

```
android/              Android app (Gradle project) - installs on the TV
custom_components/
  ttsbridge/            Home Assistant custom_component (HACS-compatible
                         path: must sit at repo root, not nested)
hacs.json              HACS manifest - lets this repo be added as a
                        HACS custom repository
docs/
  setup-guide.md        Step-by-step installation & configuration
  reference.md           Full technical reference - architecture, every
                          endpoint/entity/service, known limitations
  investigation-*.md     A real hardware/firmware bug investigation (TCL
                          Android TV dialogue loss on 5.1 audio) - kept as
                          a reference for anyone hitting the same symptom
```

**Start with the setup guide if you just want this running.** The
reference doc is for understanding *how* it works or extending it -
not required reading to get started.

## Requirements

- An Android TV device you can install a custom APK on (developer
  options / ADB debugging enabled).
- Home Assistant, with the `androidtv` integration already configured for
  that device (used for remote start/recovery — see the setup guide for
  why this matters).
- Home Assistant's **Settings → System → Network → Internal URL** set
  explicitly (matters for push updates and TTS engine resolution -
  covered in the setup guide's prerequisites checklist).

## Known limitations

- Self-hosted `remote_http` TTS engines only work for servers that speak
  plain HTTP — HA's official Piper add-on speaks the Wyoming protocol
  (binary over TCP), which is a protocol mismatch, not a config issue.
  Route Piper/cloud engines through HA's own TTS entities instead (fully
  supported, see the reference doc).
- One TCL-specific hardware/firmware bug is documented but not fixable
  from application code: 5.1 surround audio can lose its dialogue channel
  after any interruption on some TCL Android TV firmware. See
  `docs/investigation-dialogue-loss.md` for the full investigation and
  workarounds (disable TV audio processing, or use stereo tracks).
- No automated test suite yet for the HA component — verification during
  development relied on exercising real code against an actual installed
  Home Assistant instance rather than mocks, but that wasn't formalized
  into a checked-in suite.

See `docs/reference.md` §7 for the full, current list of what's built vs.
deliberately deferred.

## License

Add your preferred license here (MIT is a common, permissive choice for
a project like this if you're not sure).
