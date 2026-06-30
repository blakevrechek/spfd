"""
Scrape springfieldtogether.org for photos, board bios, partner mentions, and
event copy. Run this on your local machine because the sandbox proxy blocks
the Wix CDN.

Setup (one time):
    python3 -m pip install playwright
    python3 -m playwright install chromium

Run:
    python3 scrape_springfieldtogether.py

Output goes to ./spfd-scrape/:
    spfd-scrape/
      photos/         # every <img> and CSS background image, named by hash
      pages/          # raw rendered HTML per page
      screenshots/    # full-page screenshots per page
      text/           # innerText per page
      summary.json    # photo URLs, links, page titles, manifests

Zip the spfd-scrape/ folder and send it back so we can wire real photos into
the site.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Missing playwright. Install with:")
    print("  python3 -m pip install playwright")
    print("  python3 -m playwright install chromium")
    sys.exit(1)


BASE = "https://www.springfieldtogether.org/"
OUT_DIR = Path("spfd-scrape").resolve()
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Wix often serves resized variants. We try to fetch the largest available
# version by stripping the `/v1/fill/.../` resize transform when we find it.
WIX_RESIZE_RE = re.compile(r"/v1/(fill|crop|fit)/[^/]+/")


def slugify(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "page")[:maxlen]


def hash_name(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def normalize_wix_url(url: str) -> str:
    """If a Wix image URL has a resize transform, strip it to get the original."""
    if "wixstatic.com" not in url:
        return url
    stripped = WIX_RESIZE_RE.sub("/", url)
    return stripped


def safe_ext(url: str, content_type: str | None) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"):
        if path.lower().endswith(ext):
            return ext
    if content_type:
        if "jpeg" in content_type or "jpg" in content_type:
            return ".jpg"
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
        if "gif" in content_type:
            return ".gif"
        if "svg" in content_type:
            return ".svg"
    return ".bin"


def download(url: str, dest_dir: Path) -> dict[str, Any] | None:
    """Download a single image url; return record or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": BASE})
        with urllib.request.urlopen(req, timeout=20) as resp:
            ct = resp.headers.get("Content-Type", "")
            data = resp.read()
    except Exception as e:
        return {"url": url, "error": str(e)}

    name = hash_name(url) + safe_ext(url, ct)
    path = dest_dir / name
    path.write_bytes(data)
    return {"url": url, "saved": str(path.relative_to(OUT_DIR)), "bytes": len(data), "content_type": ct}


def scrape_page(playwright_page, page_url: str, label: str) -> dict[str, Any]:
    """Render one page, capture HTML/text/screenshot/image-urls."""
    print(f"\n→ {label}: {page_url}")
    playwright_page.goto(page_url, wait_until="networkidle", timeout=90_000)
    playwright_page.wait_for_timeout(1500)

    # Scroll to bottom to trigger Wix lazy-loaders
    for i in range(20):
        playwright_page.evaluate(f"window.scrollTo(0, {i * 600})")
        playwright_page.wait_for_timeout(180)
    playwright_page.evaluate("window.scrollTo(0, 0)")
    playwright_page.wait_for_timeout(800)

    title = playwright_page.title()

    html_path = OUT_DIR / "pages" / f"{label}.html"
    html_path.write_text(playwright_page.content(), encoding="utf-8")

    text_path = OUT_DIR / "text" / f"{label}.txt"
    text_path.write_text(playwright_page.inner_text("body"), encoding="utf-8")

    shot_path = OUT_DIR / "screenshots" / f"{label}.png"
    playwright_page.screenshot(path=str(shot_path), full_page=True)

    # Collect every image URL we can find
    images: list[str] = playwright_page.evaluate(
        """
        () => {
            const urls = new Set();
            document.querySelectorAll('img').forEach(img => {
                if (img.currentSrc) urls.add(img.currentSrc);
                if (img.src) urls.add(img.src);
                if (img.dataset.src) urls.add(img.dataset.src);
                (img.srcset || '').split(',').forEach(s => {
                    const u = s.trim().split(' ')[0];
                    if (u) urls.add(u);
                });
            });
            document.querySelectorAll('*').forEach(el => {
                const bg = getComputedStyle(el).backgroundImage;
                if (!bg || bg === 'none') return;
                const m = bg.match(/url\\("?([^")]+)"?\\)/);
                if (m) urls.add(m[1]);
            });
            return [...urls];
        }
        """
    )
    images = [u for u in images if u.startswith("http")]

    # Same-site links so we can decide what else to scrape
    links: list[dict[str, str]] = playwright_page.evaluate(
        """
        () => [...document.querySelectorAll('a[href]')]
            .map(a => ({
                text: (a.innerText || a.textContent || '').trim().slice(0, 100),
                href: a.href
            }))
            .filter(l => l.href && !l.href.startsWith('javascript'))
        """
    )

    return {
        "url": page_url,
        "label": label,
        "title": title,
        "image_urls": images,
        "links": links,
        "html_file": str(html_path.relative_to(OUT_DIR)),
        "text_file": str(text_path.relative_to(OUT_DIR)),
        "screenshot": str(shot_path.relative_to(OUT_DIR)),
    }


def main() -> int:
    for sub in ("photos", "pages", "screenshots", "text"):
        (OUT_DIR / sub).mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(90_000)

        # Start with home page; discover any other pages from its nav.
        all_pages: list[dict[str, Any]] = []
        home = scrape_page(page, BASE, "home")
        all_pages.append(home)

        # Find same-host links worth following (skip mailto, tel, anchors)
        seen = {BASE.rstrip("/")}
        candidates: list[tuple[str, str]] = []
        for link in home["links"]:
            href = link["href"].split("#")[0].rstrip("/")
            if not href or href in seen:
                continue
            host = urllib.parse.urlparse(href).hostname or ""
            if "springfieldtogether.org" not in host:
                continue
            text = link["text"] or "page"
            seen.add(href)
            candidates.append((href, text))

        # Cap at 12 subpages so this doesn't run forever
        for href, text in candidates[:12]:
            label = slugify(text) or hash_name(href)
            try:
                all_pages.append(scrape_page(page, href, label))
            except Exception as e:
                print(f"  ! failed {href}: {e}")

        browser.close()

    # Now download every unique image URL (with Wix-resize normalization)
    print("\nDownloading images…")
    seen_urls: dict[str, dict[str, Any]] = {}
    for p_info in all_pages:
        for u in p_info["image_urls"]:
            norm = normalize_wix_url(u)
            if norm in seen_urls:
                continue
            time.sleep(0.05)
            seen_urls[norm] = download(norm, OUT_DIR / "photos") or {"url": norm, "error": "skipped"}

    # Write the summary manifest
    summary = {
        "base": BASE,
        "pages": all_pages,
        "image_downloads": list(seen_urls.values()),
        "image_count": sum(1 for r in seen_urls.values() if "saved" in r),
        "image_errors": sum(1 for r in seen_urls.values() if "error" in r),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nDone. Output: {OUT_DIR}")
    print(f"  pages scraped: {len(all_pages)}")
    print(f"  images saved:  {summary['image_count']}")
    print(f"  image errors:  {summary['image_errors']}")
    print("\nZip the folder and send back:")
    print(f"  cd {OUT_DIR.parent} && zip -r spfd-scrape.zip {OUT_DIR.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
