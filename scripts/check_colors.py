#!/usr/bin/env python3
"""Assert that the themed APK really carries the OLD palette values."""
import json
import os
import re
import subprocess
import sys

AAPT2 = "aapt2"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from arsc import normalize_hex  # noqa: E402


def dump(apk):
    return subprocess.run([AAPT2, "dump", "resources", apk], capture_output=True, text=True).stdout


def main():
    apk = sys.argv[1]
    raw = json.load(open("report/expected-colors.json"))
    expected = {k: normalize_hex(v) for k, v in raw.items()}
    if not expected:
        print("no expected colours recorded")
        return 0
    lines = dump(apk).splitlines()
    ok = 0
    bad = []
    for name, want in expected.items():
        pattern = re.compile(r"^resource 0x[0-9a-f]+ color/%s$" % re.escape(name))
        idx = next((i for i, line in enumerate(lines) if pattern.match(line.strip())), None)
        if idx is None:
            bad.append("%s: resource not found" % name)
            continue
        got = None
        for line in lines[idx + 1 : idx + 4]:
            m = re.search(r"#([0-9a-fA-F]{6,8})", line)
            if m:
                got = m.group(1).lower()
                break
        if got is None:
            bad.append("%s: no literal value" % name)
            continue
        want_plain = want.lstrip("#").lower()
        if got == want_plain or got.endswith(want_plain) or want_plain.endswith(got):
            ok += 1
        else:
            bad.append("%s: got #%s want %s" % (name, got, want))
    print("palette colours carrying old values: %d/%d" % (ok, len(expected)))
    for b in bad[:15]:
        print("  !!", b)
    if bad:
        print("WARN: palette partially ported")
        return 1
    print("OK: palette ported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
