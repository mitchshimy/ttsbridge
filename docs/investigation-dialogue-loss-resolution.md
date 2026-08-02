# Resolution: Dialogue Loss Was an App-Level Audio Routing Bug

*Follow-up to "Investigation Report: Dialogue Loss After TTS Announcements
on TCL Android TV" and its addendum.*

---

## Summary

Both the original investigation and the addendum concluded this was a
TCL firmware / platform-level defect, with **no remaining app-level fix
available**. That conclusion was wrong. The issue was caused by every
announcement provider playing its audio through `USAGE_MEDIA` /
`STREAM_MUSIC` - the exact same shared output path used by the affected
streaming apps for their primary 5.1 playback. Routing announcement audio
through the accessibility usage/stream instead (`USAGE_ASSISTANCE_ACCESSIBILITY`
/ `STREAM_ACCESSIBILITY`) resolves the dialogue loss entirely. Testing
against the original known-bad configuration (Netflix, English 5.1 track,
internal speakers, TV audio processing enabled) confirms dialogue now
survives an announcement without needing to seek.

---

## What the original investigation and addendum got wrong

Both documents correctly eliminated Audio Focus *type* (transient vs.
duck), app-level ducking-vs-pausing behavior, and - per the addendum -
the Audio Focus *notification* itself (via the `manual_duck_only`
experiment) as root causes.

But the addendum's `manual_duck_only` experiment was not actually a
control for "no interruption at all." It removed the `AudioFocus`
API call, but every provider was still opening a second, concurrent
audio session on `STREAM_MUSIC` - the identical output path Netflix's
5.1 track was using - via:

* `DeviceTtsProvider`: no stream specified, so `TextToSpeech.speak()`
  defaulted to `STREAM_MUSIC`.
* `UrlAudioProvider` / `RemoteHttpTtsProvider`: both hardcoded
  `AudioAttributes.USAGE_MEDIA` on their `MediaPlayer` instances.

The `AudioAttributes` considered in the original investigation's
Hypothesis 2 were only ever applied to the **Audio Focus request**
(`AudioFocusManager`'s internal attrs object), which governs how the OS
treats the *request to interrupt* - it has no effect on which stream the
announcement's actual audio plays through. So "different AudioAttributes"
was tested for the interruption signal, but never for the actual playback
path. That gap is what both documents missed, and it's why the
`manual_duck_only` experiment still reproduced the bug: it wasn't
isolating "any concurrent audio" as a variable, only "concurrent audio
that came with a focus notification."

---

## The actual fix

All announcement playback now routes through the accessibility usage/
stream - the same mechanism TalkBack uses to speak over video without
disrupting the primary output pipeline - instead of `USAGE_MEDIA`:

* `AudioFocusManager` - focus-request `AudioAttributes` usage changed
  from `USAGE_MEDIA` to `USAGE_ASSISTANCE_ACCESSIBILITY`.
* `UrlAudioProvider`, `RemoteHttpTtsProvider` - `MediaPlayer`'s
  `AudioAttributes` usage changed the same way.
* `DeviceTtsProvider` - routed via `TextToSpeech.setAudioAttributes(...)`
  (modern, `AudioAttributes`-based) rather than the legacy
  `TextToSpeech.Engine.KEY_PARAM_STREAM` integer constant. That distinction
  mattered in practice: the legacy stream-type param is handed off into
  the platform TTS engine's own synthesis process, and
  `STREAM_ACCESSIBILITY` silently produced no audio there for a
  non-accessibility-service caller - `onStart` still fired (so our
  internal state correctly showed `SPEAKING`), but `onDone`/`onError`
  never came back, so playback hung until `AnnouncementEngine`'s own
  timeout. Switching to the `AudioAttributes`-based API - the same
  usage-based routing the `MediaPlayer` providers use successfully -
  fixed this cleanly.

`STREAM_ACCESSIBILITY` / `USAGE_ASSISTANCE_ACCESSIBILITY` requires API 26+;
below that, the app falls back to prior (`STREAM_MUSIC`) behavior, so this
has no effect on devices older than Android 8.0. Every TCL Android TV box
actually tested against is well above that floor.

---

## Test results

Reproduced under the exact failing configuration from the original
investigation:

* Netflix
* English audio track set to 5.1
* Internal TV speakers
* TV audio processing (Dolby/enhancement) enabled

**Dialogue survives the announcement.** No seeking required.

Also confirmed:

* On-device TTS (no engine specified) now produces audio correctly and
  no longer hangs in `SPEAKING` until timeout.
* Apps that rely on a genuine Audio Focus notification to duck/pause
  (Spotify, YouTube) were re-checked and continue to behave correctly.

---

## Disposition

* The original two documents remain in place for the investigative
  trail (the elimination of Audio Focus type, app-level ducking
  behavior, and the focus-notification-vs-hardware distinction was
  legitimate, useful work) but their **conclusions are superseded by
  this document**.
* `manual_duck_only` remains available as a strategy but is no longer
  believed to be relevant to this specific symptom - the actual fix
  was at the playback-attributes layer, not the focus-request layer.
* No firmware-level workaround (disabling TV audio processing, forcing
  stereo tracks) is needed anymore for this issue specifically. Those
  remain reasonable fallback options for any other output-path issue
  that isn't this one.
