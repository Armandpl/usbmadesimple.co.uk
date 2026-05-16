#!/usr/bin/env python3
"""
Fetch usbmadesimple.co.uk from the Wayback Machine.

The site went offline in early 2025; the domain now serves a registrar
"for sale" page that the Wayback Machine kept on archiving. We walk the
CDX API, pick the latest capture of each URL strictly before --cutoff
(default 20250401, just after the last known-good capture on 20250319),
download each one with the `id_` modifier (raw bytes, no Wayback banner),
and write the tree to ./site so it browses locally.

Pure stdlib.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

HOST = "usbmadesimple.co.uk"
OUT_DIR = "site"
SLEEP_SECONDS = 0.5
CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_FETCH = "https://web.archive.org/web/{ts}id_/{url}"
UA = "usbms-archive-fetch/1.0 (+local mirror; pure stdlib)"


def http_get(url: str, *, retries: int = 5, timeout: int = 180) -> bytes:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    body = gzip.decompress(body)
                elif enc == "deflate":
                    body = zlib.decompress(body)
                return body
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            delay = min(2 ** attempt, 30)
            sys.stderr.write(f"  retry {attempt}/{retries} after {delay}s ({e})\n")
            time.sleep(delay)
    raise RuntimeError(f"GET failed for {url}: {last_err}")


JUNK_RE = re.compile(r"[*]|%5[Cc]|/web/\d{8,}")


def url_to_local_path(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    if host.endswith(":80"):
        host = host[:-3]
    if host not in (HOST, f"www.{HOST}"):
        return None
    path = parts.path or "/"
    if JUNK_RE.search(path):
        return None
    if parts.query:
        return None
    if path in ("/", ""):
        return "index.html"
    rel = path.lstrip("/")
    if rel.endswith("/"):
        rel += "index.html"
    rel = urllib.parse.unquote(rel)
    if ".." in rel.split("/"):
        return None
    return rel


def cdx_list(cutoff: str) -> list[dict]:
    """Return the latest capture (statuscode 200, timestamp < cutoff) per local path."""
    params = {
        "url": f"{HOST}/*",
        "output": "json",
        "filter": "statuscode:200",
        "to": cutoff,
    }
    qs = urllib.parse.urlencode(params)
    sys.stderr.write(f"CDX query: {CDX_URL}?{qs}\n")
    body = http_get(f"{CDX_URL}?{qs}")
    data = json.loads(body)
    if not data:
        return []
    header, rows = data[0], data[1:]
    idx = {name: i for i, name in enumerate(header)}
    latest: dict[str, dict] = {}
    for r in rows:
        original = r[idx["original"]]
        ts = r[idx["timestamp"]]
        mt = r[idx["mimetype"]]
        path = url_to_local_path(original)
        if path is None:
            continue
        prev = latest.get(path)
        if prev is None or ts > prev["timestamp"]:
            latest[path] = {"path": path, "url": original, "timestamp": ts, "mimetype": mt}
    return sorted(latest.values(), key=lambda x: x["path"])


HOST_RE = re.compile(
    r"https?://(?:web\.archive\.org/web/\d+[a-z_]*/)?(?:www\.)?usbmadesimple\.co\.uk",
    re.IGNORECASE,
)

# Constrain the body to a 1024px-wide 4:3-era viewport so the 2200×10
# background tile doesn't repeat horizontally on modern wide displays.
# `html{background:#fff}` stops the browser from propagating <body
# background="..."> to the canvas, which would otherwise paint the blue
# strip at viewport x=0 instead of body x=0 (misaligning the left nav).
VIEWPORT_STYLE = b"<style>html{background:#fff}body{max-width:1024px;margin:0 auto}</style>"
HEAD_CLOSE_RE = re.compile(rb"</head>", re.IGNORECASE)


TR_TAG_RE = re.compile(r"<(/?)tr\b[^>]*>", re.IGNORECASE)
AD_MARKER_RE = re.compile(r"ADVERTISEMENT", re.IGNORECASE)


def strip_ad_rows(text: str) -> str:
    """Remove every innermost <tr>...</tr> that contains an 'ADVERTISEMENT' marker.

    The site stamps the MQP Electronics promo at the bottom of each page inside
    a `<tr><td>...ADVERTISEMENT...<table>...</table></td></tr>` block. We pick
    the innermost `<tr>` enclosing the marker (largest start position among
    enclosing pairs) so we don't accidentally strip the entire content row.
    """
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for m in TR_TAG_RE.finditer(text):
        if not m.group(1):
            stack.append(m.start())
        elif stack:
            pairs.append((stack.pop(), m.end()))
    to_remove: set[tuple[int, int]] = set()
    for m in AD_MARKER_RE.finditer(text):
        p = m.start()
        containing = [(s, e) for s, e in pairs if s < p < e]
        if containing:
            to_remove.add(max(containing, key=lambda se: se[0]))
    for s, e in sorted(to_remove, key=lambda x: -x[0]):
        text = text[:s] + text[e:]
    return text


def rewrite_html(body: bytes) -> bytes:
    text = body.decode("utf-8", errors="replace")
    text = re.sub(
        r"<!--\s*FILE ARCHIVED ON.*?END WAYBACK TOOLBAR INSERT\s*-->",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"<script[^>]*src=[\"']https?://web\.archive\.org/[^\"']+[\"'][^>]*></script>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = HOST_RE.sub("", text)
    text = strip_ad_rows(text)
    body = text.encode("utf-8")
    body, _ = HEAD_CLOSE_RE.subn(VIEWPORT_STYLE + b"</head>", body, count=1)
    return body


def fetch(cutoff: str) -> None:
    rows = cdx_list(cutoff)
    sys.stderr.write(f"discovered {len(rows)} unique paths (cutoff {cutoff})\n")
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = []
    for i, row in enumerate(rows, 1):
        local = os.path.join(OUT_DIR, row["path"])
        os.makedirs(os.path.dirname(local) or OUT_DIR, exist_ok=True)
        sys.stderr.write(f"[{i}/{len(rows)}] {row['timestamp']} -> {row['path']}\n")
        if os.path.exists(local) and os.path.getsize(local) > 0:
            sys.stderr.write("  cached\n")
            manifest.append(row)
            continue
        wb_url = WAYBACK_FETCH.format(ts=row["timestamp"], url=row["url"])
        try:
            body = http_get(wb_url)
        except Exception as e:
            sys.stderr.write(f"  FAILED: {e}\n")
            continue
        if row["mimetype"] == "text/html" or local.endswith((".htm", ".html")):
            body = rewrite_html(body)
        with open(local, "wb") as f:
            f.write(body)
        manifest.append(row)
        time.sleep(SLEEP_SECONDS)
    with open(os.path.join(OUT_DIR, "_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    sys.stderr.write(f"done. {len(manifest)} files in {OUT_DIR}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cutoff", default="20250401")
    args = ap.parse_args()
    fetch(args.cutoff)
    return 0


if __name__ == "__main__":
    sys.exit(main())
