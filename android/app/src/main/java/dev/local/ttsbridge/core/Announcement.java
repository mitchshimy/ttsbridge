package dev.local.ttsbridge.core;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.UUID;

/**
 * A single unit of work for the AnnouncementEngine. Either `text` (device TTS)
 * or `url` (pre-rendered audio, e.g. from HA's tts.piper / tts.google_translate
 * / homeway_sage) should be set; if both are set, `url` wins.
 */
public final class Announcement {

    public final String id;
    public final String text;
    public final String url;
    public final Priority priority;
    public final long timeoutMs;      // hard ceiling on playback time
    public final boolean interruptible; // can a HIGHER-priority item cut this off?
    public final boolean duck;        // AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK vs ...TRANSIENT
    public final String category;     // e.g. "motion", "doorbell", "general" - used for dedup/staleness rules
    public final long enqueuedAt;
    public final String callbackUrl;  // optional: POSTed to when this finishes/fails
    public final int volume;          // 0-100, -1 = leave as-is
    public final String engine;       // optional: registered engine id to try first (falls through to the default chain, then device, if it fails)
    public final String cacheKey;     // optional: opaque, caller-computed stable key for local disk caching of `url`'s audio content. Needed because HA's tts_proxy issues a fresh token per resolve even for a cache hit on its own side - `url` itself is NOT stable across identical repeated messages, so it can't be used as a cache key directly. Null = no caching for this announcement (always fetch url fresh, today's behavior).

    private Announcement(Builder b) {
        this.id = b.id;
        this.text = b.text;
        this.url = b.url;
        this.priority = b.priority;
        this.timeoutMs = b.timeoutMs;
        this.interruptible = b.interruptible;
        this.duck = b.duck;
        this.category = b.category;
        this.enqueuedAt = System.currentTimeMillis();
        this.callbackUrl = b.callbackUrl;
        this.volume = b.volume;
        this.engine = b.engine;
        this.cacheKey = b.cacheKey;
    }

    /** Coarse key used for duplicate suppression: same thing, same category. */
    public String dedupeKey() {
        String payload = (url != null && !url.isEmpty()) ? ("url:" + url) : ("text:" + text);
        return category + "|" + payload;
    }

    public boolean isStale(long maxAgeMs) {
        if (maxAgeMs <= 0) return false;
        return (System.currentTimeMillis() - enqueuedAt) > maxAgeMs;
    }

    public JSONObject toJson() {
        JSONObject o = new JSONObject();
        try {
            o.put("id", id);
            o.put("text", text == null ? JSONObject.NULL : text);
            o.put("url", url == null ? JSONObject.NULL : url);
            o.put("priority", priority.name());
            o.put("category", category);
            o.put("interruptible", interruptible);
            o.put("duck", duck);
            o.put("engine", engine == null ? JSONObject.NULL : engine);
            o.put("cacheKey", cacheKey == null ? JSONObject.NULL : cacheKey);
            o.put("enqueuedAt", enqueuedAt);
            o.put("ageMs", System.currentTimeMillis() - enqueuedAt);
        } catch (JSONException ignored) {
        }
        return o;
    }

    public static Builder builder() {
        return new Builder();
    }

    public static Announcement fromJson(JSONObject body) {
        Builder b = builder();
        b.text = body.optString("text", null);
        b.url = body.optString("url", null);
        b.priority = Priority.fromString(body.optString("priority", null), Priority.NORMAL);
        b.timeoutMs = body.optLong("timeout", 15000);
        b.interruptible = body.optBoolean("interruptible", true);
        b.duck = body.optBoolean("duck", false);
        b.category = body.optString("category", "general");
        b.callbackUrl = body.optString("callback", null);
        b.volume = body.optInt("volume", -1);
        b.engine = body.optString("engine", null);
        b.cacheKey = body.optString("cacheKey", null);
        return b.build();
    }

    public static final class Builder {
        private String id = UUID.randomUUID().toString();
        private String text;
        private String url;
        private Priority priority = Priority.NORMAL;
        private long timeoutMs = 15000;
        private boolean interruptible = true;
        private boolean duck = false;
        private String category = "general";
        private String callbackUrl;
        private int volume = -1;
        private String engine;
        private String cacheKey;

        public Builder text(String v) { this.text = v; return this; }
        public Builder url(String v) { this.url = v; return this; }
        public Builder priority(Priority v) { this.priority = v; return this; }
        public Builder timeoutMs(long v) { this.timeoutMs = v; return this; }
        public Builder interruptible(boolean v) { this.interruptible = v; return this; }
        public Builder duck(boolean v) { this.duck = v; return this; }
        public Builder category(String v) { this.category = v; return this; }
        public Builder callbackUrl(String v) { this.callbackUrl = v; return this; }
        public Builder volume(int v) { this.volume = v; return this; }
        public Builder engine(String v) { this.engine = v; return this; }
        public Builder cacheKey(String v) { this.cacheKey = v; return this; }

        public Announcement build() {
            if ((text == null || text.trim().isEmpty()) && (url == null || url.trim().isEmpty())) {
                throw new IllegalArgumentException("Announcement needs either text or url");
            }
            return new Announcement(this);
        }
    }
}
