package androidx.test.runner.intent;

public final class IntentStubberRegistry {
    private static IntentStubber instance;
    private IntentStubberRegistry() {}
    public static void load(IntentStubber s) { instance = s; }
    public static boolean isLoaded() { return instance != null; }
    public static IntentStubber getInstance() { return instance; }
}
