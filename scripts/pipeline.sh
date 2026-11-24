#!/usr/bin/env bash
# Port old icon + palette onto the new Instagram APK.
#
# The new APK's resources.arsc cannot be rebuilt by aapt2 (6000 layouts are
# stored as packed strings), so we preserve the table byte-for-byte and only
# swap icon bitmaps + patch palette colours in place.
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
  echo "$f: $(unzip -l "$f" | tail -1)"
done

echo "=================== STAGE 2: apktool decode (inspection) ==================="
$APKTOOL --version
$APKTOOL d -f -s -o apks/decoded_old apks/old.apk
$APKTOOL d -f -s -o apks/decoded_new apks/new.apk

echo "=================== STAGE 2b: diagnostics ==================="
bash scripts/diag.sh || true

echo "=================== STAGE 3: analysis (what would be ported) ==================="
python3 scripts/port.py || true

echo "=================== STAGE 4: repackage (icon + palette) ==================="
python3 scripts/repackage.py --apk apks/new.apk --old apks/decoded_old \
  --new apks/decoded_new --out apks/new_themed_unsigned.apk
unzip -t apks/new_themed_unsigned.apk > /dev/null && echo "unsigned zip OK"

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
unzip -t apks/new_themed_signed.apk > /dev/null && echo "signed zip OK"
echo "--- badging: original ---"
aapt2 dump badging apks/new.apk | head -6
echo "--- badging: themed ---"
aapt2 dump badging apks/new_themed_signed.apk | head -6
echo "--- icon resources: original ---"
aapt2 dump resources apks/new.apk | grep -A6 'mipmap/icon$' | head -12 || true
echo "--- icon resources: themed ---"
aapt2 dump resources apks/new_themed_signed.apk | grep -A6 'mipmap/icon$' | head -12 || true
echo "--- palette check: original ---"
aapt2 dump resources apks/new.apk | grep -A1 -E 'color/(blue_9|orange_1|grey_5)$' | head -12 || true
echo "--- palette check: themed ---"
aapt2 dump resources apks/new_themed_signed.apk | grep -A1 -E 'color/(blue_9|orange_1|grey_5)$' | head -12 || true
echo "--- integrity: only icon + arsc may differ ---"
python3 scripts/verify.py apks/new.apk apks/new_themed_signed.apk
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

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## Theme port result"
    echo ""
    echo "* Size: $(stat -c%s apks/new_themed_signed.apk) bytes"
    echo "* SHA256: $(sha256sum apks/new_themed_signed.apk | cut -d' ' -f1)"
    echo "* Upload URL: ${UP_URL:-none}"
  } >> "$GITHUB_STEP_SUMMARY"
fi
