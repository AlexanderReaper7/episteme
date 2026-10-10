plugins {
    alias(libs.plugins.android.test)
    alias(libs.plugins.baselineprofile)
}

// Generates app/src/release/generated/baselineProfiles/ by driving a release
// build of :app through the feed and an article:
//   ./gradlew :app:generateReleaseBaselineProfile
// on whatever device adb lists. The profile names methods, not machine code, so
// the emulator's x86_64 serves the phone's arm64.
android {
    namespace = "net.episteme.baselineprofile"
    compileSdk = 37
    buildToolsVersion = "37.0.0"

    defaultConfig {
        minSdk = 33
        targetSdk = 37
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    targetProjectPath = ":app"
}

baselineProfile {
    useConnectedDevices = true
}

dependencies {
    implementation(libs.androidx.junit)
    implementation(libs.androidx.uiautomator)
    implementation(libs.androidx.benchmark.macro.junit4)
}
