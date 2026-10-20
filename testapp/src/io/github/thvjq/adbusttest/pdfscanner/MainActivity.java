package io.github.thvjq.adbusttest.pdfscanner;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.TextView;

public class MainActivity extends Activity {
    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        TextView t = new TextView(this);
        t.setPadding(48, 48, 48, 48);
        t.setTextSize(18);
        t.setText("adbust test app\n\nThis is a harmless fake adware app used "
                + "to test adbust. Press Home: a fake full-screen ad will pop "
                + "up every 15 seconds until adbust removes or declaws it.");
        setContentView(t);
        startService(new Intent(this, PopupService.class));
    }
}
