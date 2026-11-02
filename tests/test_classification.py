import pytest

from snapvault.classification import CATEGORY_CODES, CATEGORY_LABELS, classify_content, is_single_http_url


@pytest.mark.parametrize("url", [
    "https://example.com/path?q=A%2FB&token=secret#section",
    "HTTP://localhost:8080/path", "https://127.0.0.1/", "https://[::1]:8443/",
    "https://例子.测试/路径?词=示例", "https://user:password@example.com/path",
])
def test_single_complete_http_urls(url):
    assert is_single_http_url(url)


@pytest.mark.parametrize("text", [
    "", "https://", "www.example.com", "https:// example.com", " https://example.com",
    "https://example.com\n", "https://one.test https://two.test", "https://one.test\nhttps://two.test",
    "Read https://example.com", "file:///C:/example.txt", "javascript:alert(1)", "ftp://example.com",
    "https://example.com:bad/", "https://example.com:99999/", "https://[broken]/",
    "https://example.com\\@other.test/", "https://example.com/<script>", "https://ex\u200bample.com",
])
def test_non_links_remain_plain_text(text):
    assert not is_single_http_url(text)


@pytest.mark.parametrize("text,category", [
    ("def greet(name):\n    return name", "tech"),
    ("数据库连接报错，需要调试", "tech"),
    ('{"tasks": [1, 2], "enabled": true}', "tech"),
    ('["one", "two"]', "tech"), ("{}", "tech"),
    ("```\nprint('hello')\n```", "tech"),
    ("请保存这张电子发票", "shopping"), ("Your tracking number is 123456", "shopping"),
    ("周五下午三点", "daily"), ("2026-11-01 14:30", "daily"),
    ("明天会议需要预约房间", "daily"), ("这篇文章值得阅读", "read"),
    (("风从远处的树林吹来，河流经过安静的村庄，街角的人们继续各自的生活。" * 9), "read"),
    ("好呀，下次见", "other"), ("x" * 250, "other"), ("[not valid JSON]", "other"),
])
def test_local_content_rules_and_conservative_fallback(text, category):
    assert classify_content(text) == category
    assert category in CATEGORY_LABELS


@pytest.mark.parametrize("url,category", [
    ("https://github.com/org/repo?tab=readme", "tech"),
    ("https://www.jd.com/item?code=123", "shopping"),
    ("https://mp.weixin.qq.com/s/abc", "read"),
    ("https://www.12306.cn/index.html", "daily"),
    ("https://github.com.evil.test/", "other"),
    ("https://not-taobao.com/", "other"),
    ("https://example.com/2026-11-01", "other"),
])
def test_url_rules_use_host_boundaries_without_visiting_pages(url, category, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Classification must not open a network connection")

    monkeypatch.setattr(socket, "socket", forbidden)
    assert classify_content(url, url) == category


def test_fixed_public_categories():
    assert CATEGORY_CODES == ("tech", "read", "shopping", "daily", "other")
    assert set(CATEGORY_LABELS) == set(CATEGORY_CODES)
