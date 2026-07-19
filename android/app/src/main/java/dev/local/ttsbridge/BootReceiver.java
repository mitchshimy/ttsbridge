package dev.local.ttsbridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * Starts the persistent AnnouncementService right after boot, so it's ready
 * (queue + HTTP API + engine warmed up) before the first real announcement.
 */
public class BootReceiver extends BroadcastReceiver {
    private static final String TAG = "TtsBridge/Boot";

    @Override
    public void onReceive(Context context, Intent bootIntent) {
        Log.i(TAG, "onReceive: action=" + bootIntent.getAction());
        try {
            Intent startIntent = new Intent(context, AnnouncementService.class);
            context.startForegroundService(startIntent);
            Log.i(TAG, "startForegroundService() called successfully");
        } catch (Exception e) {
            // Previously this was unguarded, so a failure here (e.g. the OS
            // refusing a background FGS start on some device/OEM skin) would
            // be indistinguishable from BOOT_COMPLETED never arriving at
            // all - logging it explicitly closes that blind spot.
            Log.e(TAG, "Failed to start AnnouncementService from boot", e);
        }
    }
}
