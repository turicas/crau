from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin

import lxml.html

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
    ("other", "anchor", "//link[not(@rel = 'stylesheet')]/@href"),
]


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
    if scheme in ("javascript", "mailto", "data", "tel", "about"):
        return None

    full_url = urljoin(base_url, raw_url)
    defragged, _ = urldefrag(full_url)
    return defragged if defragged else None


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

    # 2. Extract CSS URLs from <style> blocks
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

    # 3. Extract CSS URLs from inline style="..." attributes
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
