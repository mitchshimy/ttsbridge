package dev.local.ttsbridge.core;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;

/**
 * Persisted set of registered TTS engines plus the default fallback order.
 * "device" is always implicitly available and is always appended as the
 * final entry of every resolved chain, so an announcement is never
 * silently dropped just because every remote engine happened to be down.
 */
public class EngineRegistry {

    private static final String TAG = "TtsBridge/Engines";
    private static final String PREFS_NAME = "ttsbridge_prefs";
    private static final String PREF_ENGINES_JSON = "engines_registry";

    private final SharedPreferences prefs;
    private final Map<String, EngineConfig> engines = new LinkedHashMap<>();
    private final List<String> defaultChain = new ArrayList<>();

    public EngineRegistry(Context appContext) {
        prefs = appContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        load();
    }

    public synchronized void upsert(EngineConfig cfg) {
        engines.put(cfg.id, cfg);
        save();
    }

    public synchronized boolean remove(String id) {
        boolean removed = engines.remove(id) != null;
        defaultChain.remove(id);
        if (removed) save();
        return removed;
    }

    public synchronized List<EngineConfig> getAll() {
        return new ArrayList<>(engines.values());
    }

    public synchronized void setDefaultChain(List<String> ids) {
        defaultChain.clear();
        defaultChain.addAll(ids);
        save();
    }

    public synchronized List<String> getDefaultChain() {
        return new ArrayList<>(defaultChain);
    }

    /**
     * Resolves the ordered list of engines to try for one announcement:
     * its explicit per-request override first (if any and if it's a known
     * engine), then the registered default chain, then "device" always
     * last as the guaranteed fallback. Unknown/unregistered ids are
     * skipped rather than failing the whole resolution.
     */
    public synchronized List<EngineConfig> resolveChain(String explicitEngineId) {
        LinkedHashSet<String> order = new LinkedHashSet<>();
        if (explicitEngineId != null && !explicitEngineId.trim().isEmpty()) {
            order.add(explicitEngineId.trim());
        }
        order.addAll(defaultChain);
        order.add("device");

        List<EngineConfig> result = new ArrayList<>();
        for (String id : order) {
            EngineConfig cfg = "device".equals(id) ? EngineConfig.device() : engines.get(id);
            if (cfg != null) {
                result.add(cfg);
            } else {
                Log.w(TAG, "resolveChain: skipping unknown engine id '" + id + "'");
            }
        }
        return result;
    }

    // ---- persistence: one JSON blob, matching the pattern used for the webhook URL ----

    private synchronized void save() {
        try {
            JSONObject root = new JSONObject();
            JSONObject enginesJson = new JSONObject();
            for (EngineConfig cfg : engines.values()) {
                enginesJson.put(cfg.id, cfg.toJson());
            }
            root.put("engines", enginesJson);
            root.put("defaultChain", new JSONArray(defaultChain));
            prefs.edit().putString(PREF_ENGINES_JSON, root.toString()).apply();
        } catch (JSONException e) {
            Log.e(TAG, "Failed to persist engine registry", e);
        }
    }

    private synchronized void load() {
        String raw = prefs.getString(PREF_ENGINES_JSON, null);
        if (raw == null) return;
        try {
            JSONObject root = new JSONObject(raw);
            JSONObject enginesJson = root.optJSONObject("engines");
            if (enginesJson != null) {
                java.util.Iterator<String> keys = enginesJson.keys();
                while (keys.hasNext()) {
                    String id = keys.next();
                    try {
                        JSONObject cfgJson = enginesJson.getJSONObject(id);
                        // reuse fromJson's validation, but id comes from the map key here
                        cfgJson.put("id", id);
                        engines.put(id, EngineConfig.fromJson(cfgJson));
                    } catch (Exception e) {
                        Log.w(TAG, "Skipping corrupt engine entry '" + id + "': " + e.getMessage());
                    }
                }
            }
            JSONArray chainJson = root.optJSONArray("defaultChain");
            if (chainJson != null) {
                for (int i = 0; i < chainJson.length(); i++) {
                    defaultChain.add(chainJson.optString(i));
                }
            }
        } catch (JSONException e) {
            Log.e(TAG, "Failed to load engine registry, starting empty", e);
        }
    }
}
