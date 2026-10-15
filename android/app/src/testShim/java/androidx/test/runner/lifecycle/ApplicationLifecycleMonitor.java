package androidx.test.runner.lifecycle;

public interface ApplicationLifecycleMonitor {
    void addLifecycleCallback(ApplicationLifecycleCallback callback);
    void removeLifecycleCallback(ApplicationLifecycleCallback callback);
}
