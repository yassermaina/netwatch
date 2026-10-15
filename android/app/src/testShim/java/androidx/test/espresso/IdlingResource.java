package androidx.test.espresso;

public interface IdlingResource {
    String getName();
    boolean isIdleNow();
    void registerIdleTransitionCallback(ResourceCallback callback);

    interface ResourceCallback { void onTransitionToIdle(); }
}
