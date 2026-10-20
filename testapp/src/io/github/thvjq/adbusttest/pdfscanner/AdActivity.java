package io.github.thvjq.adbusttest.pdfscanner;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.TextView;

public class AdActivity extends Activity {
    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        TextView t = new TextView(this);
        t.setGravity(Gravity.CENTER);
        t.setTextSize(28);
        t.setTextColor(Color.WHITE);
        t.setBackgroundColor(Color.rgb(200, 30, 60));
        t.setText("FAKE AD\n\n(adbust test popup)\n\ntap to close");
        t.setOnClickListener(v -> finish());
        setContentView(t);
    }
}
