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
