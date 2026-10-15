package androidx.test.internal.runner.lifecycle;

import android.app.Application;
import androidx.test.runner.lifecycle.ApplicationLifecycleCallback;
import androidx.test.runner.lifecycle.ApplicationLifecycleMonitor;
import androidx.test.runner.lifecycle.ApplicationStage;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

public final class ApplicationLifecycleMonitorImpl implements ApplicationLifecycleMonitor {
    private final List<ApplicationLifecycleCallback> callbacks = new CopyOnWriteArrayList<>();

    @Override public void addLifecycleCallback(ApplicationLifecycleCallback c) { callbacks.add(c); }
    @Override public void removeLifecycleCallback(ApplicationLifecycleCallback c) { callbacks.remove(c); }

    public void signalLifecycleChange(Application app, ApplicationStage stage) {
        for (ApplicationLifecycleCallback c : callbacks) c.onApplicationLifecycleChanged(app, stage);
    }
}
