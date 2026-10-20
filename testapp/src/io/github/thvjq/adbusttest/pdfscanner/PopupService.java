package io.github.thvjq.adbusttest.pdfscanner;

import android.app.Service;
import android.content.Intent;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

/** Starts AdActivity every 15 s. Starting an activity from the background
 *  only works while the app holds SYSTEM_ALERT_WINDOW, which is exactly the
 *  right that adbust's declaw takes away. */
public class PopupService extends Service {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable popup = new Runnable() {
        @Override
        public void run() {
            Intent i = new Intent(PopupService.this, AdActivity.class);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                startActivity(i);
            } catch (RuntimeException ignored) {
                // background start refused - that's the declawed state
            }
            handler.postDelayed(this, 15000);
        }
    };

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        handler.removeCallbacks(popup);
        handler.postDelayed(popup, 15000);
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
