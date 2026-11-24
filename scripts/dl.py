#!/usr/bin/env python3
"""Download a Google Drive file, following the 'virus scan' confirmation form if
Drive decides to show it instead of the bytes."""
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

ENDPOINTS = [
    "https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t",
    "https://drive.google.com/uc?export=download&id={id}&confirm=t",
    "https://docs.google.com/uc?export=download&id={id}&confirm=t",
]


def opener_for():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept", "*/*")]
    return op


def looks_like_apk(data):
    return data[:2] == b"PK"


def needs_confirm(data):
    head = data[:6000].lower()
    return b"<form" in head and (b"confirm" in head or b"download" in head)


def follow_form(op, html):
    action = re.search(r'<form[^>]*?action="([^"]+)"', html, re.I)
    if not action:
        return None
    action = action.group(1).replace("&amp;", "&")
    params = {}
    for tag in re.findall(r"<input[^>]*>", html, re.I):
        name = re.search(r'name="([^"]*)"', tag, re.I)
        value = re.search(r'value="([^"]*)"', tag, re.I)
        if name:
            params[name.group(1)] = value.group(1) if value else ""
    print("  confirmation form params:", params, flush=True)
    if not action.startswith("http"):
        action = "https://drive.usercontent.google.com" + action
    url = action + ("&" if "?" in action else "?") + urllib.parse.urlencode(params)
    try:
        with op.open(url, timeout=900) as r:
            return r.read()
    except Exception as exc:  # noqa: BLE001
        print("  confirmation fetch failed:", exc, flush=True)
        return None


def describe(data):
    text = data[:3000].decode("utf-8", "ignore")
    title = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
    return "html title=%r" % (title.group(1).strip()[:200] if title else text[:300])


def fetch(op, url):
    try:
        with op.open(url, timeout=900) as r:
            print("  HTTP", r.status, "type=", r.headers.get("Content-Type"), "len=", r.headers.get("Content-Length"), flush=True)
            return r.read()
    except urllib.error.HTTPError as exc:
        print("  HTTPError", exc.code, exc.reason, flush=True)
        return exc.read() or b""
    except Exception as exc:  # noqa: BLE001
        print("  error:", exc, flush=True)
        return b""


def main():
    file_id, out = sys.argv[1], sys.argv[2]
    data = b""
    for tpl in ENDPOINTS:
        url = tpl.format(id=file_id)
        print("GET", url, flush=True)
        data = fetch(opener_for(), url)
        if looks_like_apk(data):
            break
        if needs_confirm(data):
            print("  drive confirmation page (%d bytes), following it" % len(data), flush=True)
            confirmed = follow_form(opener_for(), data.decode("utf-8", "ignore"))
            if confirmed and looks_like_apk(confirmed):
                data = confirmed
                break
        print("  not an APK:", describe(data) if data else "empty response", flush=True)
    if not looks_like_apk(data):
        raise SystemExit(
            "could not download a valid APK for id=%s (got %d bytes: %s)"
            % (file_id, len(data), describe(data) if data else "empty")
        )
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(data)
    print("wrote %s (%d bytes)" % (out, len(data)), flush=True)


if __name__ == "__main__":
    main()
