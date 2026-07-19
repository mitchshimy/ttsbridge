package dev.local.ttsbridge.core;

import android.content.Context;

import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import dev.local.ttsbridge.providers.AnnouncementProvider;
import dev.local.ttsbridge.providers.DeviceTtsProvider;
import dev.local.ttsbridge.providers.UrlAudioProvider;

/**
 * Picks the right provider for an announcement and plays it synchronously
 * from the caller's point of view (play() blocks the calling thread until
 * finished/errored/timed out/stopped), which keeps AnnouncementService's
 * worker loop simple: pop from queue, engine.play(), repeat.
 */
public class AnnouncementEngine {

    public interface EventListener {
        void onAnnouncementStarted(Announcement a);
        void onAnnouncementFinished(Announcement a, String errorOrNull);
    }

    private final DeviceTtsProvider deviceTts;
    private final UrlAudioProvider urlAudio;
    private final List<AnnouncementProvider> providers;
    private final AudioFocusManager focusManager;
    private final AtomicReference<AnnouncementProvider> activeProvider = new AtomicReference<>();

    public AnnouncementEngine(Context appContext) {
        deviceTts = new DeviceTtsProvider(appContext);
        urlAudio = new UrlAudioProvider();
        providers = Arrays.asList(urlAudio, deviceTts); // url checked first, falls through to device tts
        focusManager = new AudioFocusManager(appContext);
    }

    public AudioFocusManager focus() {
        return focusManager;
    }

    /** Plays one announcement to completion (or interruption). Blocks the calling thread. */
    public void play(Announcement a, EventListener events) {
        AnnouncementProvider provider = pickProvider(a);
        if (provider == null) {
            events.onAnnouncementFinished(a, "no_provider_available");
            return;
        }
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

        events.onAnnouncementFinished(a, errorHolder.get());
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
        for (AnnouncementProvider p : providers) {
            p.release();
        }
    }

    private AnnouncementProvider pickProvider(Announcement a) {
        for (AnnouncementProvider p : providers) {
            if (p.supports(a)) return p;
        }
        return null;
    }
}
