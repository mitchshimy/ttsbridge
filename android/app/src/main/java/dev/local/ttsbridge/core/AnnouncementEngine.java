package dev.local.ttsbridge.core;

import android.content.Context;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import dev.local.ttsbridge.providers.AnnouncementProvider;
import dev.local.ttsbridge.providers.DeviceTtsProvider;
import dev.local.ttsbridge.providers.RemoteHttpTtsProvider;
import dev.local.ttsbridge.providers.UrlAudioProvider;

/**
 * Picks the right provider(s) for an announcement and plays it synchronously
 * from the caller's point of view (play() blocks the calling thread until
 * finished/errored/timed out/stopped), which keeps AnnouncementService's
 * worker loop simple: pop from queue, engine.play(), repeat.
 *
 * An announcement with an explicit `url` always goes straight to
 * UrlAudioProvider - the caller already told us exactly what to play, so
 * there's nothing to select. A text-based announcement instead resolves an
 * ordered chain of engines (its own override, then the registered default
 * chain, then "device" always last) via EngineRegistry, and tries them in
 * order until one succeeds - so a Piper outage doesn't mean silence, it
 * means falling back to on-device TTS instead.
 */
public class AnnouncementEngine {

    public interface EventListener {
        void onAnnouncementStarted(Announcement a);
        void onAnnouncementFinished(Announcement a, String errorOrNull);
    }

    private final Context appContext;
    private final DeviceTtsProvider deviceTts;
    private final UrlAudioProvider urlAudio;
    private final EngineRegistry registry;
    private final AudioFocusManager focusManager;
    private final AtomicReference<AnnouncementProvider> activeProvider = new AtomicReference<>();

    public AnnouncementEngine(Context appContext) {
        this.appContext = appContext.getApplicationContext();
        deviceTts = new DeviceTtsProvider(appContext);
        urlAudio = new UrlAudioProvider(appContext);
        registry = new EngineRegistry(appContext);
        focusManager = new AudioFocusManager(appContext);
    }

    public AudioFocusManager focus() {
        return focusManager;
    }

    public EngineRegistry registry() {
        return registry;
    }

    /** Plays one announcement to completion (or interruption), trying fallback engines on genuine failure. */
    public void play(Announcement a, EventListener events) {
        if (a.url != null && !a.url.trim().isEmpty()) {
            // Explicit URL: no engine selection, no fallback - play exactly what was asked.
            Outcome outcome = attempt(urlAudio, a, events);
            events.onAnnouncementFinished(a, outcome.success ? null : outcome.error);
            return;
        }

        List<EngineConfig> chain = registry.resolveChain(a.engine);
        String lastError = "no_engine_available";
        for (EngineConfig cfg : chain) {
            AnnouncementProvider provider = providerFor(cfg);
            if (provider == null) continue;

            Outcome outcome = attempt(provider, a, events);
            if (outcome.interrupted) {
                // Cut off on purpose (e.g. an EMERGENCY pre-empted us) - not
                // an engine failure, so don't burn through the fallback
                // chain trying other engines for no reason.
                events.onAnnouncementFinished(a, "interrupted");
                return;
            }
            if (outcome.success) {
                events.onAnnouncementFinished(a, null);
                return;
            }
            lastError = cfg.id + ": " + outcome.error;
        }
        events.onAnnouncementFinished(a, "all_engines_failed (" + lastError + ")");
    }

    private AnnouncementProvider providerFor(EngineConfig cfg) {
        if (EngineConfig.TYPE_DEVICE.equals(cfg.type)) {
            return deviceTts;
        }
        if (EngineConfig.TYPE_REMOTE_HTTP.equals(cfg.type)) {
            // Fresh instance per attempt - cheap, and sidesteps any risk of
            // stale in-flight state (temp files, MediaPlayer) leaking
            // between unrelated announcements.
            return new RemoteHttpTtsProvider(appContext, cfg.id, cfg.baseUrl, cfg.jsonRequest);
        }
        return null;
    }

    private static final class Outcome {
        final boolean success;
        final boolean interrupted;
        final String error;

        Outcome(boolean success, boolean interrupted, String error) {
            this.success = success;
            this.interrupted = interrupted;
            this.error = error;
        }
    }

    /** Runs one provider to completion/error/timeout/interruption and reports which. */
    private Outcome attempt(AnnouncementProvider provider, Announcement a, EventListener events) {
        activeProvider.set(provider);
        focusManager.request(a.duck);
        if (a.volume >= 0) {
            focusManager.setVolumePercent(a.volume);
        }

        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> errorHolder = new AtomicReference<>();

        provider.play(a, new AnnouncementProvider.Listener() {
            @Override
            public void onStarted(Announcement ann) {
                events.onAnnouncementStarted(ann);
            }

            @Override
            public void onFinished(Announcement ann) {
                done.countDown();
            }

            @Override
            public void onError(Announcement ann, String reason) {
                errorHolder.set(reason);
                done.countDown();
            }
        });

        try {
            boolean finishedInTime = done.await(a.timeoutMs, TimeUnit.MILLISECONDS);
            if (!finishedInTime) {
                provider.stop();
                errorHolder.set("timeout");
            }
        } catch (InterruptedException e) {
            provider.stop();
            errorHolder.set("interrupted");
            Thread.currentThread().interrupt();
        } finally {
            focusManager.abandon();
            activeProvider.set(null);
        }

        String error = errorHolder.get();
        boolean interrupted = "interrupted".equals(error);
        return new Outcome(error == null, interrupted, error);
    }

    /** Cuts off whatever is currently playing (used for EMERGENCY pre-emption). */
    public void interruptCurrent() {
        AnnouncementProvider p = activeProvider.get();
        if (p != null) {
            p.stop();
        }
    }

    public boolean isSpeaking() {
        return activeProvider.get() != null;
    }

    public void release() {
        deviceTts.release();
        urlAudio.release();
    }
}

