from crau.extractor import extract_css_urls, extract_resources


def test_extract_resources_dependencies_and_anchors():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <link rel="stylesheet" href="/assets/style.css">
        <link rel="icon" href="/favicon.ico">
        <script src="/static/app.js"></script>
        <style>
            body { background: url('/images/bg.png'); }
        </style>
    </head>
    <body style="background-image: url('images/body.jpg')">
        <img src="/img/logo.png" alt="Logo">
        <video src="media/video.mp4"></video>
        <a href="/about-us#team">About</a>
        <a href="https://other.org/external">External</a>
        <iframe src="/embedded/frame.html"></iframe>
    </body>
    </html>
    """
    base_url = "https://example.com/site/"
    resources = extract_resources(base_url, html)

    # Check dependencies (CSS, JS, images, video)
    deps = [r for r in resources if r.link_type == "dependency"]
    dep_urls = {r.url for r in deps}

    assert "https://example.com/assets/style.css" in dep_urls
    assert "https://example.com/static/app.js" in dep_urls
    assert "https://example.com/img/logo.png" in dep_urls
    assert "https://example.com/site/media/video.mp4" in dep_urls
    assert "https://example.com/images/bg.png" in dep_urls
    assert "https://example.com/site/images/body.jpg" in dep_urls

    # Check anchors
    anchors = [r for r in resources if r.link_type == "anchor"]
    anchor_urls = {r.url for r in anchors}

    # Fragment #team must be stripped
    assert "https://example.com/about-us" in anchor_urls
    assert "https://other.org/external" in anchor_urls
    assert "https://example.com/embedded/frame.html" in anchor_urls


def test_extract_css_urls():
    css = """
    @font-face {
        src: url("../fonts/font.woff2") format("woff2"),
             url('/fonts/font.ttf');
    }
    .hero {
        background-image: url('https://cdn.example.com/hero.jpg');
    }
    """
    base_url = "https://example.com/css/main.css"
    urls = extract_css_urls(base_url, css)

    assert "https://example.com/fonts/font.woff2" in urls
    assert "https://example.com/fonts/font.ttf" in urls
    assert "https://cdn.example.com/hero.jpg" in urls


def test_extract_resources_emits_ascii_safe_urls():
    html = '<a href="/página 1">a</a><img src="/img/ação.png">'
    resources = extract_resources("http://x.com/", html)
    urls = {r.url for r in resources}
    assert urls == {"http://x.com/p%C3%A1gina%201", "http://x.com/img/a%C3%A7%C3%A3o.png"}
    for url in urls:
        url.encode("ascii")


def test_extract_resources_srcset_and_picture_sources():
    html = """
    <img src="/a.png" srcset="/a-1x.png 1x, /a-2x.png 2x">
    <picture>
        <source srcset="/wide.webp 800w,/narrow.webp 400w" type="image/webp">
        <img src="/fallback.jpg">
    </picture>
    """
    urls = {r.url for r in extract_resources("http://x.com/", html) if r.link_type == "dependency"}
    assert urls == {
        "http://x.com/a.png",
        "http://x.com/a-1x.png",
        "http://x.com/a-2x.png",
        "http://x.com/wide.webp",
        "http://x.com/narrow.webp",
        "http://x.com/fallback.jpg",
    }


def test_extract_resources_icon_and_preload_links_are_dependencies():
    html = """
    <link rel="icon" href="/favicon.ico">
    <link rel="Shortcut Icon" href="/favicon2.ico">
    <link rel="apple-touch-icon" href="/touch.png">
    <link rel="preload" as="font" href="/f.woff2">
    <link rel="manifest" href="/site.webmanifest">
    <link rel="canonical" href="/canonical">
    <link rel="alternate" href="/feed.xml">
    """
    resources = extract_resources("http://x.com/", html)
    by_type = {}
    for res in resources:
        by_type.setdefault(res.link_type, set()).add(res.url)
    assert by_type["dependency"] == {
        "http://x.com/favicon.ico",
        "http://x.com/favicon2.ico",
        "http://x.com/touch.png",
        "http://x.com/f.woff2",
        "http://x.com/site.webmanifest",
    }
    assert by_type["anchor"] == {"http://x.com/canonical", "http://x.com/feed.xml"}


def test_extract_resources_only_keeps_http_schemes():
    html = """
    <a href="tg://resolve?domain=brasil_io">t</a><a href="mailto:a@b.c">m</a>
    <a href="javascript:void(0)">j</a><a href="tel:123">t</a><a href="ftp://x/y">f</a>
    <a href="//cdn.example.com/lib.js">protocol-relative</a><a href="/ok">ok</a>
    """
    urls = {r.url for r in extract_resources("https://brasil.io/", html)}
    assert urls == {"https://cdn.example.com/lib.js", "https://brasil.io/ok"}
