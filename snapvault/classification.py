"""Small deterministic content rules; no network or model calls."""
from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import unquote, urlsplit


CATEGORY_LABELS = {
    "tech": "技术",
    "read": "阅读",
    "shopping": "购物",
    "daily": "日常",
    "other": "其他",
}
CATEGORY_CODES = tuple(CATEGORY_LABELS)

# The original single category remains as migration history. New collection
# uses independent content types and these semantic, possibly overlapping tags.
CLASSIFICATION_VERSION = "setfit-bge-v1"
TOPIC_LABELS = {
    "tech": "技术开发", "learning": "学习资料", "design": "设计灵感",
    "receipts": "订单票据", "tasks": "日程事务", "chat": "聊天记录", "media": "影音娱乐",
}
LEGACY_TOPIC_LABELS = {
    "legacy_read": "阅读（原分类）", "legacy_shopping": "购物（原分类）",
    "legacy_daily": "日常（原分类）", "legacy_other": "其他（原分类）",
}
ALL_TOPIC_LABELS = {**TOPIC_LABELS, **LEGACY_TOPIC_LABELS}

_DOMAINS = {
    "tech": ("github.com", "gitlab.com", "gitee.com", "stackoverflow.com", "stackexchange.com",
             "docs.python.org", "developer.mozilla.org", "learn.microsoft.com", "developer.android.com",
             "pypi.org", "npmjs.com", "rust-lang.org", "dev.to", "juejin.cn", "csdn.net", "cnblogs.com"),
    "read": ("zhihu.com", "mp.weixin.qq.com", "medium.com", "substack.com", "arxiv.org",
             "wikipedia.org", "douban.com", "bbc.com", "reuters.com", "theguardian.com"),
    "shopping": ("taobao.com", "tmall.com", "jd.com", "pinduoduo.com", "yangkeduo.com",
                 "amazon.com", "amazon.cn", "amazon.co.uk", "ebay.com", "aliexpress.com", "temu.com"),
    "daily": ("12306.cn", "ctrip.com", "trip.com", "fliggy.com", "maps.google.com",
              "maps.baidu.com", "map.baidu.com", "map.qq.com", "calendar.google.com"),
}
_CHINESE_WORDS = {
    "tech": ("编程", "代码", "算法", "数据库", "服务器", "调试", "报错", "前端", "后端", "源码",
             "开发文档", "命令行", "机器学习", "人工智能", "软件开发", "接口文档"),
    "read": ("阅读", "文章", "论文", "读书", "书摘", "观点", "新闻", "教程", "章节", "学习资料", "摘要"),
    "shopping": ("购物", "订单", "快递", "物流", "优惠券", "下单", "商品", "购物车", "收货", "退款", "发票", "京东", "淘宝", "拼多多"),
    "daily": ("日程", "待办", "会议", "预约", "提醒", "账单", "缴费", "行程", "地址", "电话", "航班", "车票", "酒店", "报销", "生日", "天气", "打卡"),
}
_ENGLISH_WORDS = {
    "tech": re.compile(r"\b(?:python|javascript|typescript|sql|html|css|docker|kubernetes|linux|git|api|traceback|exception|debug|compiler)\b", re.I),
    "read": re.compile(r"\b(?:article|newsletter|chapter|journal|research paper|book review|reading notes)\b", re.I),
    "shopping": re.compile(r"\b(?:shopping|checkout|coupon|refund|delivery|receipt|tracking number|order number)\b", re.I),
    "daily": re.compile(r"\b(?:meeting|appointment|reminder|itinerary|birthday|weather|to-do|todo)\b", re.I),
}
_CODE = re.compile(
    r"(?m)^\s*(?:def\s+\w+\s*\(|class\s+\w+\s*[:(]|from\s+[\w.]+\s+import\s|import\s+[\w.]+|"
    r"(?:async\s+)?function\s*\w*\s*\(|(?:const|let|var)\s+\w+\s*=|"
    r"(?:pip3?|npm|pnpm|yarn)\s+(?:install|add|run)\b|git\s+(?:clone|commit|status|push|pull)\b|"
    r"docker\s+(?:run|build|compose)\b|(?:SELECT\b.+\bFROM|INSERT\s+INTO|CREATE\s+TABLE)\b)", re.I)
_CODE_FENCE = re.compile(r"(?m)^\s*```[^\n]*\n[\s\S]+?^\s*```")
_SCHEDULE = re.compile(
    r"(?<!\d)\d{4}[-/年](?:0?[1-9]|1[0-2])[-/月](?:0?[1-9]|[12]\d|3[01])(?:日)?(?!\d)|"
    r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)|"
    r"(?:周|星期)[一二三四五六日天](?:上午|下午|晚上|早上|中午)?[一二三四五六七八九十\d]{1,3}(?:点|时)")


def is_single_http_url(text: str) -> bool:
    """Recognize one complete HTTP(S) URL without normalizing the input.

    Surrounding whitespace, multiple URLs, control characters and invalid
    authorities remain plain text. URL queries and fragments are never changed.
    """
    if not isinstance(text, str) or not text or text != text.strip():
        return False
    if any(character.isspace() or unicodedata.category(character).startswith("C") for character in text):
        return False
    if any(character in text for character in ('<', '>', '"', '\\')):
        return False
    try:
        parsed = urlsplit(text)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            return False
        parsed.port  # Reject malformed/non-numeric and out-of-range ports.
        hostname = parsed.hostname
        if ":" not in hostname:  # urlsplit validates bracketed IPv6 literals.
            encoded = hostname.encode("idna").decode("ascii")
            if len(encoded) > 253 or any(not label or len(label) > 63 for label in encoded.rstrip(".").split(".")):
                return False
            if not re.fullmatch(r"[A-Za-z0-9.-]+", encoded):
                return False
            if any(label.startswith("-") or label.endswith("-") for label in encoded.rstrip(".").split(".")):
                return False
        return True
    except (ValueError, UnicodeError):
        return False


def classify_content(text: str = "", url: str = "") -> str:
    """Return one of CATEGORY_CODES; manual choices are handled by storage."""
    text = text if isinstance(text, str) else ""
    url = url if isinstance(url, str) else ""
    candidate = url if is_single_http_url(url) else (text if is_single_http_url(text) else "")
    if candidate:
        hostname = (urlsplit(candidate).hostname or "").casefold().rstrip(".")
        for category, domains in _DOMAINS.items():
            if any(hostname == domain or hostname.endswith("." + domain) for domain in domains):
                return category
    content = unicodedata.normalize("NFKC", unquote(text + ("\n" + url if url else ""))).casefold()
    if _CODE.search(content) or _CODE_FENCE.search(content):
        return "tech"
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            if isinstance(json.loads(stripped), (dict, list)):
                return "tech"
        except (ValueError, RecursionError):
            pass
    for category in ("tech", "shopping", "daily", "read"):
        if any(word in content for word in _CHINESE_WORDS[category]) or _ENGLISH_WORDS[category].search(content):
            return category
    if not candidate and _SCHEDULE.search(content):
        return "daily"
    if len("".join(content.split())) >= 200 and len(re.findall(r"[。！？]|[.!?](?:\s|$)", content)) >= 2:
        return "read"
    return "other"


def is_code_content(text: str = "") -> bool:
    """Typography follows actual code, never a semantic topic label."""
    if not isinstance(text, str):
        return False
    if _CODE.search(text) or _CODE_FENCE.search(text):
        return True
    if re.search(r"(?m)^\s*(?:[.#][\w:-]+|:root|@media)[^\n]*\{", text):
        return True
    return bool(re.search(r"(?m)^\s*(?:Traceback \(most recent call last\)|[\w.]+(?:Error|Exception):)", text))
