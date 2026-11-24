#!/usr/bin/env python3
"""Minimal Android resources.arsc reader/writer used to recolour resources.

We only ever overwrite the 8-byte Res_value of an existing simple entry
(same size, no offsets move), so the table stays structurally valid.
"""
import struct

RES_STRING_POOL = 0x0001
RES_TABLE = 0x0002
RES_TABLE_PACKAGE = 0x0200
RES_TABLE_TYPE = 0x0201

TYPE_REFERENCE = 0x01
TYPE_INT_COLOR_ARGB8 = 0x1C
TYPE_INT_COLOR_RGB8 = 0x1D
COLOR_TYPES = (TYPE_INT_COLOR_ARGB8, TYPE_INT_COLOR_RGB8)


def normalize_hex(hex_value):
    """Old apk stores palette colours as RGB8, which apktool decodes as
    #00RRGGBB. Those are opaque colours, so drop the zero alpha."""
    value = hex_value.strip().lower()
    if len(value) == 9 and value[1:3] == "00":
        return "#ff" + value[3:]
    return value


def res_pool(data, off):
    try:
        ctype, header_size, _size = struct.unpack_from("<HHI", data, off)
    except struct.error:
        return None
    if ctype != RES_STRING_POOL:
        return None
    count, _styles, flags, strings_start, _ss = struct.unpack_from("<IIIII", data, off + 8)
    utf8 = bool(flags & 0x100)
    offsets = struct.unpack_from("<%dI" % count, data, off + header_size)
    base = off + strings_start

    def get(index):
        if index is None or index >= count:
            return None
        o = base + offsets[index]
        try:
            if utf8:
                n = data[o]
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | data[o + 1]
                    o += 2
                else:
                    o += 1
                n2 = data[o]
                if n2 & 0x80:
                    n2 = ((n2 & 0x7F) << 8) | data[o + 1]
                    o += 2
                else:
                    o += 1
                return data[o : o + n2].decode("utf-8", "replace")
            n = struct.unpack_from("<H", data, o)[0]
            o += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, o)[0]
                o += 2
            return data[o : o + n * 2].decode("utf-16-le", "replace")
        except (IndexError, struct.error):
            return None

    return get


def iter_entries(data, wanted=None):
    """Yield (resource_id, key_name, value_offset) for simple entries."""
    ctype, header_size, size = struct.unpack_from("<HHI", data, 0)
    if ctype != RES_TABLE:
        return
    end = min(len(data), size)
    p = header_size
    while p + 8 <= end:
        ctype, chs, csz = struct.unpack_from("<HHI", data, p)
        if csz < 8 or p + csz > end:
            break
        if ctype == RES_TABLE_PACKAGE:
            pkg_id = struct.unpack_from("<I", data, p + 8)[0]
            _ts, _lpt, key_strings, _lpk, _tio = struct.unpack_from(
                "<IIIII", data, p + 8 + 4 + 256
            )
            keys = res_pool(data, p + key_strings)
            q = p + chs
            while q + 8 <= p + csz:
                ttype, ths, tsz = struct.unpack_from("<HHI", data, q)
                if tsz < 8 or q + tsz > p + csz:
                    break
                if ttype == RES_TABLE_TYPE:
                    type_id = data[q + 8]
                    flags = data[q + 9]
                    entry_count, entries_start = struct.unpack_from("<II", data, q + 12)
                    base = q + entries_start
                    idx = q + ths
                    pairs = []
                    try:
                        if flags & 0x01:  # sparse entries
                            n = struct.unpack_from("<I", data, idx)[0]
                            for k in range(n):
                                ei, eo = struct.unpack_from("<HH", data, idx + 4 + k * 4)
                                pairs.append((ei, eo * 4))
                        elif flags & 0x02:  # compact / offset16
                            for k in range(entry_count):
                                eo = struct.unpack_from("<H", data, idx + k * 2)[0]
                                if eo != 0xFFFF:
                                    pairs.append((k, eo * 4))
                        else:
                            for k in range(entry_count):
                                eo = struct.unpack_from("<I", data, idx + k * 4)[0]
                                if eo != 0xFFFFFFFF:
                                    pairs.append((k, eo))
                    except struct.error:
                        pairs = []
                    for ei, eo in pairs:
                        e = base + eo
                        if e + 8 > q + tsz:
                            continue
                        try:
                            esize, eflags, ekey = struct.unpack_from("<HHI", data, e)
                        except struct.error:
                            continue
                        if eflags & 0x0001:
                            continue
                        voff = e + esize
                        if voff + 8 > q + tsz:
                            continue
                        rid = (pkg_id << 24) | (type_id << 16) | ei
                        if wanted is None or rid in wanted:
                            yield rid, (keys(ekey) if keys else None), voff
                q += tsz
        p += csz


def read_value(data, voff):
    return data[voff + 3], struct.unpack_from("<I", data, voff + 4)[0]


def write_color(data, voff, hex_value):
    """Write an opaque ARGB8 colour into a Res_value, keeping the 8-byte size.

    The source entries use several value types (ARGB8 / RGB8), and RGB8 has no
    alpha channel, which shows up as fully transparent (#00RRGGBB) when dumped.
    Palette colours are meant to be visible, so always emit ARGB8 with alpha FF.
    """
    value_str = normalize_hex(hex_value)
    value = int(value_str[1:], 16)
    if len(value_str) == 7:
        value |= 0xFF000000
    data[voff + 3] = TYPE_INT_COLOR_ARGB8
    struct.pack_into("<I", data, voff + 4, value)


def patch_colors(raw, targets, log):
    """targets: {resource_id: (name, old_hex)}. Returns (bytes, {name: count})."""
    data = bytearray(raw)
    counts = {}
    wanted = dict(targets)
    for _round in range(3):
        pending = set(wanted) - set(counts)
        if not pending:
            break
        refs = {}
        for rid, _name, voff in iter_entries(data, pending):
            dtype, val = read_value(data, voff)
            name, old = wanted[rid]
            if dtype in COLOR_TYPES:
                write_color(data, voff, old)
                counts[name] = counts.get(name, 0) + 1
            elif dtype == TYPE_REFERENCE:
                refs[val] = (name, old)
        for ref_id, (name, old) in refs.items():
            if ref_id not in wanted:
                log("  %s is a reference -> following to 0x%08x" % (name, ref_id))
                wanted[ref_id] = (name, old)
    return bytes(data), counts
