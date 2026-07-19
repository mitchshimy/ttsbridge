package dev.local.ttsbridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import dev.local.ttsbridge.core.Announcement;
import dev.local.ttsbridge.core.AnnouncementEngine;
import dev.local.ttsbridge.core.AnnouncementQueue;
import dev.local.ttsbridge.core.EngineState;
import dev.local.ttsbridge.core.Priority;
import dev.local.ttsbridge.http.ControlHttpServer;

/**
 * A persistent foreground service that owns:
 *  - the announcement queue + worker loop
 *  - the playback engine (provider selection, audio focus)
 *  - a small local HTTP API for Home Assistant to talk to
 *  - a MediaSession + notification so the OS/Bluetooth/ARC treat it sanely
 *
 * Legacy one-shot intents (--es text / --es url) are still accepted and are
 * translated into a NORMAL-priority Announcement, so existing ADB-based HA
 * automations keep working while you migrate to the HTTP API.
 */
public class AnnouncementService extends Service {

    private static final String TAG = "TtsBridge/Service";
    private static final String CHANNEL_ID = "ttsbridge_channel";
    private static final int NOTIF_ID = 1;
    public static final int HTTP_PORT = 8098;
    private static final String PREFS_NAME = "ttsbridge_prefs";
    private static final String PREF_WEBHOOK_URL = "webhook_url";

    // How long a queued item of each category is still worth playing once it
    // reaches the front of the queue. 0 = never goes stale.
    private static final long MOTION_STALE_MS = 20_000;
    private static final long GENERAL_STALE_MS = 0;

    private AnnouncementEngine engine;
    private AnnouncementQueue queue;
    private ControlHttpServer httpServer;
    private MediaSession mediaSession;
    private PowerManager.WakeLock wakeLock;
    private ExecutorService callbackExecutor;
    private java.util.concurrent.ScheduledExecutorService heartbeatExecutor;
    private Thread workerThread;
    private volatile boolean running = false;

    private volatile Announcement current = null;
    private volatile EngineState state = EngineState.IDLE;

    // Set via POST /webhook and persisted, so HA gets pushed state changes
    // instead of having to poll /status. Persisted to SharedPreferences so a
    // service restart (crash, manual stop/start) doesn't lose the
    // registration - HA's power-on automation also re-registers it as a
    // belt-and-suspenders measure for the fresh-install/cleared-data case.
    private volatile String webhookUrl;

    @Override
    public void onCreate() {
        super.onCreate();
        // Must happen before ANYTHING else that could take more than a
        // moment (e.g. binding the TextToSpeech engine below), or Android
        // kills the process with ForegroundServiceDidNotStartInTimeException.
        startForegroundWithNotification();

        engine = new AnnouncementEngine(getApplicationContext());
        queue = new AnnouncementQueue(category ->
                "motion".equalsIgnoreCase(category) ? MOTION_STALE_MS : GENERAL_STALE_MS);
        callbackExecutor = Executors.newSingleThreadExecutor();
        webhookUrl = getSharedPreferences(PREFS_NAME, MODE_PRIVATE).getString(PREF_WEBHOOK_URL, null);

        setupMediaSession();
        startWorker();
        startHttpServer();
        startHeartbeat();
    }

    /**
     * Pushes a webhook update every 60s regardless of activity. Without this,
     * the only pushes are on SPEAKING/IDLE transitions - during a quiet
     * overnight period with zero announcements, a perfectly healthy TV would
     * look identical (from HA's side) to one that died hours ago, since
     * nothing would have pushed in either case. This keeps last_updated
     * moving on the HA sensor whenever we're actually alive, so a staleness
     * check on the HA side can tell the difference.
     */
    private void startHeartbeat() {
        heartbeatExecutor = Executors.newSingleThreadScheduledExecutor();
        heartbeatExecutor.scheduleWithFixedDelay(
                this::pushWebhookUpdate, 60, 60, java.util.concurrent.TimeUnit.SECONDS);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopCurrentAndMaybeClear(false);
            return START_STICKY;
        }
        if (intent != null) {
            String url = intent.getStringExtra("url");
            String text = intent.getStringExtra("text");
            boolean warmOnly = intent.getBooleanExtra("warmOnly", false);
            if ((url != null && !url.trim().isEmpty()) || (text != null && !text.trim().isEmpty())) {
                Announcement.Builder b = Announcement.builder();
                if (url != null && !url.trim().isEmpty()) b.url(url); else b.text(text);
                String priority = intent.getStringExtra("priority");
                b.priority(Priority.fromString(priority, Priority.NORMAL));
                enqueue(b.build());
            } else if (!warmOnly) {
                Log.d(TAG, "onStartCommand: no text/url, service just staying alive");
            }
        }
        return START_STICKY; // this is now a persistent service, not one-shot
    }

    // ---- Queueing + interruption rules ----

    /** @return status string: "queued" | "duplicate" | "playing" */
    public String enqueue(Announcement a) {
        Announcement current = this.current;
        boolean shouldInterruptCurrent = current != null
                && a.priority.rank < current.priority.rank
                && (current.interruptible || a.priority == Priority.EMERGENCY);

        Announcement accepted = queue.offer(a);
        if (accepted == null) {
            return "duplicate";
        }
        if (shouldInterruptCurrent) {
            engine.interruptCurrent();
        }
        return "queued";
    }

    public void stopCurrentAndMaybeClear(boolean clearQueue) {
        engine.interruptCurrent();
        if (clearQueue) queue.clear();
    }

    private void startWorker() {
        running = true;
        workerThread = new Thread(() -> {
            while (running) {
                try {
                    Announcement next = queue.takeNextFresh();
                    current = next;
                    state = EngineState.SPEAKING;
                    acquireWakeLock();
                    updateNotification();
                    updateMediaSession();
                    pushWebhookUpdate();

                    engine.play(next, new AnnouncementEngine.EventListener() {
                        @Override
                        public void onAnnouncementStarted(Announcement a) {
                            updateNotification();
                            updateMediaSession();
                        }

                        @Override
                        public void onAnnouncementFinished(Announcement a, String errorOrNull) {
                            notifyCallback(a, errorOrNull);
                        }
                    });

                    current = null;
                    state = queue.size() > 0 ? EngineState.BUSY : EngineState.IDLE;
                    releaseWakeLock();
                    updateNotification();
                    updateMediaSession();
                    pushWebhookUpdate();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                } catch (Exception e) {
                    Log.e(TAG, "Worker loop error", e);
                }
            }
        }, "AnnouncementWorker");
        workerThread.setDaemon(true);
        workerThread.start();
    }

    private void notifyCallback(Announcement a, String errorOrNull) {
        if (a.callbackUrl == null || a.callbackUrl.trim().isEmpty()) return;
        callbackExecutor.execute(() -> {
            try {
                URL url = new URL(a.callbackUrl);
                HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                conn.setRequestMethod("POST");
                conn.setDoOutput(true);
                conn.setConnectTimeout(5000);
                conn.setReadTimeout(5000);
                conn.setRequestProperty("Content-Type", "application/json");
                JSONObject payload = a.toJson();
                payload.put("success", errorOrNull == null);
                if (errorOrNull != null) payload.put("error", errorOrNull);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(payload.toString().getBytes(StandardCharsets.UTF_8));
                }
                conn.getResponseCode(); // drain
                conn.disconnect();
            } catch (Exception e) {
                Log.w(TAG, "Finish callback failed: " + e.getMessage());
            }
        });
    }

    /** Shared by GET /status and the webhook pusher, so they can never drift apart. */
    private JSONObject buildStatusJson() throws JSONException {
        JSONObject resp = new JSONObject();
        resp.put("state", state.name());
        resp.put("current", current == null ? JSONObject.NULL : current.toJson());
        resp.put("queueSize", queue.size());
        resp.put("volume", engine.focus().getVolumePercent());
        return resp;
    }

    /**
     * Pushes the current status to the registered webhook (if any), so HA
     * gets state changes instantly instead of having to poll /status on a
     * timer and mostly missing short-lived SPEAKING windows.
     */
    private void pushWebhookUpdate() {
        String url = webhookUrl;
        if (url == null || url.trim().isEmpty()) return;
        callbackExecutor.execute(() -> {
            try {
                JSONObject payload = buildStatusJson();
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                conn.setRequestMethod("POST");
                conn.setDoOutput(true);
                conn.setConnectTimeout(5000);
                conn.setReadTimeout(5000);
                conn.setRequestProperty("Content-Type", "application/json");
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(payload.toString().getBytes(StandardCharsets.UTF_8));
                }
                conn.getResponseCode(); // drain
                conn.disconnect();
            } catch (Exception e) {
                Log.w(TAG, "Webhook push failed: " + e.getMessage());
            }
        });
    }

    // ---- HTTP API ----

    private void startHttpServer() {
        httpServer = new ControlHttpServer(HTTP_PORT);

        httpServer.route("POST", "/announce", (body, query) -> {
            try {
                Announcement a = Announcement.fromJson(body);
                String result = enqueue(a);
                JSONObject resp = new JSONObject()
                        .put("id", a.id)
                        .put("status", result)
                        .put("queueSize", queue.size());
                return new ControlHttpServer.Response("duplicate".equals(result) ? 200 : 202, resp);
            } catch (IllegalArgumentException e) {
                return new ControlHttpServer.Response(400, ControlHttpServer.errorJson(e.getMessage()));
            }
        });

        httpServer.route("POST", "/stop", (body, query) -> {
            boolean clear = "true".equalsIgnoreCase(query.get("clear")) || body.optBoolean("clear", false);
            stopCurrentAndMaybeClear(clear);
            return new ControlHttpServer.Response(200, new JSONObject().put("stopped", true).put("clearedQueue", clear));
        });

        httpServer.route("GET", "/status", (body, query) -> new ControlHttpServer.Response(200, buildStatusJson()));

        httpServer.route("GET", "/queue", (body, query) -> {
            List<Announcement> snapshot = queue.snapshot();
            JSONArray arr = new JSONArray();
            for (Announcement a : snapshot) arr.put(a.toJson());
            JSONObject resp = new JSONObject().put("queue", arr).put("size", snapshot.size());
            return new ControlHttpServer.Response(200, resp);
        });

        httpServer.route("POST", "/volume", (body, query) -> {
            int level = body.optInt("level", -1);
            if (level < 0 || level > 100) {
                return new ControlHttpServer.Response(400, ControlHttpServer.errorJson("level must be 0-100"));
            }
            engine.focus().setVolumePercent(level);
            return new ControlHttpServer.Response(200, new JSONObject().put("volume", level));
        });

        httpServer.route("GET", "/webhook", (body, query) ->
                new ControlHttpServer.Response(200, new JSONObject()
                        .put("url", webhookUrl == null ? JSONObject.NULL : webhookUrl)));

        httpServer.route("POST", "/webhook", (body, query) -> {
            String url = body.optString("url", null);
            webhookUrl = (url == null || url.trim().isEmpty()) ? null : url.trim();
            getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit()
                    .putString(PREF_WEBHOOK_URL, webhookUrl)
                    .apply();
            if (webhookUrl != null) {
                pushWebhookUpdate(); // send an immediate snapshot so HA doesn't wait for the next transition
            }
            return new ControlHttpServer.Response(200, new JSONObject()
                    .put("url", webhookUrl == null ? JSONObject.NULL : webhookUrl)
                    .put("registered", webhookUrl != null));
        });

        httpServer.start();
    }

    // ---- Notification / MediaSession / lifecycle plumbing ----

    private void startForegroundWithNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "Announcement Bridge", NotificationManager.IMPORTANCE_MIN);
            channel.setShowBadge(false);
            nm.createNotificationChannel(channel);
        }
        startForeground(NOTIF_ID, buildNotification());
    }

    private Notification buildNotification() {
        Notification.Builder builder = (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);

        String title = state == EngineState.SPEAKING && current != null
                ? "Speaking" + (current.text != null ? ": " + truncate(current.text) : " announcement")
                : "Announcement Bridge ready";

        builder.setContentTitle(title)
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setPriority(Notification.PRIORITY_MIN)
                .setOngoing(true);

        Intent stopIntent = new Intent(this, AnnouncementService.class).setAction(ACTION_STOP);
        PendingIntent stopPending = PendingIntent.getService(this, 0, stopIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0));
        builder.addAction(new Notification.Action.Builder(
                android.R.drawable.ic_media_pause, "Stop", stopPending).build());

        return builder.build();
    }

    private static final String ACTION_STOP = "dev.local.ttsbridge.action.STOP";

    private void updateNotification() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.notify(NOTIF_ID, buildNotification());
    }

    private void setupMediaSession() {
        mediaSession = new MediaSession(this, "TtsBridgeSession");
        mediaSession.setCallback(new MediaSession.Callback() {
            @Override
            public void onStop() {
                stopCurrentAndMaybeClear(false);
            }
        });
        mediaSession.setActive(true);
        updateMediaSession();
    }

    private void updateMediaSession() {
        if (mediaSession == null) return;
        int playbackState = state == EngineState.SPEAKING ? PlaybackState.STATE_PLAYING : PlaybackState.STATE_STOPPED;
        PlaybackState ps = new PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_STOP)
                .setState(playbackState, 0, 1f)
                .build();
        mediaSession.setPlaybackState(ps);
    }

    private void acquireWakeLock() {
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            //noinspection deprecation
            wakeLock = pm.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK, "ttsbridge:speak");
        }
        if (!wakeLock.isHeld()) {
            wakeLock.acquire(30_000);
        }
    }

    private void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
    }

    private static String truncate(String s) {
        return s.length() > 40 ? s.substring(0, 40) + "…" : s;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (workerThread != null) workerThread.interrupt();
        if (httpServer != null) httpServer.stop();
        if (mediaSession != null) mediaSession.release();
        if (engine != null) engine.release();
        if (callbackExecutor != null) callbackExecutor.shutdownNow();
        if (heartbeatExecutor != null) heartbeatExecutor.shutdownNow();
        releaseWakeLock();
        super.onDestroy();
    }
}
