// Neo Feed's `google-gsa` module, vendored (0062): the launcher side of Google
// Discover's overlay protocol, which Lawnchair speaks to its "Feed provider".
// Origin and licence in README.md. Only this file and the README are ours.
plugins {
    alias(libs.plugins.android.library)
}

android {
    namespace = "com.google.android.libraries"
    compileSdk = 37
    buildToolsVersion = "37.0.0"

    defaultConfig {
        minSdk = 33
        consumerProguardFiles("consumer-rules.pro")
    }

    // Switch expressions in OverlayControllerCallback.
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation(libs.androidx.annotation)
    implementation(libs.androidx.lifecycle.runtime)
    implementation(libs.androidx.savedstate)
}
