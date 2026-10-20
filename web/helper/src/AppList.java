import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.os.Looper;

import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Runs on the phone through app_process (not installed as an app):
 *   CLASSPATH=/data/local/tmp/adbust-applist.dex app_process / AppList
 * Prints one tab-separated line per installed app:
 *   pkg  label  system(0/1)  enabled(0/1)  launcher(0/1)  installer  firstInstallMs
 * Plain adb has no way to read app names, which is why this exists.
 */
public class AppList {
    static Context systemContext() throws Exception {
        Looper.prepareMainLooper();
        Class<?> at = Class.forName("android.app.ActivityThread");
        Constructor<?> ctor = at.getDeclaredConstructor();
        ctor.setAccessible(true);
        Object thread = ctor.newInstance();
        Field cur = at.getDeclaredField("sCurrentActivityThread");
        cur.setAccessible(true);
        cur.set(null, thread);
        // Samsung's CompatSandbox reads the configuration while building the
        // system context, so give the thread a ConfigurationController first.
        try {
            Class<?> cc = Class.forName("android.app.ConfigurationController");
            Class<?> internal = Class.forName("android.app.ActivityThreadInternal");
            Constructor<?> ccCtor = cc.getDeclaredConstructor(internal);
            ccCtor.setAccessible(true);
            Field ccField = at.getDeclaredField("mConfigurationController");
            ccField.setAccessible(true);
            ccField.set(thread, ccCtor.newInstance(thread));
        } catch (Throwable t) { /* before Android 12 there is none */ }
        Method get = at.getDeclaredMethod("getSystemContext");
        return (Context) get.invoke(thread);
    }

    static String clean(CharSequence s) {
        return s == null ? "" : s.toString().replace('\t', ' ').replace('\n', ' ').trim();
    }

    public static void main(String[] args) throws Exception {
        PackageManager pm = systemContext().getPackageManager();
        Set<String> launchers = new HashSet<>();
        Intent main = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        for (ResolveInfo ri : pm.queryIntentActivities(main, 0)) {
            launchers.add(ri.activityInfo.packageName);
        }
        List<PackageInfo> pkgs = pm.getInstalledPackages(0);
        StringBuilder out = new StringBuilder();
        for (PackageInfo pi : pkgs) {
            ApplicationInfo ai = pi.applicationInfo;
            if (ai == null) continue;
            String label;
            try {
                label = clean(ai.loadLabel(pm));
            } catch (Throwable t) {
                label = "";
            }
            String installer = "";
            try {
                installer = clean(pm.getInstallSourceInfo(pi.packageName).getInstallingPackageName());
            } catch (Throwable t) { /* not every package has one */ }
            boolean system = (ai.flags & ApplicationInfo.FLAG_SYSTEM) != 0;
            out.append(pi.packageName).append('\t').append(label).append('\t')
               .append(system ? 1 : 0).append('\t').append(ai.enabled ? 1 : 0).append('\t')
               .append(launchers.contains(pi.packageName) ? 1 : 0).append('\t')
               .append(installer).append('\t').append(pi.firstInstallTime).append('\n');
        }
        System.out.print(out);
        System.out.flush();
    }
}
