from accession.parse import parse_feed, parse_html, parse_sitemap
from accession.urls import in_scope, is_asset, looks_like_pagination, normalize, parse_domain_input


def test_normalize_basics():
    assert normalize("HTTP://Example.COM:80/a/../b/./c?b=2&a=1#frag") == "http://example.com/b/c?a=1&b=2"
    assert normalize("https://example.com") == "https://example.com/"
    assert normalize("https://example.com:443/x/") == "https://example.com/x/"
    assert normalize("https://example.com:8443/x") == "https://example.com:8443/x"


def test_normalize_drops_tracking_and_keeps_hashbang():
    assert normalize("https://e.com/p?utm_source=x&id=3&fbclid=abc") == "https://e.com/p?id=3"
    assert normalize("https://e.com/app#!/inbox") == "https://e.com/app#!/inbox"
    assert normalize("https://e.com/doc#section-2") == "https://e.com/doc"


def test_normalize_relative_and_rejects():
    assert normalize("../about", base="https://e.com/blog/post") == "https://e.com/about"
    assert normalize("//cdn.e.com/x", base="https://e.com/") == "https://cdn.e.com/x"
    for bad in ("mailto:a@b.c", "javascript:void(0)", "tel:123", "ftp://e.com/x", ""):
        assert normalize(bad, base="https://e.com/") is None


def test_normalize_percent_encoding():
    assert normalize("https://e.com/%7Euser/a%2fb") == "https://e.com/~user/a%2Fb"
    assert normalize("https://e.com/café menu") == "https://e.com/caf%C3%A9%20menu"


def test_scope():
    assert in_scope("https://www.e.com/x", "e.com", False)
    assert in_scope("https://e.com/x", "www.e.com", False)
    assert not in_scope("https://blog.e.com/x", "e.com", False)
    assert in_scope("https://blog.e.com/x", "e.com", True)
    assert not in_scope("https://note.com/x", "e.com", True)


def test_helpers():
    assert is_asset("https://e.com/a/logo.PNG") and not is_asset("https://e.com/report.pdf")
    assert looks_like_pagination("https://e.com/blog/page/3/")
    assert looks_like_pagination("https://e.com/list?page=2")
    assert parse_domain_input("www.Example.com/blog") == ("example.com", "https://www.example.com/blog")


def test_parse_html_sources():
    info = parse_html("""<html><head><title> Hello
        world </title><link rel="canonical" href="/c"><link rel="next" href="/p2">
        <link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>
        <body><a href="/a">a</a><a rel="next" href="/p2">next</a><area href="/map">
        <meta http-equiv="refresh" content="0; url=/moved"></body></html>""")
    sources = {(l.url, l.source) for l in info.links}
    assert info.title == "Hello world" and info.canonical == "/c"
    assert {("/c", "canonical"), ("/p2", "pagination"), ("/feed.xml", "feed"), ("/a", "link"),
            ("/map", "link"), ("/moved", "meta-refresh")} <= sources


def test_parse_sitemap_index_urlset_and_text():
    idx = parse_sitemap(b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        b"<sitemap><loc>https://e.com/s1.xml</loc></sitemap></sitemapindex>")
    assert idx.sitemaps == ["https://e.com/s1.xml"] and not idx.pages
    us = parse_sitemap(b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc> https://e.com/a </loc>'
                       b"<lastmod>2026-01-02</lastmod></url></urlset>")
    assert us.pages == [("https://e.com/a", "2026-01-02")]
    import gzip

    assert parse_sitemap(gzip.compress(b"https://e.com/x\nhttps://e.com/y\n")).pages == [
        ("https://e.com/x", None), ("https://e.com/y", None)]


def test_parse_feeds():
    rss = b'<rss version="2.0"><channel><item><link>https://e.com/1</link></item></channel></rss>'
    atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link rel="alternate" href="https://e.com/2"/>'
            b'<link rel="edit" href="https://e.com/edit"/></entry></feed>')
    assert parse_feed(rss) == ["https://e.com/1"]
    assert parse_feed(atom) == ["https://e.com/2"]
