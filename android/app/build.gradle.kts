plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.baselineprofile)
}

// Where the server is. The default is the tailnet name the phone resolves; the
// emulator reaches the host's dev server with
//   ./gradlew installDebug -Pepisteme.baseUrl=http://10.0.2.2:8201
val baseUrl = providers.gradleProperty("episteme.baseUrl")
    .orElse("https://episteme.java-tint.ts.net")
    .get()

android {
    namespace = "net.episteme.app"
    compileSdk = 37
    buildToolsVersion = "37.0.0"

    defaultConfig {
        applicationId = "net.episteme.app"
        // Android 13, the reader's phone. Predictive back animates from 34 up;
        // below that the same code just completes the gesture.
        minSdk = 33
        targetSdk = 37
        versionCode = 1
        versionName = "0.1"
        buildConfigField("String", "BASE_URL", "\"$baseUrl\"")
    }

    buildTypes {
        release {
            // A debuggable, unshrunk build is not what the reader should judge
            // scrolling by: on the phone the debug feed missed 40% of frame
            // deadlines, nearly all on the UI thread.
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
            // There is no release key yet. The debug key lets this install over a
            // debug build without losing app data.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.viewmodel.navigation3)
    implementation(libs.androidx.navigation3.runtime)
    implementation(libs.androidx.navigation3.ui)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons)
    implementation(libs.compose.ui.tooling.preview)
    debugImplementation(libs.compose.ui.tooling)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.okhttp)
    implementation(libs.coil.compose)
    implementation(libs.coil.network.okhttp)
    implementation(libs.androidx.browser)
    implementation(libs.androidx.webkit)
    implementation(libs.markdown.renderer.m3)
    implementation(libs.vico.compose.m3)
    implementation(libs.media3.exoplayer)
    implementation(libs.media3.session)
    // Hands the shipped profile to ART on first launch. Without the Play Store
    // nothing else does, and a sideloaded APK stays interpreted and JIT-compiled.
    implementation(libs.androidx.profileinstaller)
    baselineProfile(project(":baselineprofile"))
    testImplementation(libs.junit)
}
