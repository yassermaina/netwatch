package androidx.test.platform.app;

import android.app.Instrumentation;
import android.os.Bundle;

public final class InstrumentationRegistry {
    private static Instrumentation instrumentation;
    private static Bundle arguments = new Bundle();

    private InstrumentationRegistry() {}

    public static void registerInstance(Instrumentation i, Bundle args) {
        instrumentation = i;
        arguments = args == null ? new Bundle() : new Bundle(args);
    }

    public static Instrumentation getInstrumentation() {
        if (instrumentation == null) throw new IllegalStateException("No instrumentation registered");
        return instrumentation;
    }

    public static Bundle getArguments() { return new Bundle(arguments); }
}
