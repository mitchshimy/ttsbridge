package dev.local.ttsbridge.core;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Persistent, LRU-bounded cache of previously-played audio clips.
 *
 * Deliberately does NOT compute its own key from text/voice/engine/etc. The
 * dominant announcement path (HA resolving a message through a registered
 * tts.* entity) never gives Android that metadata, and HA's own tts_proxy
 * issues a fresh token per resolve even on a cache hit, so `url` itself
 * isn't stable either. Instead the caller (currently: UrlAudioProvider,
 * driven by Announcement.cacheKey) supplies an opaque key it already knows
 * is stable for "this is the same content as last time" - computed
 * HA-side in notify.py from message+engine, where that information
 * actually lives.
 *
 * Single-writer assumption: AnnouncementEngine's worker loop plays one
 * announcement at a time (see AnnouncementEngine's class doc), so there's
 * no concurrent-write race on the same key to worry about here. If that
 * ever changes (e.g. a second concurrent playback path is added), this
 * class will need an in-flight-key guard added.
 */
public class SpeechCache {

    private static final String TAG = "TtsBridge/SpeechCache";
    private static final String PREFS_NAME = "ttsbridge_prefs";
    private static final String PREF_INDEX_JSON = "speech_cache_index";
    private static final String CACHE_DIR_NAME = "tts_cache";

    private static final int DEFAULT_MAX_ENTRIES = 500;
    private static final long DEFAULT_MAX_BYTES = 500L * 1024 * 1024; // 500 MB

    private final File cacheDir;
    private final SharedPreferences prefs;
    private final int maxEntries;
    private final long maxBytes;

    /** key -> entry. LinkedHashMap only for stable iteration order when logging; eviction re-sorts by lastUsedAt explicitly rather than relying on insertion order. */
    private final Map<String, Entry> index = new LinkedHashMap<>();

    private static final class Entry {
        final String fileName;
        long sizeBytes;
        long lastUsedAt;

        Entry(String fileName, long sizeBytes, long lastUsedAt) {
            this.fileName = fileName;
            this.sizeBytes = sizeBytes;
            this.lastUsedAt = lastUsedAt;
        }
    }

    public SpeechCache(Context appContext) {
        this(appContext, DEFAULT_MAX_ENTRIES, DEFAULT_MAX_BYTES);
    }

    public SpeechCache(Context appContext, int maxEntries, long maxBytes) {
        // getFilesDir(), NOT getCacheDir() - Android may delete getCacheDir()
        // at any time under storage pressure, defeating the whole point.
        this.cacheDir = new File(appContext.getFilesDir(), CACHE_DIR_NAME);
        //noinspection ResultOfMethodCallIgnored
        cacheDir.mkdirs();
        this.prefs = appContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        this.maxEntries = maxEntries;
        this.maxBytes = maxBytes;
        loadIndex();
        pruneOrphansAndMissing();
    }

    /** Returns the cached file for `key`, updating its last-used time, or null on a miss. */
    public synchronized File get(String key) {
        String safeKey = safeKey(key);
        Entry e = index.get(safeKey);
        if (e == null) return null;
        File f = new File(cacheDir, e.fileName);
        if (!f.exists()) {
            // Index says we have it, disk disagrees (cleared data, manual
            // deletion, etc) - drop the stale entry rather than lying about a hit.
            index.remove(safeKey);
            saveIndex();
            return null;
        }
        e.lastUsedAt = System.currentTimeMillis();
        saveIndex();
        return f;
    }

    /**
     * Copies `source`'s bytes into the cache under `key` and returns the
     * cache's own copy (NOT `source`) - callers should play the returned
     * file, not the original, so repeated hits and this first write end up
     * reading the exact same path.
     */
    public synchronized File put(String key, File source) throws IOException {
        String safeKey = safeKey(key);
        String fileName = safeKey + ".audio";
        File dest = new File(cacheDir, fileName);
        File tmp = new File(cacheDir, fileName + ".tmp");

        try (InputStream in = new java.io.FileInputStream(source);
             FileOutputStream out = new FileOutputStream(tmp)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
        }
        // Atomic rename: a crash/kill mid-copy leaves only a stray .tmp file,
        // never a truncated file masquerading as a valid cache hit.
        if (!tmp.renameTo(dest)) {
            //noinspection ResultOfMethodCallIgnored
            tmp.delete();
            throw new IOException("Failed to finalize cache entry for key=" + key);
        }

        index.put(safeKey, new Entry(fileName, dest.length(), System.currentTimeMillis()));
        evictIfNeeded();
        saveIndex();
        return dest;
    }

    private void evictIfNeeded() {
        long totalBytes = 0;
        for (Entry e : index.values()) totalBytes += e.sizeBytes;

        if (index.size() <= maxEntries && totalBytes <= maxBytes) return;

        List<Map.Entry<String, Entry>> byAge = new ArrayList<>(index.entrySet());
        byAge.sort(Comparator.comparingLong(en -> en.getValue().lastUsedAt));

        int i = 0;
        while (i < byAge.size() && (index.size() > maxEntries || totalBytes > maxBytes)) {
            Map.Entry<String, Entry> oldest = byAge.get(i);
            File f = new File(cacheDir, oldest.getValue().fileName);
            //noinspection ResultOfMethodCallIgnored
            f.delete();
            totalBytes -= oldest.getValue().sizeBytes;
            index.remove(oldest.getKey());
            i++;
        }
    }

    /** Reconciles the index against what's actually on disk once at startup - covers manual file deletion, app data partially cleared, etc. */
    private void pruneOrphansAndMissing() {
        boolean changed = false;
        List<String> staleKeys = new ArrayList<>();
        for (Map.Entry<String, Entry> en : index.entrySet()) {
            if (!new File(cacheDir, en.getValue().fileName).exists()) {
                staleKeys.add(en.getKey());
            }
        }
        for (String k : staleKeys) {
            index.remove(k);
            changed = true;
        }
        if (changed) saveIndex();
    }

    private static String safeKey(String key) {
        // Callers are expected to pass an already-hashed opaque key (see
        // notify.py), but re-hash defensively so this class is safe against
        // any future caller passing raw text, weird characters, or a key
        // long enough to blow past filesystem filename limits.
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(key.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(digest.length * 2);
            for (byte b : digest) sb.append(String.format("%02x", b));
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            // SHA-256 is guaranteed present on Android; this is unreachable in practice.
            throw new RuntimeException(e);
        }
    }

    // ---- persistence: one JSON blob, matching the pattern EngineRegistry already uses ----

    private synchronized void saveIndex() {
        try {
            JSONObject root = new JSONObject();
            for (Map.Entry<String, Entry> en : index.entrySet()) {
                JSONObject o = new JSONObject();
                o.put("fileName", en.getValue().fileName);
                o.put("sizeBytes", en.getValue().sizeBytes);
                o.put("lastUsedAt", en.getValue().lastUsedAt);
                root.put(en.getKey(), o);
            }
            prefs.edit().putString(PREF_INDEX_JSON, root.toString()).apply();
        } catch (JSONException e) {
            Log.e(TAG, "Failed to persist speech cache index", e);
        }
    }

    private synchronized void loadIndex() {
        String raw = prefs.getString(PREF_INDEX_JSON, null);
        if (raw == null) return;
        try {
            JSONObject root = new JSONObject(raw);
            java.util.Iterator<String> keys = root.keys();
            while (keys.hasNext()) {
                String k = keys.next();
                try {
                    JSONObject o = root.getJSONObject(k);
                    index.put(k, new Entry(o.getString("fileName"), o.getLong("sizeBytes"), o.getLong("lastUsedAt")));
                } catch (JSONException e) {
                    Log.w(TAG, "Skipping corrupt speech cache entry '" + k + "': " + e.getMessage());
                }
            }
        } catch (JSONException e) {
            Log.e(TAG, "Failed to load speech cache index, starting empty", e);
        }
    }
}
