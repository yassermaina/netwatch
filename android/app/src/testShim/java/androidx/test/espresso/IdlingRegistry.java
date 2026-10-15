package androidx.test.espresso;

import android.os.Looper;
import java.util.Collection;
import java.util.Collections;

public final class IdlingRegistry {
    private static final IdlingRegistry INSTANCE = new IdlingRegistry();
    public static IdlingRegistry getInstance() { return INSTANCE; }
    public Collection<IdlingResource> getResources() { return Collections.emptyList(); }
    public Collection<Looper> getLoopers() { return Collections.emptyList(); }
}
