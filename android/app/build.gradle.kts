import java.util.Properties

plugins {
    kotlin("jvm")
}

// ---------------------------------------------------------------- SDK location
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
val sdkDir: String = localProps.getProperty("sdk.dir")
    ?: System.getenv("ANDROID_HOME") ?: System.getenv("ANDROID_SDK_ROOT")
    ?: error("Set sdk.dir in local.properties or ANDROID_HOME")
val compileSdk = 35
val minSdk = 26
val buildToolsVersion: String = localProps.getProperty("build.tools") ?: "35.0.1"
val versionCode = 1
val versionName = "1.0.0"
val applicationId = "io.github.netwatch.android"

val androidJar = file("$sdkDir/platforms/android-$compileSdk/android.jar")
val buildTools = file("$sdkDir/build-tools/$buildToolsVersion")
val aapt2 = File(buildTools, "aapt2")
val zipalign = File(buildTools, "zipalign")
val d8Jar = File(buildTools, "lib/d8.jar")
val apksignerJar = File(buildTools, "lib/apksigner.jar")

val genDir = layout.buildDirectory.dir("generated/r")
val outDir = layout.buildDirectory.dir("apk")

kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }
tasks.withType<JavaCompile>().configureEach { options.release.set(17) }
java { sourceSets["main"].java.srcDir(genDir) }

dependencies {
    compileOnly(files(androidJar))
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    // Core logic is Android-free; tests run on the JVM with real org.json and SQLite (JDBC).
    testImplementation("org.json:json:20240303")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
    testImplementation("org.xerial:sqlite-jdbc:3.47.1.0")
}

// ---------------------------------------------------------------- resources
val compileResources by tasks.registering(Exec::class) {
    val res = file("src/main/res")
    val out = layout.buildDirectory.file("intermediates/res.zip")
    inputs.dir(res); outputs.file(out)
    doFirst { out.get().asFile.parentFile.mkdirs() }
    commandLine(aapt2, "compile", "--dir", res, "-o", out.get().asFile)
}

val linkResources by tasks.registering(Exec::class) {
    dependsOn(compileResources)
    val manifest = file("src/main/AndroidManifest.xml")
    val flat = layout.buildDirectory.file("intermediates/res.zip")
    val base = layout.buildDirectory.file("intermediates/base.apk")
    val rules = layout.buildDirectory.file("intermediates/aapt_rules.txt")
    inputs.file(manifest); inputs.file(flat)
    outputs.file(base); outputs.dir(genDir); outputs.file(rules)
    doFirst { genDir.get().asFile.mkdirs() }
    commandLine(
        aapt2, "link", "-I", androidJar, "--manifest", manifest, "-o", base.get().asFile,
        "--java", genDir.get().asFile, "--proguard", rules.get().asFile,
        "--min-sdk-version", minSdk, "--target-sdk-version", compileSdk,
        "--version-code", versionCode, "--version-name", versionName,
        "--rename-manifest-package", applicationId, "--auto-add-overlay", flat.get().asFile,
    )
}
tasks.named("compileKotlin") { dependsOn(linkResources) }
tasks.named("compileJava") { dependsOn(linkResources) }

// ---------------------------------------------------------------- dex (R8 shrinks Kotlin stdlib + coroutines)
val dex by tasks.registering(JavaExec::class) {
    dependsOn("jar")
    val jarTask = tasks.named<Jar>("jar")
    val runtime = configurations["runtimeClasspath"]
    val dexOut = layout.buildDirectory.dir("intermediates/dex")
    inputs.files(jarTask, runtime, file("proguard-rules.pro"))
    outputs.dir(dexOut)
    classpath = files(d8Jar)
    mainClass.set("com.android.tools.r8.R8")
    doFirst {
        dexOut.get().asFile.apply { deleteRecursively(); mkdirs() }
        args(
            listOf("--release", "--min-api", "$minSdk", "--lib", androidJar.path,
                "--pg-conf", file("proguard-rules.pro").path,
                "--pg-conf", layout.buildDirectory.file("intermediates/aapt_rules.txt").get().asFile.path,
                "--output", dexOut.get().asFile.path, jarTask.get().archiveFile.get().asFile.path) +
                runtime.files.map { it.path }
        )
    }
}

// ---------------------------------------------------------------- package, align, sign
val debugKeystore = rootProject.file("keystore/netwatch-debug.keystore")

val createKeystore by tasks.registering(Exec::class) {
    onlyIf { !debugKeystore.exists() }
    doFirst { debugKeystore.parentFile.mkdirs() }
    commandLine("keytool", "-genkeypair", "-keystore", debugKeystore, "-storepass", "android",
        "-alias", "netwatch", "-keypass", "android", "-keyalg", "RSA", "-keysize", "2048",
        "-validity", "10000", "-dname", "CN=NetWatch Debug,O=NetWatch,C=US")
}

val assembleApk by tasks.registering {
    group = "build"
    description = "Builds and signs build/apk/netwatch-<version>.apk"
    dependsOn(linkResources, dex, createKeystore)
    val unsigned = layout.buildDirectory.file("intermediates/unaligned.apk")
    val aligned = layout.buildDirectory.file("intermediates/aligned.apk")
    val finalApk = outDir.map { it.file("netwatch-$versionName.apk") }
    outputs.file(finalApk)
    doLast {
        val u = unsigned.get().asFile
        layout.buildDirectory.file("intermediates/base.apk").get().asFile.copyTo(u, overwrite = true)
        val dexDir = layout.buildDirectory.dir("intermediates/dex").get().asFile
        providers.exec { workingDir(dexDir); commandLine("zip", "-q", "-X", u.path, *dexDir.list()!!.sorted().toTypedArray()) }.result.get()
        providers.exec { commandLine(zipalign, "-p", "-f", "4", u.path, aligned.get().asFile.path) }.result.get()
        val out = finalApk.get().asFile.apply { parentFile.mkdirs() }
        providers.exec {
            commandLine("java", "-jar", apksignerJar.path, "sign", "--ks", debugKeystore.path,
                "--ks-pass", "pass:android", "--ks-key-alias", "netwatch", "--key-pass", "pass:android",
                "--out", out.path, aligned.get().asFile.path)
        }.result.get()
        providers.exec { commandLine("java", "-jar", apksignerJar.path, "verify", out.path) }.result.get()
        println("APK: ${out.path} (${out.length() / 1024} KiB)")
    }
}
