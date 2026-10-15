// NetWatch for Android.
// Built without the Android Gradle Plugin: Gradle compiles Kotlin against android.jar and drives the
// Android SDK build-tools (aapt2, R8/D8, zipalign, apksigner) directly. Only the SDK and Maven Central
// are needed — see README.md.
pluginManagement {
    repositories {
        maven("https://maven-central.storage-download.googleapis.com/maven2") // Maven Central mirror
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven("https://maven-central.storage-download.googleapis.com/maven2") // Maven Central mirror
        mavenCentral()
    }
}
rootProject.name = "NetWatch"
include(":app")
