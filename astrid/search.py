"""Web search (Brave) and plain page reading, so she can look things up and read on her own."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from .net import session

log = logging.getLogger("astrid.search")

# Pages that are video, feeds or login walls: nothing to read.
_SKIP_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com", "x.com", "twitter.com",
               "pinterest.", "linkedin.com", "vimeo.com", "spotify.com", "apple.com/music", "soundcloud.com",
               # video-only sites: nothing to read
               "pornhub.", "xvideos.", "xnxx.", "xhamster.", "redtube.", "youporn.", "spankbang.", "eporner.")
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
_TAGS = re.compile(r"<[^>]+>")


class SearchError(Exception):
    pass


class Brave:
    def __init__(self, api_key: str, base_url: str = "https://api.search.brave.com/res/v1"):
        self._key = api_key
        self._base = base_url.rstrip("/")

    def _search(self, query: str, count: int) -> list[dict]:
        try:
            r = session().get(
                self._base + "/web/search",
                params={"q": query, "count": count, "safesearch": "off", "text_decorations": "false"},
                headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
                timeout=15,
            )
        except requests.RequestException as e:
            raise SearchError(f"network {type(e).__name__}") from None
        if r.status_code != 200:
            raise SearchError(f"HTTP {r.status_code}: {r.text[:160]}")
        out = []
        for item in ((r.json().get("web") or {}).get("results") or []):
            url = item.get("url") or ""
            if url.startswith("http"):
                out.append({
                    "title": html.unescape(_TAGS.sub("", item.get("title") or ""))[:200],
                    "url": url,
                    "snippet": html.unescape(_TAGS.sub("", item.get("description") or ""))[:400],
                })
        return out

    async def search(self, query: str, count: int = 8) -> list[dict]:
        return await asyncio.to_thread(self._search, query, count)


class _Text(HTMLParser):
    """Collects readable text: paragraphs, headings, list items, quotes. Skips scripts, menus and footers."""
    SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button", "select", "iframe"}
    BLOCK = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "pre", "td", "dd", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.block = 0
        self.title = ""
        self._in_title = False
        self.blocks: list[str] = []
        self.loose: list[str] = []
        self._cur: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.block += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK and self.block:
            self.block -= 1
            if not self.block:
                text = " ".join("".join(self._cur).split())
                if len(text) > 30:
                    self.blocks.append(text)
                self._cur = []

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self.skip:
            return
        elif self.block:
            self._cur.append(data)
        else:
            self.loose.append(data)


def extract_text(page: str, max_chars: int = 7000) -> tuple[str, str]:
    """(title, readable text) from raw HTML."""
    p = _Text()
    try:
        p.feed(page)
    except Exception:
        pass
    text = "\n".join(p.blocks)
    if len(text) < 400:  # pages without much paragraph markup: take the loose text too
        text = (text + "\n" + " ".join(" ".join(p.loose).split())).strip()
    return " ".join(p.title.split())[:200], text[:max_chars]


def readable(url: str) -> bool:
    host = urlparse(url).netloc.lower() + urlparse(url).path.lower()
    return not any(s in host for s in _SKIP_HOSTS) and not url.lower().endswith(".pdf")


def _fetch(url: str, max_chars: int) -> tuple[str, str] | None:
    try:
        r = session().get(url, headers={"User-Agent": _UA, "Accept": "text/html"}, timeout=15)
    except requests.RequestException as e:
        log.info("could not read %s (%s)", url, type(e).__name__)
        return None
    if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "html"):
        log.info("could not read %s (HTTP %s)", url, r.status_code)
        return None
    title, text = extract_text(r.text[:600_000], max_chars)
    if len(text) < 300:  # age gate, paywall or a page with nothing on it
        log.info("nothing readable at %s", url)
        return None
    return title, text


async def fetch(url: str, max_chars: int = 7000) -> tuple[str, str] | None:
    return await asyncio.to_thread(_fetch, url, max_chars)
