package dev.local.ttsbridge.core;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.Lock;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Thread-safe priority queue: lower Priority.rank first, then FIFO within the
 * same rank. Applies two policies at enqueue time:
 *
 *  - Duplicate suppression: an item with the same dedupeKey() either already
 *    sitting in the queue, OR accepted within the last DEDUP_WINDOW_MS
 *    (whether it's still queued, currently playing, or already finished),
 *    means the new one is dropped. The time-window part matters because a
 *    short announcement can go from "queued" to "already playing" before the
 *    next duplicate request even lands - checking the backlog alone misses
 *    that case entirely.
 *  - Staleness: category-specific max age. A "motion" announcement that sat
 *    around for 30s is no longer useful once it reaches the front - it's
 *    dropped instead of played (see AnnouncementService.STALE_RULES).
 */
public class AnnouncementQueue {

    public interface StalenessPolicy {
        long maxAgeMsFor(String category);
    }

    private static final long DEDUP_WINDOW_MS = 5000;

    private final Lock lock = new ReentrantLock();
    private final Condition notEmpty = lock.newCondition();
    private final PriorityQueue<Announcement> heap = new PriorityQueue<>(
            Comparator.<Announcement, Integer>comparing(a -> a.priority.rank)
                    .thenComparingLong(a -> a.enqueuedAt));
    private final Map<String, Long> recentlyAccepted = new HashMap<>();
    private final StalenessPolicy stalenessPolicy;

    public AnnouncementQueue(StalenessPolicy stalenessPolicy) {
        this.stalenessPolicy = stalenessPolicy;
    }

    /** @return the accepted announcement, or null if it was suppressed as a duplicate. */
    public Announcement offer(Announcement a) {
        lock.lock();
        try {
            long now = System.currentTimeMillis();
            purgeStaleRecentKeys(now);

            for (Announcement existing : heap) {
                if (existing.dedupeKey().equals(a.dedupeKey())) {
                    return null; // duplicate already queued
                }
            }
            Long lastAccepted = recentlyAccepted.get(a.dedupeKey());
            if (lastAccepted != null && (now - lastAccepted) < DEDUP_WINDOW_MS) {
                return null; // duplicate of something accepted moments ago
            }

            recentlyAccepted.put(a.dedupeKey(), now);
            heap.offer(a);
            notEmpty.signalAll();
            return a;
        } finally {
            lock.unlock();
        }
    }

    private void purgeStaleRecentKeys(long now) {
        recentlyAccepted.entrySet().removeIf(e -> (now - e.getValue()) > DEDUP_WINDOW_MS);
    }

    /** Blocks until an item is available, skipping any that have gone stale. */
    public Announcement takeNextFresh() throws InterruptedException {
        lock.lock();
        try {
            while (true) {
                while (heap.isEmpty()) {
                    notEmpty.await();
                }
                Announcement head = heap.poll();
                long maxAge = stalenessPolicy.maxAgeMsFor(head.category);
                if (head.isStale(maxAge)) {
                    continue; // drop it, loop again
                }
                return head;
            }
        } finally {
            lock.unlock();
        }
    }

    public boolean hasEmergencyWaiting() {
        lock.lock();
        try {
            return !heap.isEmpty() && heap.peek().priority == Priority.EMERGENCY;
        } finally {
            lock.unlock();
        }
    }

    public void clear() {
        lock.lock();
        try {
            heap.clear();
        } finally {
            lock.unlock();
        }
    }

    public List<Announcement> snapshot() {
        lock.lock();
        try {
            return new ArrayList<>(heap);
        } finally {
            lock.unlock();
        }
    }

    public int size() {
        lock.lock();
        try {
            return heap.size();
        } finally {
            lock.unlock();
        }
    }
}
