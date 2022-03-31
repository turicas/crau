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
