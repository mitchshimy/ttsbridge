package dev.local.ttsbridge.core;

import org.json.JSONException;
import org.json.JSONObject;

/**
 * A registered TTS engine. "device" always exists implicitly and never
 * needs registering - it's the guaranteed last resort in every fallback
 * chain. Everything else is a self-hosted HTTP TTS server (e.g. Piper)
 * that this app can call directly, without HA in the loop.
 */
public final class EngineConfig {

    public static final String TYPE_DEVICE = "device";
    public static final String TYPE_REMOTE_HTTP = "remote_http";

    public final String id;
    public final String type;
    public final String baseUrl;      // remote_http only
    public final boolean jsonRequest; // remote_http only: true = POST {"text":...}, false = raw text body

    public EngineConfig(String id, String type, String baseUrl, boolean jsonRequest) {
        this.id = id;
        this.type = type;
        this.baseUrl = baseUrl;
        this.jsonRequest = jsonRequest;
    }

    public static EngineConfig device() {
        return new EngineConfig("device", TYPE_DEVICE, null, false);
    }

    public JSONObject toJson() {
        JSONObject o = new JSONObject();
        try {
            o.put("id", id);
            o.put("type", type);
            o.put("baseUrl", baseUrl == null ? JSONObject.NULL : baseUrl);
            o.put("jsonRequest", jsonRequest);
        } catch (JSONException ignored) {
        }
        return o;
    }

    public static EngineConfig fromJson(JSONObject o) {
        String id = o.optString("id", null);
        if (id == null || id.trim().isEmpty()) {
            throw new IllegalArgumentException("engine id is required");
        }
        if ("device".equalsIgnoreCase(id)) {
            throw new IllegalArgumentException("'device' is reserved for the built-in on-device engine");
        }
        if (id.toLowerCase().startsWith("tts.")) {
            // The HA custom_component's notify.py treats any engine value
            // starting with "tts." as a Home Assistant TTS entity id to
            // resolve via media_source, not a bridge-native engine - a
            // registration here with that prefix would never actually be
            // reachable, silently misrouted on the HA side instead.
            throw new IllegalArgumentException(
                    "engine ids starting with 'tts.' are reserved for Home Assistant TTS entities");
        }
        String type = o.optString("type", TYPE_REMOTE_HTTP);
        if (!TYPE_REMOTE_HTTP.equals(type)) {
            throw new IllegalArgumentException("unsupported engine type: " + type);
        }
        String baseUrl = o.optString("baseUrl", null);
        if (baseUrl == null || baseUrl.trim().isEmpty()) {
            throw new IllegalArgumentException("baseUrl is required for remote_http engines");
        }
        boolean jsonRequest = o.optBoolean("jsonRequest", true);
        return new EngineConfig(id.trim(), type, baseUrl.trim(), jsonRequest);
    }
}
