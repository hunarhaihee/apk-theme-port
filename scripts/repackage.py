#!/usr/bin/env python3
"""Repackage the NEW apk with the OLD icon + OLD palette, without rebuilding the
resource table.

Why: Instagram 456 stores ~6000 layouts as packed strings inside resources.arsc
(value `L|offset|size|crc`), so no aapt2 build can reproduce the table. Anything
else would destroy resources. So instead we keep every compiled resource, all
dex, all libs and all assets byte-identical and only:
  * replace the launcher icon bitmaps (legacy mipmap/icon + adaptive
    foreground/background layers),
  * patch the ARGB colour values of the shared palette inside resources.arsc
    in place (same size, so no offsets move).
"""
import argparse
import glob
import io
import json
import os
import re
import struct
import subprocess
import sys
import zipfile
from xml.etree import ElementTree as ET

from PIL import Image

AAPT2 = os.environ.get("AAPT2", "aapt2")
LOG = []


def log(*parts):
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    LOG.append(line)


# --------------------------------------------------------------------------- colours
def collect_colors(root):
    colors = {}
    for d in sorted(glob.glob(os.path.join(root, "res", "values*"))):
        for f in sorted(glob.glob(os.path.join(d, "*.xml"))):
            try:
                r = ET.parse(f).getroot()
            except Exception:  # noqa: BLE001
                continue
            if r.tag != "resources":
                continue
            for ch in r:
                if ch.tag.split("}")[-1] != "color":
                    continue
                name = ch.get("name")
                val = (ch.text or "").strip()
                if name and val:
                    colors.setdefault(name, val)
    return colors


def resolve(colors, value, depth=0):
    if not value or depth > 6:
        return None
    value = value.strip()
    if value.startswith("#"):
        return value
    m = re.match(r"^@color/([\w.]+)$", value)
    if m:
        return resolve(colors, colors.get(m.group(1)), depth + 1)
    return None


def swap_color(data, cur_hex, new_hex):
    """Replace every Res_value whose ARGB/RGB payload equals cur_hex."""
    total = 0
    variants = []
    if len(cur_hex) == 9:
        variants.append((0x1C, int(cur_hex[1:], 16), int(new_hex[1:], 16)))
    elif len(cur_hex) == 7:
        variants.append((0x1C, 0xFF000000 | int(cur_hex[1:], 16), 0xFF000000 | int(new_hex[1:], 16)))
        variants.append((0x1D, int(cur_hex[1:], 16), int(new_hex[1:], 16)))
    for dtype, vc, vn in variants:
        needle = b"\x08\x00\x00" + bytes([dtype]) + struct.pack("<I", vc)
        repl = b"\x08\x00\x00" + bytes([dtype]) + struct.pack("<I", vn)
        count = data.count(needle)
        if count:
            data = data.replace(needle, repl)
            total += count
    return data, total


# --------------------------------------------------------------------------- icons
def file_map(apk):
    """resource name ('mipmap/icon') -> [(config, zip path)] from the real arsc."""
    proc = subprocess.Popen(
        [AAPT2, "dump", "resources", apk], stdout=subprocess.PIPE, text=True, errors="replace"
    )
    cur = None
    out = {}
    for line in proc.stdout:
        s = line.strip()
        if s.startswith("resource "):
            parts = s.split()
            cur = parts[-1] if len(parts) >= 3 else None
        elif s.startswith("(") and "(file)" in s:
            cfg = s[1 : s.index(")")] if ")" in s else ""
            rest = s.split("(file)", 1)[1].strip().split()
            if rest and cur:
                out.setdefault(cur, []).append((cfg, rest[0]))
    proc.wait()
    return out


def best_old_icon(decoded_old):
    cands = []
    for pattern in ("res/drawable*/icon.png", "res/mipmap*/icon.png"):
        cands.extend(glob.glob(os.path.join(decoded_old, pattern)))
    best, best_area = None, -1
    for c in cands:
        try:
            with Image.open(c) as im:
                area = im.width * im.height
            if area > best_area:
                best, best_area = c, area
        except Exception:  # noqa: BLE001
            continue
    return best


def dominant_color(im):
    small = im.convert("RGB").resize((48, 48))
    q = small.quantize(colors=6, method=0)
    palette = q.getpalette()[: 18]
    counts = sorted(q.getcolors(), reverse=True)
    idx = counts[0][1]
    return (palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2], 255)


def encode(im, original_bytes):
    if b"RIFF" in original_bytes[:16] and b"WEBP" in original_bytes[:16]:
        buf = io.BytesIO()
        im.save(buf, "WEBP", lossless=True, quality=100)
        return buf.getvalue()
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def make_icon_replacement(original_bytes, kind, src, bg):
    im = Image.open(io.BytesIO(original_bytes)).convert("RGBA")
    w, h = im.size
    if kind == "icon":
        art = src.resize((w, h), Image.LANCZOS)
        return encode(art, original_bytes), (w, h)
    if kind == "foreground":
        inner = (max(1, int(w * 0.72)), max(1, int(h * 0.72)))
        art = src.resize(inner, Image.LANCZOS)
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        canvas.paste(art, ((w - inner[0]) // 2, (h - inner[1]) // 2), art)
        return encode(canvas, original_bytes), (w, h)
    if kind == "background":
        solid = Image.new("RGBA", (w, h), bg)
        return encode(solid, original_bytes), (w, h)
    raise ValueError(kind)


# --------------------------------------------------------------------------- main
ICON_RESOURCES = (
    ("mipmap/icon", "icon"),
    ("mipmap/ig_launcher_foreground", "foreground"),
    ("mipmap/ig_launcher_background", "background"),
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", default="apks/new.apk")
    ap.add_argument("--old", default="apks/decoded_old")
    ap.add_argument("--new", default="apks/decoded_new")
    ap.add_argument("--out", default="apks/new_themed_unsigned.apk")
    args = ap.parse_args()

    os.makedirs("report", exist_ok=True)

    old_colors = collect_colors(args.old)
    new_colors = collect_colors(args.new)
    log("old colors:", len(old_colors), "new colors:", len(new_colors))

    changed = {}
    for name, ov in old_colors.items():
        if name not in new_colors:
            continue
        o = resolve(old_colors, ov)
        n = resolve(new_colors, new_colors[name])
        if o and n and o.lower() != n.lower():
            changed[name] = (n.lower(), o.lower())
    log("colors that differ and can be ported:", len(changed))

    by_cur = {}
    for name, (cur, old) in changed.items():
        by_cur.setdefault(cur, {}).setdefault(old, []).append(name)
    pairs = []
    for cur, olds in by_cur.items():
        if len(olds) == 1:
            old = next(iter(olds))
            pairs.append((cur, old, olds[old]))
        else:
            log("  ambiguous current value", cur, "->", {k: v[:4] for k, v in olds.items()})

    src_path = best_old_icon(args.old)
    if not src_path:
        log("[error] no old launcher bitmap found")
        return 1
    src = Image.open(src_path).convert("RGBA")
    bg = dominant_color(src)
    log("old icon source:", src_path, src.size, "dominant bg:", bg)

    fmap = file_map(args.apk)
    zin = zipfile.ZipFile(args.apk)
    names = set(zin.namelist())

    replacements = {}
    for resname, kind in ICON_RESOURCES:
        entries = fmap.get(resname, [])
        log("resource", resname, "->", entries)
        if not entries:
            log("  [warn] not found in resource table")
            continue
        for cfg, path in entries:
            if not path.lower().endswith((".png", ".webp", ".jpg", ".jpeg")):
                log("  skip non-image", cfg, path)
                continue
            if path not in names:
                log("  [warn] zip entry missing", path)
                continue
            try:
                data, size = make_icon_replacement(zin.read(path), kind, src, bg)
            except Exception as exc:  # noqa: BLE001
                log("  [warn] could not rebuild", path, exc)
                continue
            replacements[path] = data
            log("  replaced", path, "(%s, %sx%s)" % (cfg, size[0], size[1]))

    removed = []
    patched_colors = 0
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zout:
        for info in zin.infolist():
            name = info.filename
            base = name.rsplit("/", 1)[-1].upper()
            if name.startswith("META-INF/") and base.endswith(
                (".SF", ".RSA", ".DSA", ".EC", "MANIFEST.MF")
            ):
                removed.append(name)
                continue
            data = zin.read(name)
            if name == "resources.arsc":
                for cur, old, owners in pairs:
                    data, count = swap_color(data, cur, old)
                    patched_colors += count
                    log("  arsc color", cur, "->", old, "occurrences:", count, owners[:6])
            elif name in replacements:
                data = replacements[name]
            zi = zipfile.ZipInfo(name, date_time=info.date_time)
            if name == "resources.arsc":
                zi.compress_type = zipfile.ZIP_STORED
            elif name in replacements:
                zi.compress_type = zipfile.ZIP_DEFLATED
            else:
                zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.internal_attr = info.internal_attr
            zi.create_system = info.create_system
            zout.writestr(zi, data)

    log("rewrote", args.out)
    log("icon entries replaced:", len(replacements))
    log("arsc colour values patched:", patched_colors)
    log("signature files dropped:", removed)

    with open(os.path.join("report", "replaced.json"), "w") as fh:
        json.dump(
            {
                "replaced": sorted(replacements),
                "removed": removed,
                "colors": {k: v for k, v in changed.items()},
                "patched_color_values": patched_colors,
            },
            fh,
            indent=1,
        )
    with open(os.path.join("report", "repackage-report.txt"), "w") as fh:
        fh.write("\n".join(LOG) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
