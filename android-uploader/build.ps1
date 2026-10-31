# 无 Gradle 构建 APK（需要 Android SDK + JDK 17+）
param(
    [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$BuildTools = "36.0.0",
    [string]$Platform = "android-36"
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$bt = Join-Path $Sdk "build-tools\$BuildTools"
$androidJar = Join-Path $Sdk "platforms\$Platform\android.jar"
$out = Join-Path $root "build"
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path $out, "$out\classes", "$out\dex", "$out\gen" | Out-Null

Write-Host "1/6 aapt2 compile"
& "$bt\aapt2.exe" compile --dir "$root\res" -o "$out\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 compile 失败" }

Write-Host "2/6 aapt2 link"
& "$bt\aapt2.exe" link -o "$out\base.apk" --manifest "$root\AndroidManifest.xml" -I $androidJar --min-sdk-version 26 --target-sdk-version 36 --java "$out\gen" "$out\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 link 失败" }

Write-Host "3/6 javac"
$sources = Get-ChildItem "$root\src", "$out\gen" -Recurse -Filter *.java | ForEach-Object { $_.FullName }
& javac --release 17 -encoding UTF-8 -cp $androidJar -d "$out\classes" $sources
if ($LASTEXITCODE -ne 0) { throw "javac 失败" }

Write-Host "4/6 d8"
$classes = Get-ChildItem "$out\classes" -Recurse -Filter *.class | ForEach-Object { $_.FullName }
& "$bt\d8.bat" --release --lib $androidJar --min-api 26 --output "$out\dex" $classes
if ($LASTEXITCODE -ne 0) { throw "d8 失败" }

Write-Host "5/6 打包 dex"
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open("$out\base.apk", "Update")
$old = $zip.GetEntry("classes.dex")
if ($old) { $old.Delete() }
[System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "$out\dex\classes.dex", "classes.dex") | Out-Null
$zip.Dispose()
& "$bt\zipalign.exe" -f 4 "$out\base.apk" "$out\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "zipalign 失败" }

Write-Host "6/6 apksigner"
$ks = Join-Path $root "debug.keystore"
if (-not (Test-Path $ks)) {
    & keytool -genkeypair -keystore $ks -storepass android -alias androiddebugkey -keypass android -dname "CN=Android Debug,O=Android,C=US" -keyalg RSA -validity 10000 2>&1 | Out-Null
}
& "$bt\apksigner.bat" sign --ks $ks --ks-pass pass:android --key-pass pass:android --out "$root\qqmonitor-uploader.apk" "$out\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner 失败" }

Write-Host "完成：$root\qqmonitor-uploader.apk"
