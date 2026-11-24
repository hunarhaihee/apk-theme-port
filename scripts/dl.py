#!/usr/bin/env python3
"""Download a Google Drive file, following the 'virus scan' confirmation form if
Drive decides to show it instead of the bytes."""
import os
import re
import sys
import urllib.parse
import urllib.request
import http.cookiejar

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def opener_for():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept", "*/*")]
    return op


def is_html(data):
    head = data[:4096].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<form" in head


def confirm_and_fetch(op, html):
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
    print("drive confirmation params:", params, flush=True)
    if not action.startswith("http"):
        action = "https://drive.usercontent.google.com" + action
    url = action + ("&" if "?" in action else "?") + urllib.parse.urlencode(params)
    with op.open(url, timeout=900) as r:
        return r.read()


def main():
    file_id, out = sys.argv[1], sys.argv[2]
    url = (
        "https://drive.usercontent.google.com/download?id=%s&export=download&confirm=t" % file_id
    )
    op = opener_for()
    print("GET", url, flush=True)
    with op.open(url, timeout=900) as r:
        data = r.read()
    if is_html(data):
        print("got drive HTML page (%d bytes), following confirmation form" % len(data), flush=True)
        data2 = confirm_and_fetch(op, data.decode("utf-8", "ignore"))
        if data2 is not None:
            data = data2
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(data)
    print("wrote %s (%d bytes), magic=%r" % (out, len(data), data[:4]), flush=True)
    if is_html(data):
        raise SystemExit("download still looks like HTML, not an APK")


if __name__ == "__main__":
    main()
