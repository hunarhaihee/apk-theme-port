#!/usr/bin/env python3
"""Port the OLD launcher icon + visual theme onto the NEW decoded APK.

Hard rule: never add or remove *resource entries*. Only file bodies and existing
values entries are rewritten, so the resource id map stays identical and the
pristine classes.dex from the new APK keeps working unchanged.
"""
import glob
import os
import re
import shutil
from xml.etree import ElementTree as ET

ET.register_namespace("android", "http://schemas.android.com/apk/res/android")
A = "{http://schemas.android.com/apk/res/android}"

OLD = "apks/decoded_old"
NEW = "apks/decoded_new"
REPORT = "report"

LOG = []
DENSITY = {"ldpi": 0.75, "mdpi": 1.0, "hdpi": 1.5, "xhdpi": 2.0, "xxhdpi": 3.0, "xxxhdpi": 4.0}
DENSITY_ORDER = ["xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi", "ldpi"]


def say(*parts):
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    LOG.append(line)


def save_log():
    os.makedirs(REPORT, exist_ok=True)
    with open(os.path.join(REPORT, "port-report.txt"), "w") as fh:
        fh.write("\n".join(LOG) + "\n")


def parse(path):
    try:
        return ET.parse(path).getroot()
    except Exception as exc:  # noqa: BLE001
        say("[warn] cannot parse", path, exc)
        return None


def write_xml(root, path):
    try:
        ET.indent(root, space="    ")
    except Exception:  # noqa: BLE001
        pass
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------- res helpers
def res_dirs(root, rtype):
    dirs = sorted(glob.glob(os.path.join(root, "res", rtype + "-*")))
    plain = os.path.join(root, "res", rtype)
    if os.path.isdir(plain):
        dirs.insert(0, plain)
    return dirs


def res_files(root, ref):
    m = re.match(r"^@(\w+)/([\w.]+)$", ref or "")
    if not m:
        return []
    rtype, name = m.group(1), m.group(2)
    found = []
    for d in res_dirs(root, rtype):
        for fn in sorted(os.listdir(d)):
            base, _ext = os.path.splitext(fn)
            if base == name:
                found.append(os.path.join(d, fn))
    return found


def qualifier(path):
    d = os.path.basename(os.path.dirname(path))
    return d.split("-", 1)[1] if "-" in d else ""


def density_of(path):
    for part in qualifier(path).split("-"):
        if part in DENSITY:
            return DENSITY[part]
    return None


def write_image(im, dst):
    ext = os.path.splitext(dst)[1].lower()
    if ext == ".webp":
        im.save(dst, "WEBP", lossless=True, quality=100)
    elif ext == ".png":
        im.save(dst, "PNG", optimize=True)
    elif ext in (".jpg", ".jpeg"):
        im.convert("RGB").save(dst, "JPEG", quality=95)
    else:
        return False
    return True


def copy_asset(src, dst, inset=None):
    """Copy/convert src into dst, resizing when density buckets differ.

    inset: fraction of the target size the artwork should occupy (centred).
    """
    se = os.path.splitext(src)[1].lower()
    de = os.path.splitext(dst)[1].lower()
    sd, dd = density_of(src), density_of(dst)
    same_size = sd is not None and dd is not None and abs(sd - dd) < 1e-6
    if se == de and same_size and inset is None:
        shutil.copyfile(src, dst)
        return True
    try:
        from PIL import Image
    except ImportError:
        if se == de:
            shutil.copyfile(src, dst)
            return True
        return False
    im = Image.open(src).convert("RGBA")
    target = None
    if sd and dd:
        target = (max(1, int(round(im.width * dd / sd))), max(1, int(round(im.height * dd / sd))))
    if inset is not None:
        base = target or im.size
        inner = (max(1, int(base[0] * inset)), max(1, int(base[1] * inset)))
        im = im.resize(inner, Image.LANCZOS)
        canvas = Image.new("RGBA", base, (0, 0, 0, 0))
        canvas.paste(im, ((base[0] - inner[0]) // 2, (base[1] - inner[1]) // 2), im)
        im = canvas
    elif target:
        im = im.resize(target, Image.LANCZOS)
    return write_image(im, dst)


def best_bitmap(root, ref):
    files = [f for f in res_files(root, ref) if not f.lower().endswith(".xml")]
    if not files:
        return None

    def key(f):
        d = density_of(f)
        return d if d else 1.0

    return max(files, key=key)


def port_bitmaps(old_ref, new_ref):
    old = {qualifier(f): f for f in res_files(OLD, old_ref) if not f.lower().endswith(".xml")}
    if not old:
        say("[warn] old has no bitmaps for", old_ref)
        return 0
    targets = [f for f in res_files(NEW, new_ref) if not f.lower().endswith(".xml")]
    if not targets:
        say("  no bitmap slots for", new_ref)
        return 0
    done = 0
    for f in targets:
        src = old.get(qualifier(f))
        if src is None:
            src = best_bitmap(OLD, old_ref)
        if src and copy_asset(src, f):
            done += 1
            say("  icon", os.path.relpath(f, NEW), "<-", os.path.relpath(src, OLD))
    return done


def adaptive_xml(root, ref):
    for f in res_files(root, ref):
        if f.lower().endswith(".xml"):
            return f
    return None


def layers(xml_path):
    out = {}
    root = parse(xml_path)
    if root is None:
        return out
    for child in root:
        tag = child.tag.split("}")[-1]
        val = child.get(A + "drawable")
        if val:
            out[tag] = val
    return out


def dominant_color(im):
    small = im.convert("RGBA").resize((32, 32)).convert("RGB")
    try:
        q = small.quantize(colors=8, method=0)
        palette = q.getpalette()[: 8 * 3]
        counts = sorted(q.getcolors(), reverse=True)
        idx = counts[0][1]
        r, g, b = palette[idx * 3 : idx * 3 + 3]
        return "#%02x%02x%02x" % (r, g, b)
    except Exception:  # noqa: BLE001
        return None


def fill_adaptive_from_legacy(old_ref, new_xml):
    """NEW has an adaptive icon but OLD does not: paint OLD artwork into the layers."""
    new_layers = layers(new_xml)
    say("  adaptive layers to fill:", new_layers)
    src = best_bitmap(OLD, old_ref)
    if not src:
        say("  [warn] no legacy bitmap to build adaptive layers from")
        return 0
    try:
        from PIL import Image

        base_im = Image.open(src).convert("RGBA")
        bg = dominant_color(base_im)
    except Exception:  # noqa: BLE001
        bg = None
    done = 0
    for tag, ref in new_layers.items():
        for f in res_files(NEW, ref):
            if f.lower().endswith(".xml"):
                if tag == "background" and bg:
                    body = (
                        '<?xml version="1.0" encoding="utf-8"?>\n'
                        '<shape xmlns:android="http://schemas.android.com/apk/res/android"'
                        ' android:shape="rectangle">\n    <solid android:color="%s" />\n</shape>\n' % bg
                    )
                    if "<vector" in open(f, encoding="utf-8", errors="ignore").read():
                        continue
                    with open(f, "w") as fh:
                        fh.write(body)
                    done += 1
                    say("  adaptive bg", os.path.relpath(f, NEW), "->", bg)
                continue
            inset = 0.7 if tag in ("foreground", "monochrome") else 1.0
            if copy_asset(src, f, inset=inset):
                done += 1
                say("  adaptive layer", tag, os.path.relpath(f, NEW), "<-", os.path.relpath(src, OLD))
    return done


# --------------------------------------------------------------------------- icon
def manifest_icon(root, key):
    app = root.find("application") if root is not None else None
    return app.get(A + key) if app is not None else None


def port_icons():
    old_man = parse(os.path.join(OLD, "AndroidManifest.xml"))
    new_man = parse(os.path.join(NEW, "AndroidManifest.xml"))
    if old_man is None or new_man is None:
        say("[error] manifest missing")
        return 0
    say("manifest icon      old=%r new=%r" % (manifest_icon(old_man, "icon"), manifest_icon(new_man, "icon")))
    say("manifest roundIcon old=%r new=%r" % (manifest_icon(old_man, "roundIcon"), manifest_icon(new_man, "roundIcon")))

    total = 0
    for key in ("icon", "roundIcon"):
        new_ref = manifest_icon(new_man, key)
        if not new_ref:
            continue
        old_ref = manifest_icon(old_man, key) or manifest_icon(old_man, "icon")
        if not old_ref:
            continue
        say("porting", key, old_ref, "->", new_ref)
        total += port_bitmaps(old_ref, new_ref)
        new_xml = adaptive_xml(NEW, new_ref)
        old_xml = adaptive_xml(OLD, old_ref)
        if new_xml and old_xml:
            old_layers, new_layers = layers(old_xml), layers(new_xml)
            say("  adaptive old layers", old_layers)
            say("  adaptive new layers", new_layers)
            tree = parse(old_xml)
            if tree is not None:
                for child in list(tree):
                    tag = child.tag.split("}")[-1]
                    if tag in new_layers:
                        child.set(A + "drawable", new_layers[tag])
                    else:
                        tree.remove(child)
                        say("  dropped layer", tag)
                write_xml(tree, new_xml)
                say("  wrote adaptive xml", os.path.relpath(new_xml, NEW))
            for tag, nref in new_layers.items():
                oref = old_layers.get(tag)
                if oref:
                    total += port_bitmaps(oref, nref)
        elif new_xml and not old_xml:
            total += fill_adaptive_from_legacy(old_ref, new_xml)
    return total


# --------------------------------------------------------------------------- theme
VISUAL_ITEMS = {
    "colorPrimary",
    "colorPrimaryDark",
    "colorPrimaryVariant",
    "colorSecondary",
    "colorSecondaryVariant",
    "colorAccent",
    "colorOnPrimary",
    "colorOnSecondary",
    "colorSurface",
    "colorBackground",
    "colorControlNormal",
    "colorControlActivated",
    "colorControlHighlight",
    "windowBackground",
    "statusBarColor",
    "navigationBarColor",
    "android:colorPrimary",
    "android:colorPrimaryDark",
    "android:colorAccent",
    "android:colorControlNormal",
    "android:colorControlActivated",
    "android:colorControlHighlight",
    "android:colorBackground",
    "android:windowBackground",
    "android:statusBarColor",
    "android:navigationBarColor",
    "android:textColorPrimary",
    "android:textColorSecondary",
    "android:textColorHint",
    "android:textColorTertiary",
    "android:windowLightStatusBar",
    "android:windowLightNavigationBar",
    "android:windowDrawsSystemBarBackgrounds",
}


def collect_values(root):
    colors, styles = {}, {}
    for d in sorted(glob.glob(os.path.join(root, "res", "values*"))):
        for f in sorted(glob.glob(os.path.join(d, "*.xml"))):
            r = parse(f)
            if r is None or r.tag != "resources":
                continue
            for ch in r:
                tag = ch.tag.split("}")[-1]
                name = ch.get("name")
                if not name:
                    continue
                if tag == "color":
                    colors.setdefault(name, (ch.text or "").strip())
                elif tag == "style":
                    bucket = styles.setdefault(name, {})
                    for item in ch:
                        iname = item.get("name")
                        if iname:
                            bucket.setdefault(iname, (item.text or "").strip())
    return colors, styles


def resolve_color(colors, value, depth=0):
    if not value or depth > 6:
        return None
    value = value.strip()
    if value.startswith("#"):
        return value
    m = re.match(r"^@color/([\w.]+)$", value)
    if m:
        return resolve_color(colors, colors.get(m.group(1)), depth + 1)
    return None


def copy_referenced(ref):
    m = re.match(r"^@(\w+)/([\w.]+)$", ref)
    if not m or m.group(1) not in ("drawable", "mipmap"):
        return 0
    copied = 0
    for nf in res_files(NEW, ref):
        if not nf.lower().endswith((".xml", ".png", ".webp", ".jpg", ".jpeg")):
            continue
        src = {qualifier(f): f for f in res_files(OLD, ref)}.get(qualifier(nf))
        if src and os.path.splitext(src)[1].lower() == os.path.splitext(nf)[1].lower():
            if copy_asset(src, nf):
                copied += 1
                say("  theme asset", os.path.relpath(nf, NEW), "<-", os.path.relpath(src, OLD))
    return copied


def port_theme():
    old_colors, old_styles = collect_values(OLD)
    say("old colors:", len(old_colors), "old styles:", len(old_styles))
    changed = 0
    for f in sorted(glob.glob(os.path.join(NEW, "res", "values*", "*.xml"))):
        r = parse(f)
        if r is None or r.tag != "resources":
            continue
        dirty = False
        for ch in r:
            tag = ch.tag.split("}")[-1]
            name = ch.get("name")
            if not name:
                continue
            if tag == "color" and name in old_colors:
                newv = resolve_color(old_colors, old_colors[name]) or old_colors[name]
                if newv and (ch.text or "").strip() != newv:
                    ch.text = newv
                    dirty = True
                    changed += 1
                    say("  color", name, "->", newv)
            elif tag == "style" and name in old_styles:
                existing = {it.get("name"): it for it in ch if it.get("name")}
                for iname, ival in old_styles[name].items():
                    if iname not in VISUAL_ITEMS:
                        continue
                    if iname not in existing and not iname.startswith("android:"):
                        continue
                    val = ival
                    if val.startswith("@android:"):
                        pass
                    elif val.startswith("@color/"):
                        resolved = resolve_color(old_colors, val)
                        if not resolved:
                            continue
                        val = resolved
                    elif val.startswith("@"):
                        if not res_files(NEW, val):
                            say("  skip missing res", name, iname, val)
                            continue
                        copy_referenced(val)
                    if iname in existing:
                        if (existing[iname].text or "") != val:
                            existing[iname].text = val
                            dirty = True
                            changed += 1
                            say("  style", name, iname, "->", val)
                    else:
                        item = ET.SubElement(ch, "item")
                        item.set("name", iname)
                        item.text = val
                        dirty = True
                        changed += 1
                        say("  style+", name, iname, "->", val)
        if dirty:
            write_xml(r, f)
    return changed


# --------------------------------------------------------------------------- alias repair
PATHY = re.compile(r"^(?:res/)?([a-z][a-z\-]*)/([^/]+?)(?:\.(?:xml|png|webp|jpg|jpeg|9\.png))?$")


def normalize_aliases():
    """apktool can emit value aliases as bare paths; aapt2 needs @type/name refs."""
    fixed = 0
    for f in sorted(glob.glob(os.path.join(NEW, "res", "values*", "*.xml"))):
        r = parse(f)
        if r is None or r.tag != "resources":
            continue
        dirty = False
        for ch in r:
            rtype = ch.get("type")
            if not rtype:
                continue
            val = (ch.text or "").strip()
            if not val or val.startswith(("@", "?", "#")):
                continue
            m = PATHY.match(val)
            if m and m.group(1) == rtype:
                ch.text = "@%s/%s" % (rtype, m.group(2))
                dirty = True
                fixed += 1
        if dirty:
            write_xml(r, f)
            say("  normalized aliases in", os.path.relpath(f, NEW))
    say("alias entries normalized:", fixed)


# --------------------------------------------------------------------------- recon
def recon():
    lines = []
    for label, root in (("OLD", OLD), ("NEW", NEW)):
        man = parse(os.path.join(root, "AndroidManifest.xml"))
        lines.append("=== %s manifest: icon=%r roundIcon=%r" % (label, manifest_icon(man, "icon"), manifest_icon(man, "roundIcon")))
        lines.append("=== %s mipmap/drawable dirs" % label)
        dirs = [os.path.basename(d) for d in glob.glob(os.path.join(root, "res", "mipmap*")) + glob.glob(os.path.join(root, "res", "drawable*"))]
        lines.append(" ".join(sorted(dirs)))
        lines.append("=== %s icon files" % label)
        for d in sorted(glob.glob(os.path.join(root, "res", "*"))):
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                low = fn.lower()
                if ("icon" in low or "launcher" in low) and os.path.getsize(os.path.join(d, fn)) < 400000:
                    lines.append("  %s/%s (%d bytes)" % (os.path.basename(d), fn, os.path.getsize(os.path.join(d, fn))))
    for rel in ("res/values/layouts.xml", "res/values/layouts.xml"):
        pass
    for rel in ("res/values/layouts.xml",):
        p = os.path.join(NEW, rel)
        if os.path.exists(p):
            txt = open(p, encoding="utf-8", errors="ignore").read().splitlines()
            lines.append("=== NEW %s (%d lines) ===" % (rel, len(txt)))
            lines.extend(txt[:25])
        p = os.path.join(OLD, rel)
        if os.path.exists(p):
            txt = open(p, encoding="utf-8", errors="ignore").read().splitlines()
            lines.append("=== OLD %s (%d lines) ===" % (rel, len(txt)))
            lines.extend(txt[:10])
    os.makedirs(REPORT, exist_ok=True)
    with open(os.path.join(REPORT, "icon-recon.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    os.makedirs(REPORT, exist_ok=True)
    recon()
    icons = port_icons()
    say("icon files ported:", icons)
    theme = port_theme()
    say("theme entries ported:", theme)
    normalize_aliases()
    save_log()
    if icons == 0:
        say("[warn] no icon file was replaced")


if __name__ == "__main__":
    main()
