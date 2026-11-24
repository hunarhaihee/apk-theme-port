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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arsc  # noqa: E402

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


def public_color_ids(decoded_new):
    ids = {}
    path = os.path.join(decoded_new, "res", "values", "public.xml")
    if not os.path.exists(path):
        log("[warn] no public.xml in decoded tree")
        return ids
    try:
        root = ET.parse(path).getroot()
    except Exception:  # noqa: BLE001
        return ids
    for ch in root:
        if ch.tag.split("}")[-1] == "public" and ch.get("type") == "color":
            try:
                ids[ch.get("name")] = int(ch.get("id"), 16)
            except (TypeError, ValueError):
                continue
    return ids


def swap_color(data, cur_hex, new_hex):
    """Fallback: replace every Res_value whose payload equals cur_hex."""
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
    """Old artwork as the adaptive background (full bleed) + empty foreground.

    The old icon is a full-bleed square, so letting the launcher mask the
    background layer reproduces it best; an inset foreground would show it small
    on a coloured plate instead.
    """
    im = Image.open(io.BytesIO(original_bytes)).convert("RGBA")
    w, h = im.size
    if kind == "icon":
        art = src.resize((w, h), Image.LANCZOS)
        return encode(art, original_bytes), (w, h)
    if kind == "foreground":
        return encode(Image.new("RGBA", (w, h), (0, 0, 0, 0)), original_bytes), (w, h)
    if kind == "background":
        square = src
        side = max(src.size)
        if src.size != (side, side):
            square = Image.new("RGBA", (side, side), bg)
            square.paste(src, ((side - src.width) // 2, (side - src.height) // 2), src)
        art = square.resize((w, h), Image.LANCZOS)
        return encode(art, original_bytes), (w, h)
    raise ValueError(kind)


def write_preview(src, legacy, fg, bg):
    tiles = []
    for label, im in (("old", src), ("legacy", legacy), ("background", bg), ("foreground", fg)):
        if im is None:
            continue
        tile = im.convert("RGBA")
        tile.thumbnail((192, 192), Image.LANCZOS)
        canvas = Image.new("RGBA", (192, 192), (40, 40, 40, 255))
        canvas.paste(tile, ((192 - tile.width) // 2, (192 - tile.height) // 2), tile)
        tiles.append((label, canvas))
    if not tiles:
        return
    sheet = Image.new("RGBA", (192 * len(tiles), 192), (40, 40, 40, 255))
    for i, (_label, canvas) in enumerate(tiles):
        sheet.paste(canvas, (192 * i, 0))
    os.makedirs("report", exist_ok=True)
    sheet.save(os.path.join("report", "icon-preview.png"))
    log("icon preview tiles:", [t[0] for t in tiles])


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

    ids = public_color_ids(args.new)
    targets = {}
    for name, (_cur, old) in changed.items():
        rid = ids.get(name)
        if rid is None:
            log("  no resource id for", name)
            continue
        targets[rid] = (name, old)
    log("palette entries with a resource id:", len(targets))

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
    preview = {"legacy": None, "foreground": None, "background": None}
    for resname, kind in ICON_RESOURCES:
        entries = fmap.get(resname, [])
        log("resource", resname, "->", entries)
        if not entries:
            log("  [warn] not found in resource table")
            continue
        best = None
        best_area = -1
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
            if size[0] * size[1] > best_area:
                best_area = size[0] * size[1]
                best = Image.open(io.BytesIO(data)).convert("RGBA")
        preview[kind] = best

    removed = []
    patched_counts = {}
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
                data, patched_counts = arsc.patch_colors(data, targets, log)
                for nm, c in sorted(patched_counts.items()):
                    log("  patched color", nm, "entries:", c)
                missing = [t[0] for rid, t in targets.items() if t[0] not in patched_counts]
                if missing:
                    log("  not found by id, byte-pattern fallback:", missing[:10])
                    for rid, (nm, old) in targets.items():
                        if nm not in missing:
                            continue
                        data, extra = swap_color(data, changed[nm][0], old)
                        if extra:
                            log("    fallback patched", nm, "occurrences:", extra)
                            patched_counts[nm] = extra
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
    log("palette colours patched:", len(patched_counts))
    log("signature files dropped:", removed)

    write_preview(src, preview.get("legacy"), preview.get("foreground"), preview.get("background"))

    expected = {nm: old for nm, old in ((t[0], t[1]) for t in targets.values()) if nm in patched_counts}
    with open(os.path.join("report", "expected-colors.json"), "w") as fh:
        json.dump(expected, fh, indent=1)
    with open(os.path.join("report", "replaced.json"), "w") as fh:
        json.dump(
            {
                "replaced": sorted(replacements),
                "removed": removed,
                "colors": changed,
                "patched": patched_counts,
            },
            fh,
            indent=1,
        )
    with open(os.path.join("report", "repackage-report.txt"), "w") as fh:
        fh.write("\n".join(LOG) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
