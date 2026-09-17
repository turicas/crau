from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin

import lxml.html

from crau.utils import iri_to_uri

REGEXP_CSS_URL = re.compile(r"""url\(['"]?(.*?)['"]?\)""", re.IGNORECASE)

XPATH_EXTRACTORS = [
    # Media dependencies
    ("media", "dependency", "//img/@src"),
    ("media", "dependency", "//audio/@src"),
    ("media", "dependency", "//video/@src"),
    ("media", "dependency", "//source/@src"),
    ("media", "dependency", "//embed/@src"),
    ("media", "dependency", "//object/@data"),
    # Style dependencies
    ("css", "dependency", "//link[@rel = 'stylesheet']/@href"),
    # JS dependencies
    ("js", "dependency", "//script/@src"),
    # Anchors
    ("other", "anchor", "//a/@href"),
    ("other", "anchor", "//area/@href"),
    ("other", "anchor", "//iframe/@src"),
]

# <link rel="..."> values pointing to assets the page needs to render; every other rel
# (canonical, alternate, next, ...) is treated as an anchor
DEPENDENCY_LINK_RELS = {"icon", "apple-touch-icon", "apple-touch-icon-precomposed", "preload", "modulepreload", "prefetch", "manifest"}

SRCSET_XPATHS = ["//img/@srcset", "//source/@srcset"]


def parse_srcset(value: str) -> list[str]:
    """Return the URL candidates of a srcset attribute ("a.png 1x, b.png 2x" -> [a.png, b.png])."""
    urls = []
    for candidate in value.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        urls.append(candidate.split()[0])
    return urls


@dataclass(frozen=True)
class Resource:
    name: str
    type: str  # "link" | "code"
    link_type: str  # "dependency" | "anchor"
    url: str


def _clean_url(base_url: str, raw_url: str) -> str | None:
    raw_url = raw_url.strip()
    if not raw_url:
        return None
    scheme = raw_url.split(":", 1)[0].lower() if ":" in raw_url else ""
    if scheme and scheme not in ("http", "https"):
        # mailto:, javascript:, tel:, tg:, data:, about: ... cannot be archived
        return None

    full_url = urljoin(base_url, raw_url)
    defragged, _ = urldefrag(full_url)
    return iri_to_uri(defragged) if defragged else None


def extract_css_urls(base_url: str, css_content: str) -> list[str]:
    """Extract and normalize all URLs inside CSS content (url(...))."""
    found_urls = []
    for match in REGEXP_CSS_URL.finditer(css_content):
        raw_url = match.group(1).strip()
        cleaned = _clean_url(base_url, raw_url)
        if cleaned:
            found_urls.append(cleaned)
    return found_urls


def extract_resources(base_url: str, html_content: str) -> list[Resource]:
    """Extract media, stylesheets, scripts and anchors from HTML content."""
    if not html_content or not html_content.strip():
        return []

    try:
        root = lxml.html.fromstring(html_content)
    except Exception:
        return []

    resources: list[Resource] = []
    seen_urls: set[str] = set()

    # 1. Extract links via XPath
    for name, link_type, xpath in XPATH_EXTRACTORS:
        try:
            results = root.xpath(xpath)
        except Exception:
            continue

        for result in results:
            if isinstance(result, str):
                cleaned = _clean_url(base_url, result)
                if cleaned and cleaned not in seen_urls:
                    seen_urls.add(cleaned)
                    resources.append(
                        Resource(
                            name=name,
                            type="link",
                            link_type=link_type,
                            url=cleaned,
                        )
                    )

    # 2. <link rel=...> other than stylesheets: asset rels are dependencies, the rest anchors
    for link in root.xpath("//link[@href]"):
        rels = {rel.lower() for rel in (link.get("rel") or "").split()}
        if "stylesheet" in rels:
            continue
        cleaned = _clean_url(base_url, link.get("href") or "")
        if not cleaned or cleaned in seen_urls:
            continue
        seen_urls.add(cleaned)
        if rels & DEPENDENCY_LINK_RELS:
            resources.append(Resource(name="media", type="link", link_type="dependency", url=cleaned))
        else:
            resources.append(Resource(name="other", type="link", link_type="anchor", url=cleaned))

    # 3. Responsive image candidates (<img srcset>, <picture><source srcset>)
    for xpath in SRCSET_XPATHS:
        for srcset in root.xpath(xpath):
            if not isinstance(srcset, str):
                continue
            for raw_url in parse_srcset(srcset):
                cleaned = _clean_url(base_url, raw_url)
                if cleaned and cleaned not in seen_urls:
                    seen_urls.add(cleaned)
                    resources.append(Resource(name="media", type="link", link_type="dependency", url=cleaned))

    # 4. Extract CSS URLs from <style> blocks
    for style_text in root.xpath("//style/text()"):
        if isinstance(style_text, str):
            for css_url in extract_css_urls(base_url, style_text):
                if css_url not in seen_urls:
                    seen_urls.add(css_url)
                    resources.append(
                        Resource(
                            name="media",
                            type="link",
                            link_type="dependency",
                            url=css_url,
                        )
                    )

    # 5. Extract CSS URLs from inline style="..." attributes
    for inline_style in root.xpath("//*/@style"):
        if isinstance(inline_style, str):
            for css_url in extract_css_urls(base_url, inline_style):
                if css_url not in seen_urls:
                    seen_urls.add(css_url)
                    resources.append(
                        Resource(
                            name="media",
                            type="link",
                            link_type="dependency",
                            url=css_url,
                        )
                    )

    return resources
