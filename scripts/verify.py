#!/usr/bin/env python3
"""Prove that only the icon bitmaps and resources.arsc changed.

Every dex, every native lib, every asset, every other resource file must be
byte-identical to the input APK, otherwise the "new features still work" claim
would be false.
"""
import json
import os
import sys
import zipfile


def main():
    orig_path, out_path = sys.argv[1], sys.argv[2]
    meta = json.load(open("report/replaced.json"))
    allowed_changed = set(meta["replaced"]) | {"resources.arsc"}
    allowed_removed = set(meta["removed"])

    a = zipfile.ZipFile(orig_path)
    b = zipfile.ZipFile(out_path)
    a_map = {i.filename: i for i in a.infolist()}
    b_map = {i.filename: i for i in b.infolist()}

    problems = []
    changed = []

    for name in sorted(a_map):
        if name in allowed_removed:
            if name in b_map:
                problems.append("signature file still present: " + name)
            continue
        if name not in b_map:
            problems.append("file missing from output: " + name)
            continue
        ia, ib = a_map[name], b_map[name]
        if ia.file_size == ib.file_size and ia.CRC == ib.CRC:
            continue
        if name in allowed_changed:
            changed.append(name)
            continue
        problems.append("unexpected change: %s (%d -> %d bytes)" % (name, ia.file_size, ib.file_size))

    for name in sorted(b_map):
        if name not in a_map:
            problems.append("unexpected new file: " + name)

    dexes = [n for n in a_map if n.startswith("classes") and n.endswith(".dex")]
    libs = [n for n in a_map if n.startswith("lib/")]
    assets = [n for n in a_map if n.startswith("assets/")]
    same_dex = sum(1 for n in dexes if n in b_map and a_map[n].CRC == b_map[n].CRC)
    same_lib = sum(1 for n in libs if n in b_map and a_map[n].CRC == b_map[n].CRC)
    same_assets = sum(1 for n in assets if n in b_map and a_map[n].CRC == b_map[n].CRC)

    lines = [
        "original: %s (%d entries)" % (orig_path, len(a_map)),
        "output:   %s (%d entries)" % (out_path, len(b_map)),
        "dex files identical:    %d/%d" % (same_dex, len(dexes)),
        "native libs identical:  %d/%d" % (same_lib, len(libs)),
        "assets identical:       %d/%d" % (same_assets, len(assets)),
        "changed on purpose:     %s" % (", ".join(changed) if changed else "none"),
        "removed signatures:     %s" % (", ".join(sorted(allowed_removed)) if allowed_removed else "none"),
        "problems:               %d" % len(problems),
    ]
    lines.extend("  !! " + p for p in problems[:40])
    text = "\n".join(lines)
    print(text)
    os.makedirs("report", exist_ok=True)
    with open("report/verify.txt", "w") as fh:
        fh.write(text + "\n")
    if problems:
        print("\nFAIL: output differs from the original APK outside the allowed set", file=sys.stderr)
        return 1
    print("\nOK: output differs only in the icon bitmaps and resources.arsc")
    return 0


if __name__ == "__main__":
    sys.exit(main())
