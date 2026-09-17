from crau.utils import resource_matches_base_url


def test_resource_matches_base_url_empty_allowed_list():
    urls = [
        "https://url.com",
        "https://example.com",
        "https://net.br/path",
    ]
    allowed = []
    for url in urls:
        assert resource_matches_base_url(url, allowed) is True


def test_resource_matches_base_url_allowed_is_domain():
    urls = [
        ("https://url.com", False),
        ("https://example.com", True),
        ("https://example.com/anything", True),
        ("https://facebook.com/example.com", False),
        ("https://net.br/path", False),
    ]
    allowed = ["example.com"]
    for url, match_ in urls:
        assert resource_matches_base_url(url, allowed) is match_, url


def test_resource_matches_base_url_allowed_is_domain_with_scheme():
    urls = [
        ("https://url.com", False),
        ("https://example.com", True),
        ("http://example.com/anything", True),
        ("https://facebook.com/example.com", False),
        ("https://net.br/path", False),
    ]
    allowed = ["http://example.com"]
    for url, match_ in urls:
        assert resource_matches_base_url(url, allowed) is match_, url

    allowed = ["https://example.com"]
    for url, match_ in urls:
        assert resource_matches_base_url(url, allowed) is match_, url


def test_resource_matches_base_url_allowed_domain_with_path():
    urls = [
        ("https://url.com", False),
        ("https://example.com", False),
        ("https://example.com/path", True),
        ("https://example.com/path/root", True),
        ("https://example.com/anything", False),
        ("https://facebook.com/example.com", False),
        ("https://net.br/path", False),
    ]
    allowed = ["example.com/path"]
    for url, match_ in urls:
        assert resource_matches_base_url(url, allowed) is match_, url


def test_resource_matches_base_url_miscelaneous_allowed():
    urls = [
        ("https://url.com", False),
        ("https://example.com", False),
        ("https://example.com/path", True),
        ("https://example.com/path/root", True),
        ("https://example.com/anything", False),
        ("https://facebook.com/example.com", False),
        ("https://www.twitter.com/profile/username", True),
        ("https://www.twitter.com/example.com", False),
        ("https://net.br/path", False),
    ]
    allowed = ["example.com/path", "twitter.com/profile"]
    for url, match_ in urls:
        assert resource_matches_base_url(url, allowed) is match_, url


def test_iri_to_uri_quotes_non_ascii_and_spaces():
    from crau.utils import iri_to_uri

    assert iri_to_uri("http://x.com/página 1?q=ação#frag") == "http://x.com/p%C3%A1gina%201?q=a%C3%A7%C3%A3o#frag"


def test_iri_to_uri_is_idempotent_and_keeps_reserved_chars():
    from crau.utils import iri_to_uri

    url = "https://example.com/a%20b/c;v=1?x=1&y=2+3&z=%C3%A7"
    assert iri_to_uri(url) == url
    assert iri_to_uri(iri_to_uri("http://x.com/página")) == "http://x.com/p%C3%A1gina"


def test_iri_to_uri_encodes_idna_host_and_keeps_port():
    from crau.utils import iri_to_uri

    assert iri_to_uri("http://münchen.de:8080/x") == "http://xn--mnchen-3ya.de:8080/x"


def test_decode_body_uses_content_type_charset():
    from crau.utils import decode_body

    body = "Informação pública".encode("iso-8859-1")
    text = decode_body(body, [(b"Content-Type", b"text/html; charset=ISO-8859-1")])
    assert text == "Informação pública"


def test_decode_body_sniffs_meta_charset_when_header_is_missing():
    from crau.utils import decode_body

    html = '<html><head><meta charset="windows-1252"></head><body>Ação</body></html>'
    text = decode_body(html.encode("cp1252"), [(b"Content-Type", b"text/html")])
    assert "Ação" in text

    html5 = '<html><head><meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1"></head>çã</html>'
    assert "çã" in decode_body(html5.encode("latin-1"), [])


def test_decode_body_defaults_to_utf8_and_handles_bom_and_gzip():
    import codecs
    import gzip

    from crau.utils import decode_body

    assert decode_body("ação".encode("utf-8"), []) == "ação"
    assert decode_body(codecs.BOM_UTF8 + "ação".encode("utf-8"), []) == "ação"
    compressed = gzip.compress("<p>ação</p>".encode("utf-8"))
    assert decode_body(compressed, [(b"content-encoding", b"gzip")]) == "<p>ação</p>"


def test_decode_body_invalid_declared_charset_falls_back_without_raising():
    from crau.utils import decode_body

    text = decode_body(b"\xfd\xfc\xfbabc", [(b"Content-Type", b"text/html; charset=not-a-charset")])
    assert text.endswith("abc")
