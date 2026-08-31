# Investigation: Homeway Cache Poisoning, and the Signature-Detection Fix

## Summary

A `tts_announce` automation-level bug (a Home Assistant `response_variable`
unwrap mistake — entity-targeted services return `{entity_id: {...}}`, not
the payload directly) meant a cache-check probe was silently reporting
"not cached" on every single call, regardless of whether a real cache hit
existed. That bug alone was fixable at the automation layer. But
diagnosing it surfaced a second, more serious problem living in this
component: **when Homeway is over quota, it doesn't error — it resolves
"successfully" to a canned degraded clip**, indistinguishable from real
synthesis to any code that isn't listening to the actual audio. Because
`chime.async_render`'s cache write was (and had to be) unconditional on a
successful resolve, this meant a single quota-exhausted moment could
permanently poison a cache entry — every future request for that exact
message would keep serving the same wrong clip forever, quota available
again or not.

The fix adds real content-based detection: every fresh resolve is
hashed (post-decode, so encoder differences don't matter) and checked
against a small, extensible registry of known-degraded-response
signatures (`known_bad_signatures.json`) *before* it's ever allowed to
be written to the durable cache or played. A match raises
`PoisonedSynthesis` — refused, logged, never cached — instead of
silently succeeding with bad audio.

---

## How the poisoning was actually found

This started as an unrelated bug report: a Homeway-cached announcement
played correctly with an `input_boolean` toggle on, but fell straight to
Google with the toggle off — looked like the cache-check wasn't running
at all when the toggle was disabled.

Tracing it (via HA script traces showing the actual `response_variable`
shape returned by `ttsbridge.check_cache`) found the real bug: the probe
*was* running, correctly, every time — but the automation reading its
result was checking `raw_cache_check.get('cached', false)` on the
**outer** entity-keyed dict, which has no `cached` key at that level.
`.get()`'s default silently returned `false` on every call. The toggle
only *looked* relevant because a cache miss with the toggle on falls
back to re-synthesizing through Homeway again (same voice, invisible),
while a miss with the toggle off falls back to Google (audibly
different, impossible to miss).

Fixing that unwrap bug (so the cache probe actually worked) should have
been the end of it. Instead it exposed the real problem: a previously
good, already-cached announcement started playing in a different voice.
Manually decoding the cached file's audio and cross-referencing it
against Homeway's raw resolve (captured straight from HA's own
`config/tts` before it gets cleared) confirmed it: **the cached file's
audio didn't match what the message should have sounded like at all —
it was a canned quota/limit clip, tagged with a rotating, seemingly
meaningless locale code (`ja-jp`, `sk-sk`, `af-za` were all captured
across independent triggers, on unrelated messages, all decoding to the
byte-identical PCM signature)**. Homeway wasn't failing loudly when
over quota — it was returning this same static clip, dressed up as a
normal successful resolve, which `chime.async_render`'s cache write had
no way to distinguish from the real thing.

---

## Root cause

`chime.async_render`'s cache write (`os.replace(tmp_out, out_path)`) is
unconditional on the ffmpeg concat step succeeding — by design, since it
has no way to know "this audio is correct" versus "this audio is
correct-shaped but wrong." A degraded resolve is not an error from HA's
perspective: `media_source.async_resolve_media()` returns a valid URL,
the download succeeds, ffmpeg concat succeeds. Nothing in that pipeline
has ever had a reason to fail. The bug this session started with
(cache checks always reporting "miss") compounded this: because the
Homeway cache probe never actually worked, *every* announcement was
re-resolving live rather than reusing a cached hit — meaning far more
opportunities than intended for a quota-exhausted moment to land on and
silently overwrite an already-good cache entry.

---

## The fix

**Detection** (`chime.py`):

- `_async_pcm_signature()` — decodes a clip to raw PCM (`ffmpeg -f
  s16le -ar 44100 -ac 1`) and MD5-hashes the result. Decoding first,
  rather than hashing the compressed bytes directly, means encoder/
  container differences don't produce false negatives for genuinely
  identical audio.
- `known_bad_signatures.json` — the actual registry, `{signature_hash:
  human_label}`. Loaded once via `functools.lru_cache` (cheap to check
  on every genuine cache miss; the file itself rarely changes and a
  restart is expected to pick up edits).
- `PoisonedSynthesis` — a distinct exception type, deliberately **not**
  swallowed by the generic "chime failed, fall back to playing the raw
  url without a chime" handling that already existed for unrelated
  failures (network hiccups, a missing chime asset). A degraded
  response must not play as if it were correct, and must not be
  cached — either of those would be strictly worse than the pre-fix
  behavior for a message that hasn't been resolved yet.

**Coverage** (`notify.py`): the check had to end up running on every
resolve path, not just the chime-applying one. Two earlier, narrower
placements each missed a real case:

1. Checking only inside `async_render` missed the no-chime path
   entirely — `chime: none` (or any value pointing at a chime asset
   that doesn't exist) falls through to a pre-existing "chime couldn't
   be applied, playing without it" branch that never calls
   `async_render` at all.
2. A first attempt at closing that gap added a check specifically to
   the "`chime_id` is falsy" branch — but `chime: "none"` is a
   *non-empty string*, so it's truthy, and took the *other* uncovered
   branch (file not found) instead.

The actual fix: the check now runs **unconditionally**, immediately
after the resolve and before any chime-related branching happens at
all — independent of which branch execution falls into next, so no
future branch restructuring can silently reopen this gap.

**Single download, not two:** `async_check_tts_url()` downloads and
checks once, then hands the temp file to `async_render()` (via its new
optional `speech_path` parameter) when a chime does apply — which takes
over ownership of cleanup — rather than each path downloading and
hashing the same URL independently. `async_render()` still has its own
internal check as a fallback when called with no pre-fetched
`speech_path`, so any other caller that doesn't go through the shared
check first stays covered too.

---

## Verifying it's actually the same clip, not several

Before trusting a single registry entry to cover several distinct
Homeway failures, every capture was decoded to raw PCM and MD5-hashed
(never compared as raw compressed bytes, which would have produced
false negatives from encoder-level differences alone):

| Capture | Locale tag | Trigger | PCM hash |
|---|---|---|---|
| 1 | `ja-jp` | (chime unset) | `765f2f69cde8b0422c2c8f1ab7fa07a1` |
| 2 | `ja-jp` | `chime: none` | `765f2f69cde8b0422c2c8f1ab7fa07a1` |
| 3 | `ja-jp` | (chime unset) | `765f2f69cde8b0422c2c8f1ab7fa07a1` |
| 4 | `ja-jp` | (chime unset) | `765f2f69cde8b0422c2c8f1ab7fa07a1` |
| 5 | `sk-sk` | different TTS category | `765f2f69cde8b0422c2c8f1ab7fa07a1` |
| 6 | `af-za` | `chime: none` (missing-chime path) | `765f2f69cde8b0422c2c8f1ab7fa07a1` |
| 7 | `af-za` | `chime: none` (missing-chime path) | `765f2f69cde8b0422c2c8f1ab7fa07a1` |

Seven independent captures, three different locale tags, multiple
different trigger categories — one signature. That locale tag rotates
per-capture and carries no real information; it was a red herring
early on, not a meaningful classifier. Content hash is the right and
only reliable thing to key detection on here.

---

## How to extend it

The registry is meant to grow, not stay fixed at one entry. If Homeway
ever returns a genuinely *different* degraded clip:

1. Reproduce it, then grab the raw file straight from HA's `config/tts`
   directory before HA clears it on its own.
2. `ffmpeg -v error -i newfile.mp3 -f s16le -ar 44100 -ac 1 - | md5sum`
3. Add the hash to `known_bad_signatures.json` with a short label.
4. Restart HA (the registry is `lru_cache`d at load, not re-read
   per-call, so an edit alone won't take effect until reload).

No code changes needed — the check already loops over however many
entries the registry holds.

---

## Known limitation

This is inherently **reactive**, not predictive: it only catches a
degraded response it has already seen at least once. A genuinely new
failure mode — a different canned clip Homeway hasn't been observed
returning yet — plays and potentially gets cached exactly once before
anyone notices and adds its signature. Catching a *first-ever*
occurrence of an unknown variant would need a structurally different
approach (e.g. some heuristic on resolve timing or expected
language/voice profile, rather than pure content matching) — a
meaningfully bigger undertaking than this fix, and not something built
here. Given how consistently the same single clip has reproduced across
every trigger tested (seven captures, one signature), a low-effort
reactive registry was judged the right scope for the actual failure
rate observed.

## Test results

- Poison check confirmed firing correctly against a genuine live
  Homeway quota-limit hit (`_LOGGER.error` line observed in
  `ha core logs`, matching the registered signature exactly) — refused
  to cache, no audio played on that attempt.
- Confirmed via a purpose-built PCM-content scan that the *existing*
  cache (post-cleanup, 81 manually-reviewed entries) contained zero
  matches against the registered signature — the fix's coverage gaps
  (chime-unset and chime-missing paths) were found and closed via
  direct reproduction, not left as theoretical.
- Confirmed the DRY refactor (single download+check, handed off to
  `async_render` rather than duplicated) preserves detection - retested
  after the refactor against the same `chime: none` reproduction that
  found the original coverage gap.
