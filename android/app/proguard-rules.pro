# Components referenced from AndroidManifest.xml are kept by the aapt2-generated rules.
-keepattributes SourceFile,LineNumberTable
-dontwarn kotlinx.coroutines.debug.**
-dontwarn org.codehaus.mojo.animal_sniffer.**
-dontwarn androidx.annotation.**
# kotlinx.coroutines: Dispatchers.Main is discovered through ServiceLoader
-keep class kotlinx.coroutines.android.AndroidDispatcherFactory { *; }
-keep class kotlinx.coroutines.android.AndroidExceptionPreHandler { *; }
-keepnames class kotlinx.coroutines.internal.MainDispatcherFactory {}
-keepnames class kotlinx.coroutines.CoroutineExceptionHandler {}
