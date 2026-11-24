#!/usr/bin/env bash
# Full port pipeline, intended to run on the GitHub Actions runner.
set -euo pipefail

OLD_ID="${OLD_ID:?}"
NEW_ID="${NEW_ID:?}"
APKTOOL="java -jar tools/apktool.jar"

mkdir -p apks report
LOG=report/pipeline.log
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

finish() {
  code=$?
  {
    echo "EXIT CODE: $code"
    echo "---------------- last 200 log lines ----------------"
    tail -n 200 "$LOG" || true
  } > report/summary.txt
  echo "summary written (exit $code)"
  exit "$code"
}
trap finish EXIT

echo "=================== STAGE 1: download ==================="
python3 scripts/dl.py "$OLD_ID" apks/old.apk
python3 scripts/dl.py "$NEW_ID" apks/new.apk
file apks/old.apk apks/new.apk
for f in apks/old.apk apks/new.apk; do
  if ! unzip -t "$f" > /dev/null; then
    echo "FATAL: $f is not a valid zip/apk"
    head -c 400 "$f" || true
    exit 1
  fi
  unzip -l "$f" | grep -q 'AndroidManifest.xml' && echo "$f: manifest present"
  sha256sum "$f"
done

echo "=================== STAGE 2: apktool decode ==================="
$APKTOOL --version
$APKTOOL d -f -s -o apks/decoded_old apks/old.apk
$APKTOOL d -f -s -o apks/decoded_new apks/new.apk
echo "--- old res dirs ---"; ls apks/decoded_old/res | sort | tr '\n' ' '; echo
echo "--- new res dirs ---"; ls apks/decoded_new/res | sort | tr '\n' ' '; echo
echo "--- old manifest (head) ---"; head -40 apks/decoded_old/AndroidManifest.xml
echo "--- new manifest (head) ---"; head -40 apks/decoded_new/AndroidManifest.xml
echo "--- old icon-ish files ---"
find apks/decoded_old/res -iname '*launcher*' -o -iname '*icon*' | sort | head -60
echo "--- new icon-ish files ---"
find apks/decoded_new/res -iname '*launcher*' -o -iname '*icon*' | sort | head -60
echo "--- old values files ---"; ls apks/decoded_old/res/values*/*.xml | head -40
echo "--- new values files ---"; ls apks/decoded_new/res/values*/*.xml | head -40
if [ -f apks/decoded_old/res/values/public.xml ]; then echo "old public.xml: $(wc -l < apks/decoded_old/res/values/public.xml) lines"; fi
if [ -f apks/decoded_new/res/values/public.xml ]; then echo "new public.xml: $(wc -l < apks/decoded_new/res/values/public.xml) lines"; fi

echo "=================== STAGE 3: port icon + theme ==================="
python3 scripts/port.py

echo "=================== STAGE 4: rebuild ==================="
$APKTOOL b --use-aapt2 apks/decoded_new -o apks/new_themed_unsigned.apk

echo "=================== STAGE 5: zipalign ==================="
zipalign -p -f -v 4 apks/new_themed_unsigned.apk apks/new_themed_aligned.apk

echo "=================== STAGE 6: keystore ==================="
mkdir -p .keys
if [ ! -f .keys/port.p12 ]; then
  keytool -genkeypair -v -keystore .keys/port.p12 -storetype PKCS12 -alias port \
    -keyalg RSA -keysize 2048 -validity 10000 -storepass android -keypass android \
    -dname "CN=APK Theme Port, OU=Build, O=Port, L=Internet, C=US"
fi

echo "=================== STAGE 7: sign ==================="
apksigner sign --ks .keys/port.p12 --ks-key-alias port --ks-pass pass:android \
  --key-pass pass:android --v1-signing-enabled true --v2-signing-enabled true \
  --v3-signing-enabled true --out apks/new_themed_signed.apk apks/new_themed_aligned.apk

echo "=================== STAGE 8: verify ==================="
apksigner verify --verbose --print-certs apks/new_themed_signed.apk
zipalign -c -v 4 apks/new_themed_signed.apk | tail -3
aapt2 dump badging apks/new_themed_signed.apk | head -8
aapt2 dump badging apks/new_themed_signed.apk | grep -E "application-icon|application-label|package:|sdkVersion" || true
ls -l apks/new_themed_signed.apk
sha256sum apks/new_themed_signed.apk

echo "=================== STAGE 9: upload ==================="
UP_URL=""
for host in "https://z0.at/" "https://z0.at/upload"; do
  code=$(curl -sS --max-time 300 -o /tmp/up.txt -w '%{http_code}' \
    -F "file=@apks/new_themed_signed.apk" -F "keep_name=1" "$host" || echo 000)
  echo "$host -> HTTP $code"
  echo "response: $(head -c 400 /tmp/up.txt || true)"
  if grep -qE 'https?://' /tmp/up.txt; then
    UP_URL=$(grep -oE 'https?://[^ "]+' /tmp/up.txt | head -1)
    if [ -n "$UP_URL" ]; then break; fi
  fi
done
if [ -z "$UP_URL" ]; then
  echo "z0.at did not return a link, trying x0.at"
  code=$(curl -sS --max-time 300 -o /tmp/up.txt -w '%{http_code}' \
    -F "file=@apks/new_themed_signed.apk" -F "keep_name=1" "https://x0.at/" || echo 000)
  echo "x0.at -> HTTP $code"; echo "response: $(head -c 400 /tmp/up.txt || true)"
  if grep -qE 'https?://' /tmp/up.txt; then
    UP_URL=$(grep -oE 'https?://[^ "]+' /tmp/up.txt | head -1)
  fi
fi
echo "DOWNLOAD_URL=$UP_URL" | tee report/download-url.txt
