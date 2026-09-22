from __future__ import annotations

from crau.browser import search as web_search

def test_redirect_links_are_decoded_and_ads_dropped():
    decode = web_search.decode_result_url
    assert (
        decode("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fone%3Fa%3D1&rut=x")
        == "https://example.org/one?a=1"
    )
    assert decode("https://duckduckgo.com/y.js?ad_provider=bing&u3=x") is None
    assert (
        decode("https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly9leGFtcGxlLm9yZy9vbmU_YT0x&ntb=1")
        == "https://example.org/one?a=1"
    )
    assert decode("https://example.com/direct") == "https://example.com/direct"
    broken = "https://www.bing.com/ck/a?u=a1%%%"
    assert decode(broken) == broken


def test_results_are_cleaned_deduplicated_limited_and_numbered():
    raw = [
        {"title": "  First\n result ", "url": "https://a.org/", "snippet": " some   text "},
        {"title": "Ad", "url": "https://duckduckgo.com/y.js?ad_provider=x", "snippet": ""},
        {"title": "Duplicate", "url": "https://a.org/", "snippet": ""},
        {"title": "Second", "url": "https://b.org/", "snippet": ""},
        {"title": "Third", "url": "https://c.org/", "snippet": ""},
    ]
    assert web_search.clean_results(raw, limit=2) == [
        {"position": 1, "title": "First result", "url": "https://a.org/", "snippet": "some text"},
        {"position": 2, "title": "Second", "url": "https://b.org/", "snippet": ""},
    ]
