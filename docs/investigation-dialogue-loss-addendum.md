# Addendum: Manual-Duck-Only Experiment and Final Conclusion

> **⚠️ Superseded.** The "final conclusion" below (no remaining
> app-level fix) was wrong - the `manual_duck_only` experiment did not
> actually control for concurrent audio on the shared output path (see
> [`investigation-dialogue-loss-resolution.md`](./investigation-dialogue-loss-resolution.md)).
> The experiment itself and its result are accurately reported; the
> interpretation was not.

*Follow-up to "Investigation Report: Dialogue Loss After TTS Announcements on TCL Android TV"*

---

## Purpose

The original investigation eliminated Audio Focus type (duck vs. transient),
`AudioAttributes`, and app-level ducking-vs-pausing behavior as root causes,
converging on a multichannel (5.1) audio restoration defect somewhere below
the application layer. One gap remained: every test in the original
investigation still involved the bridge calling Android's official
`AudioFocus` API, meaning the interrupted app (Netflix, Nuvio) always
received a genuine focus-loss/gain callback and reacted to it via its own
internal logic. This left open a real, testable alternative explanation:
that the failure was triggered specifically by an app's *reaction* to that
notification, rather than by anything at the shared audio output/hardware
level.

## The Experiment

A new audio focus strategy, `manual_duck_only`, was added to the bridge
specifically to isolate this variable. Under this strategy, the bridge
**never calls `requestAudioFocus()` or `abandonAudioFocus()` at all** - no
other app receives any Audio Focus notification, duck or pause, in either
direction. The bridge instead directly and silently adjusts the shared
system music stream volume around its own playback, entirely outside
Android's Audio Focus mechanism.

If this strategy avoided the dialogue-loss bug, it would confirm the
trigger was Netflix/Nuvio's own reaction to a focus-loss callback. If the
bug still occurred, it would confirm the failure has nothing to do with
Audio Focus notifications at all.

### Test conditions

Reproduced under the exact failing configuration from the original
investigation:

* Netflix
* English audio track set to 5.1
* Internal TV speakers
* TV audio processing (Dolby/enhancement) enabled

### Result

**Dialogue was lost, identically to every prior test.** Background music
and effects continued normally; center-channel dialogue disappeared and
did not return without seeking.

## Conclusion

This result closes the one remaining gap in the original investigation.
With the Audio Focus API removed from the equation entirely - no
notification of any kind sent to Netflix - the failure still occurred.
This rules out app-level reaction to Audio Focus as a contributing factor
under any strategy, and confirms the defect sits at or below the shared
audio mixer/decoder/output layer, consistent with the original report's
conclusion of a TCL firmware or platform-level defect in multichannel
audio restoration after interruption.

There is no remaining Audio Focus strategy, `AudioAttributes` combination,
or app-level behavior change available to the bridge (or to any
third-party Android app) that could work around this defect. The two
workarounds identified in the original investigation remain the only
effective fixes:

* Disable the television's built-in audio processing, or
* Use stereo audio tracks instead of 5.1 where acceptable.

## Disposition

`manual_duck_only` remains available in the bridge as a dormant,
switchable strategy (`GET`/`POST /audio-focus-strategy`) rather than being
removed, since it may have value on different hardware in the future.
It is **not** the default: it provides no benefit against this specific
defect, and has a real cost against the apps that were already behaving
correctly (Spotify, YouTube), which rely on receiving a genuine Audio
Focus notification to duck or pause appropriately. The default strategy
remains `system`.

No further app-level investigation into this specific dialogue-loss
symptom is planned. Future investigation, if pursued, would need to target
the TV's firmware/audio pipeline directly (vendor logs, HDMI/ARC-level
signal analysis, or comparison against non-TCL Android TV hardware) rather
than anything reachable from application code.
