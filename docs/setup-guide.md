# TTS Bridge — Setup Guide

*For how the system works internally, see
[`reference.md`](reference.md). This
document is purely "how do I get this running," start to finish.*

---

## Before you start — prerequisites checklist

Go through this list first. Skipping any of these is the cause of nearly
every issue hit during this project's development.

- [ ] **The `androidtv` integration is already set up in Home Assistant**
  for this TV, with working ADB access (you can already control power/
  volume from HA). This is required for: the config flow's "start it
  remotely" fallback, self-heal, and the auto-generated automations.
- [ ] **Settings → System → Network → Internal URL is set explicitly**
  (e.g. `http://192.168.2.104:8123`) — don't rely on auto-detection. This
  affects webhook push registration and TTS engine URL resolution. If
  you're running behind a reverse proxy (common if you use any remote-
  access service like Homeway, NabuCasa, etc.), auto-detection is
  especially likely to guess wrong.
- [ ] **A way to copy files onto your HA instance** - the Samba share
  add-on is the simplest option if you're on HAOS and don't already have
  something (Settings → Add-ons → Add-on Store → search "Samba share").
- [ ] **`adb` (Android platform-tools)** installed on a PC on the same
  network, and USB or network debugging enabled on the TV. Only needed
  for the *initial* app install and any manual troubleshooting - not
  needed for day-to-day operation once everything's running (see the
  reference doc §1 for why).

**You do not need to write or edit any YAML for the core system to work.**
That was true of the earlier version of this project; it is no longer
true now that the custom_component exists. The only reason you'd still
touch YAML is if you build your own cross-device orchestration logic
("Director" script) on top - entirely optional, and covered at the end of
this guide.

---

## Part 1 — Install the Android app

1. Get the APK (`ttsbridge_v2.zip` → extract → build with
   `gradle assembleDebug` if building from source, or use an existing
   `app-debug.apk`).
2. Install it:
   ```
   adb install -r app-debug.apk
   ```
3. Start it once, to get it out of Android's "stopped" state and confirm
   it actually works, before touching Home Assistant at all:
   ```
   adb shell am start-foreground-service -n dev.local.ttsbridge/.AnnouncementService
   ```
4. Confirm it's reachable over the network (replace with your TV's IP):
   ```
   curl http://192.168.2.100:8098/status
   ```
   Expect `{"state":"IDLE","current":null,"queueSize":0,"volume":...}`. If
   this fails, stop here and troubleshoot the app first (see
   Troubleshooting below) - nothing on the HA side will work until this
   does.

No configuration is needed on the app side beyond this - permissions,
cleartext traffic, foreground service setup are all already baked into
the manifest.

---

## Part 2 — Install the custom_component

1. Extract `ttsbridge_custom_component.zip` on your PC.
2. Connect to your HA config share:
   `\\<ha-ip>\config` (Windows File Explorer address bar).
3. If a `custom_components` folder doesn't exist there, create one.
4. Copy the whole `ttsbridge` folder into it, so you end up with:
   ```
   config/custom_components/ttsbridge/
       __init__.py
       manifest.json
       brand/icon.png
       ... (all the other .py files)
   ```
5. **Full restart**: Settings → System → Restart. (Not "reload YAML" -
   new Python modules need a real restart.)

---

## Part 3 — Add a device

1. Settings → Devices & Services → **Add Integration** → search
   **"TTS Bridge"**.
2. Enter the TV's IP address and port (`8098` is the default, shown
   pre-filled).
3. **What happens next depends on whether the app is already running:**

   **If it connects immediately:** you'll go straight to step 4.

   **If it can't connect** ("Bridge not responding"): you'll see a screen
   asking you to pick the matching Android TV device from a dropdown
   (populated automatically from your `androidtv` integration entities).
   Pick it, submit, and the integration will try starting the app
   remotely via ADB and retry the connection. If it still fails after
   that, double-check the app is genuinely installed and the TV is
   powered on and reachable on the network.

4. **"Install recommended automations?"** - pick the same Android TV
   device to have two automations written for you automatically
   (power-on start, and self-heal if the bridge ever stops responding),
   or leave it blank to skip and manage that yourself. This is entirely
   optional and can't be added later without re-adding the device - if
   you skip it now and change your mind, remove and re-add the device
   through the integration.
5. Done - the device now shows up as a Device card with a status sensor
   and a notify entity attached.

**Repeat this whole section for each additional TV** - the integration
supports multiple devices from the start, each with its own independent
entities, recovery cycle, and (if opted in) automations.

---

## Part 4 — Confirm it actually works

**Developer Tools → Actions**, YAML mode:
```yaml
action: ttsbridge.announce
target:
  entity_id: notify.<your_device>_announce
data:
  message: "Setup test, one two three"
```
You should hear it within a couple of seconds. Check
**Developer Tools → States** for `sensor.<your_device>_status` right
after - it should show `SPEAKING` briefly, then `IDLE`.

**If you installed the automations in Part 3, test those too**, since
they're the thing that matters most if you're ever away from a keyboard
when something goes wrong:
- Power the TV fully off and on, and confirm the sensor comes back to
  `IDLE` on its own without you touching anything.
- Force-stop the app (`adb shell am force-stop dev.local.ttsbridge`) while
  the TV stays on, and confirm it's automatically resurrected within a
  few minutes and you hear "Announcement bridge recovered."

---

## Part 5 — Optional configuration

**Picking a specific TTS voice per-announcement** (Piper, Google
Translate, Homeway Sage, or anything else you already have configured as
an HA `tts.*` entity): use the `tts_engine` field on `ttsbridge.announce`
- it renders as a real dropdown of your installed TTS entities. No extra
setup needed on this project's side; it just needs the HA TTS integration
itself to already be configured, which is outside this project's scope.

**Registering a self-hosted, genuinely HTTP-based TTS server** (not
Wyoming/Piper-via-HA - see the reference doc §2.5 for why that
distinction matters): use the bridge's own `/engines` API directly, e.g.
```
curl -X POST http://192.168.2.100:8098/engines -H "Content-Type: application/json" -d "{\"id\":\"my_engine\",\"baseUrl\":\"http://host:port/path\",\"jsonRequest\":true}"
curl -X POST http://192.168.2.100:8098/engines/default -H "Content-Type: application/json" -d "{\"chain\":[\"my_engine\"]}"
```

**Audio focus strategy** (rarely needed - see the reference doc §2.9 for
the specific hardware bug this was built to test):
```
curl -X POST http://192.168.2.100:8098/audio-focus-strategy -H "Content-Type: application/json" -d "{\"strategy\":\"manual_duck_only\"}"
```

**Cross-device orchestration** (multiple TVs, phone fallback, per-engine
priority order): this is genuinely custom, house-specific logic and
stays as a hand-written HA script/automation calling `ttsbridge.announce`
- not something this project provides out of the box, by design (see the
reference doc §6).

---

## Troubleshooting

**"Cannot connect" when adding a device, and you're sure the app is
running:**
- Confirm from the *HA machine's* network, not just your PC:
  `curl http://<tv-ip>:8098/status`. A working curl from your Windows PC
  doesn't guarantee HA's own network path is the same (different VLAN,
  Docker networking, etc.).
- Confirm the app didn't crash on start - `adb logcat -d -s
  TtsBridge/Service AndroidRuntime` right after starting it.

**The app won't start automatically on boot:**
- This is expected on some TVs (confirmed specifically on TCL Android TV
  boxes running "Guard" firmware, which blocks third-party auto-start
  with no user-facing override on Android 14+). This is why the
  power-on automation exists - it doesn't rely on the OS's own boot
  broadcast at all, just HA noticing the TV turned on.

**"Referenced entities ... are missing or not currently available" in
the logs, from a self-heal automation:**
- If you ever renamed the device's notify entity, an *older* generated
  automation file may still reference the old name. Remove and re-add
  the device to regenerate it (current versions target by `device_id`,
  which survives renames - see the reference doc §4.6 for why this
  changed).

**`Platform automation does not generate unique IDs. ID ... already
exists` in the logs after this integration reloads automations:**
- This is very likely a pre-existing duplicate automation ID *elsewhere*
  in your own config, unrelated to this project - it's just being
  surfaced because `automation.reload` now runs more often (every time a
  device is added/removed with automations enabled). Worth fixing on its
  own merits, but not something this integration caused.

**No internal HA URL warning in the logs:**
- Set Settings → System → Network → Internal URL explicitly, then reload
  the integration (Settings → Devices & Services → TTS Bridge → ⋮ →
  Reload). Until this is set, the system still works via 30-second
  polling - you just lose the instant push updates and the ability for
  TTS-engine URL resolution to build a reachable address.

**Windows `cmd.exe` + `adb`/`curl` gotchas** (not a bug, just genuinely
easy to trip on):
- `\` is *not* a line-continuation character in `cmd.exe` - a multi-line
  `adb` command with trailing `\` silently only runs the first line. Use
  `^` for line continuation in `cmd.exe`, or just keep commands on one
  line.
- Nested double quotes in `curl -d "{...}"` need escaping as `\"`, not
  single quotes - `cmd.exe` doesn't handle single-quoted JSON the way
  bash does.
- If a curl command contains a literal placeholder like `<HA_IP>`,
  `cmd.exe` will try to interpret `<` as file redirection and fail with
  "The system cannot find the file specified" - always substitute the
  real value first.
