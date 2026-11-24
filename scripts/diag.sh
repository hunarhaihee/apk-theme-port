#!/usr/bin/env bash
# Compact diagnostics about the two APKs, written to report/diag.txt (small).
set -uo pipefail
OUT=report/diag.txt
mkdir -p report
: > "$OUT"
w() { echo "$@" >> "$OUT"; }

w "apktool: $(java -jar tools/apktool.jar --version 2>&1 | head -1)"
w "aapt2: $(command -v aapt2) $(aapt2 version 2>&1 | head -1)"

for triple in "OLD:apks/old.apk:apks/decoded_old" "NEW:apks/new.apk:apks/decoded_new"; do
  label=${triple%%:*}
  rest=${triple#*:}
  apk=${rest%%:*}
  dir=${rest#*:}
  w ""
  w "================ $label ($apk) ================"
  w "version: $(aapt2 dump badging "$apk" 2>/dev/null | head -1)"
  w "res/layout files: $(ls "$dir/res/layout" 2>/dev/null | wc -l)"
  w "res/drawable files: $(ls "$dir/res/drawable" 2>/dev/null | wc -l)"
  w "res/xml files: $(ls "$dir/res/xml" 2>/dev/null | wc -l)"
  w "values*/layouts.xml files: $(ls "$dir"/res/values*/layouts.xml 2>/dev/null | wc -l)"
  w "total typed items across values*/layouts.xml: $(cat "$dir"/res/values*/layouts.xml 2>/dev/null | grep -c '<item type=')"
  w "sample of values/layouts.xml:"
  head -6 "$dir/res/values/layouts.xml" 2>/dev/null | sed 's/^/    /' >> "$OUT"
  w "alias name -> layout file present?"
  for n in $(grep -hoE 'name="[^"]+"' "$dir"/res/values/layouts.xml 2>/dev/null | head -6 | sed 's/name="//;s/"$//'); do
    if [ -f "$dir/res/layout/$n.xml" ]; then
      w "    $n -> layout file EXISTS ($(wc -c < "$dir/res/layout/$n.xml") bytes)"
    else
      w "    $n -> NO layout file"
    fi
  done
  sample=$(grep -hoE 'name="[^"]+"' "$dir"/res/values/layouts.xml 2>/dev/null | head -1 | sed 's/name="//;s/"$//')
  if [ -n "${sample:-}" ]; then
    w "ORIGINAL arsc entry for '$sample':"
    aapt2 dump resources "$apk" 2>/dev/null | grep -m1 -A4 "layout/$sample" | sed 's/^/    /' >> "$OUT"
  fi
  w "ORIGINAL arsc icon entries:"
  aapt2 dump resources "$apk" 2>/dev/null | grep -m4 -A2 -E "(mipmap|drawable)/icon(\s|$)" | sed 's/^/    /' >> "$OUT"
  w "adaptive icon xml in decoded tree:"
  find "$dir/res" -path '*anydpi*' -name 'icon*' | sed 's/^/    /' >> "$OUT"
  for f in $(find "$dir/res" -path '*anydpi*' -name 'icon*.xml' | head -2); do
    w "  --- $f"
    sed 's/^/    /' "$f" >> "$OUT"
  done
  w "mipmap-*/icon* and ig_launcher* files:"
  find "$dir/res" -name 'icon.*' -o -name 'ig_launcher*' | sort | sed 's/^/    /' >> "$OUT"
  w "public.xml id for icon/ig_launcher:"
  grep -h -E 'name="(icon|ig_launcher_[a-z]+)"' "$dir/res/values/public.xml" 2>/dev/null | head -10 | sed 's/^/    /' >> "$OUT"
done

w ""
w "================ END DIAG ================"
cat "$OUT"
