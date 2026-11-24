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

SIG_EXT = (".SF", ".RSA", ".DSA", ".EC", "MANIFEST.MF")


def is_signature(name):
    base = name.rsplit("/", 1)[-1].upper()
    return name.startswith("META-INF/") and base.endswith(SIG_EXT)


def is_stamp(name):
    return name.startswith("stamp-cert") or name.endswith("stamp-cert-sha256")


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
    dropped = []

    for name in sorted(a_map):
        if is_signature(name) or is_stamp(name):
            if name not in b_map:
                dropped.append(name)
            continue
        if name in allowed_removed:
            if name in b_map:
                problems.append("file that should be dropped is present: " + name)
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

    added = []
    for name in sorted(b_map):
        if name in a_map:
            continue
        if is_signature(name):
            added.append(name)
            continue
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
        "dropped old signature:  %s" % (", ".join(sorted(dropped)) if dropped else "none"),
        "added new signature:    %s" % (", ".join(sorted(added)) if added else "none"),
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
    print("\nOK: only icon bitmaps + resources.arsc differ; code, libs and assets are untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
