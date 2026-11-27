"""Copies of one news report count once and show once (engine 1.3.3). All items are test data."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("rapidfuzz")

from engine import assess, dupes  # noqa: E402
from engine_fixtures import ent, news, quake, snap, tag  # noqa: E402

TITLE = "New US sanctions on China over chip exports"


def china_news(raw_id: str, title: str = TITLE, *, age_h: float = 6.0, url: str | None = None):
    return replace(news(raw_id, tag("sanctions", 0.9, countries=["CHN"]), age_h=age_h, title=title), url=url)


def supplier():
    return ent("pearl", "supplier", name="Pearl River PCB Works", countries=["CHN"], criticality="high")


def test_title_key_ignores_case_accents_and_punctuation():
    assert dupes.title_key("Trump administration expands anti-cartel campaign!") == \
        dupes.title_key("TRUMP Administration expands anti cartel campaign") == \
        "trump administration expands anti cartel campaign"  # fmt: skip
    assert dupes.title_key("Sanktionen gegen Zölle in Peking") == dupes.title_key("sanktionen gegen zolle in peking")
    assert dupes.title_key("中国 出口 管制") == "中国 出口 管制"  # other scripts keep their words
    assert dupes.title_key("Market update") is None and dupes.title_key("") is None and dupes.title_key(None) is None


def test_url_key_drops_what_says_how_a_reader_arrived():
    base = "example.com/world/2026/11/27/sanctions-story"
    for u in ("https://www.example.com/world/2026/11/27/sanctions-story/",
              "http://example.com/world/2026/11/27/sanctions-story?utm_source=feed&utm_medium=rss#top",
              "https://EXAMPLE.com/world//2026/11/27/sanctions-story?fbclid=abc"):  # fmt: skip
        assert dupes.url_key(u) == base, u
    assert dupes.url_key("https://example.com/article?id=4&utm_campaign=x") == "example.com/article?id=4"
    assert dupes.url_key("https://example.com/article?id=4") != dupes.url_key("https://example.com/article?id=5")
    assert dupes.url_key("https://example.com/") is None  # a home page names no article
    assert dupes.url_key("mailto:desk@example.com") is None and dupes.url_key(None) is None


def test_only_news_is_merged():
    assert dupes.item_keys("gdelt", "news", TITLE, None) == (f"title:{dupes.title_key(TITLE)}",)
    assert dupes.item_keys("usgs", "hazard.earthquake", "M 4.5 - 10 km SW of Osaka, Japan", "https://e.test/q1") == ()
    assert dupes.item_keys("ofac", "sanction.designation", "OFAC SDN list: ACME", None) == ()
    assert dupes.item_keys("osv", "cyber.vulnerability", "Remote code execution in requests", None) == ()


def test_six_copies_with_one_title_count_once():
    one = assess([supplier()], snap([china_news("c0")]), base_currency=None)
    six = assess([supplier()], snap([china_news(f"c{i}", age_h=6 + i) for i in range(6)]), base_currency=None)
    assert len(one) == len(six) == 1
    [a] = six
    assert [ev.event_raw_id for ev in a.evidence] == ["c0"]  # the strongest copy: the most recent one
    assert a.likelihood == one[0].likelihood and a.score == one[0].score
    assert len(a.factors["likelihood"]["items"]) == 1 and a.factors["explain"]["item_count"] == 1
    assert a.explanation.startswith("A recent news report describes sanctions aimed at China")


def test_copies_by_url_and_copies_of_copies_are_one_item():
    items = [
        china_news("a", "US widens chip sanctions on China", url="https://news.example/sanctions-china?utm_source=x"),
        china_news("b", "Washington widens chip sanctions on China", url="https://www.news.example/sanctions-china/",
                   age_h=7),
        china_news("c", "Washington widens chip sanctions on China", url="https://other.example/story", age_h=8),
        china_news("d", "China faces wider US sanctions on its chip makers", age_h=9),  # another report
    ]  # fmt: skip
    [a] = assess([supplier()], snap(items), base_currency=None)
    assert [ev.event_raw_id for ev in a.evidence] == ["a", "d"]


def test_two_quakes_with_one_title_stay_two_items():
    site = ent("osaka", "site", name="Osaka plant", lat=34.69, lon=135.50, countries=["JPN"])
    q1, q2 = quake("q1", 34.7, 135.5, age_h=1), quake("q2", 34.71, 135.51, age_h=2)
    title = "M 5.0 - 3 km N of Osaka, Japan"
    [a] = assess([site], snap([replace(q1, title=title), replace(q2, title=title)]), base_currency=None)
    assert len(a.evidence) == 2
