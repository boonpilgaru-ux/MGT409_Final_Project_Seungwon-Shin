"""Option photos (app/images.py) against mocked Wikipedia / Wikimedia Commons APIs and a fake
photo judge. Fully offline."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app import images
from app.errors import LLMError

COMMONS = "commons.wikimedia.org"


# --------------------------------------------------------------------------- fake Wikimedia


def thumb(name: str, width: int = 960) -> str:
    slug = name.replace(" ", "_")
    return f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{slug}/{width}px-{slug}"


def article(title: str, image: str | None = None, *, width: int = 1200, height: int = 1000,
            description: str = "", disambiguation: bool = False, english: str | None = None) -> dict:
    """A formatversion=2 page as prop=pageimages|pageprops|description|langlinks returns it."""
    page: dict[str, Any] = {"pageid": abs(hash(title)) % 10_000, "ns": 0, "title": title}
    if image:
        page["pageimage"] = image.replace(" ", "_")
        page["thumbnail"] = {"source": thumb(image), "width": min(width, 960), "height": height * min(width, 960) // width}
        page["original"] = {"source": thumb(image, width), "width": width, "height": height}
    if description:
        page["description"] = description
    if disambiguation:
        page["pageprops"] = {"disambiguation": ""}
    if english:
        page["langlinks"] = [{"lang": "en", "title": english}]
    return page


def commons_file(title: str, *, width: int = 2000, height: int = 1800, mime: str = "image/jpeg",
                 description: str = "", categories: str = "", artist: str = "Kim") -> dict:
    name = title.replace(" ", "_")
    return {
        "title": f"File:{title}.jpg",
        "imageinfo": [{
            "url": f"https://upload.wikimedia.org/wikipedia/commons/a/aa/{name}.jpg",
            "thumburl": f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/aa/{name}.jpg/960px-{name}.jpg",
            "width": width,
            "height": height,
            "mime": mime,
            "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}.jpg",
            "extmetadata": {
                "Artist": {"value": f'<a href="//commons.wikimedia.org/wiki/User:{artist}">{artist}</a>'},
                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                "ImageDescription": {"value": description},
                "Categories": {"value": categories},
            },
        }],
    }


class Wikimedia:
    """Mock transport for <lang>.wikipedia.org and commons.wikimedia.org; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.articles: dict[tuple[str, str], dict] = {}  # (lang, title) -> page
        self.redirects: dict[tuple[str, str], str] = {}  # (lang, from) -> to
        self.searches: dict[tuple[str, str], list[str]] = {}  # (lang, query) -> article titles
        self.commons: dict[str, list[dict]] = {}  # Commons search query -> files
        self.file_meta: dict[str, dict] = {}  # file name -> extmetadata (for lead images)
        self.override: Callable[[httpx.Request], Any] | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            result = self.override(request)
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
        params = request.url.params
        if request.url.host == COMMONS:
            if "gsrsearch" in params:
                query = params["gsrsearch"].removesuffix(" filetype:bitmap")
                files = [{**f, "index": i + 1} for i, f in enumerate(self.commons.get(query, []))]
                return httpx.Response(200, json={"query": {"pages": files}} if files else {"batchcomplete": True})
            pages = []
            for title in params["titles"].split("|"):
                name = title.removeprefix("File:").replace(" ", "_")
                meta = self.file_meta.get(name)
                pages.append({"title": title.replace("_", " "), "imageinfo": [{"extmetadata": meta}]} if meta
                             else {"title": title, "missing": True})
            return httpx.Response(200, json={"query": {"pages": pages}})
        lang = request.url.host.split(".")[0]
        if "gsrsearch" in params:
            titles = self.searches.get((lang, params["gsrsearch"]), [])
            pages = [{**self.articles[(lang, t)], "index": i + 1} for i, t in enumerate(titles)]
            return httpx.Response(200, json={"query": {"pages": pages}} if pages else {"batchcomplete": True})
        pages, redirects = [], []
        for title in params["titles"].split("|"):
            target = self.redirects.get((lang, title), title)
            if target != title:
                redirects.append({"from": title, "to": target})
            pages.append(self.articles.get((lang, target), {"ns": 0, "title": target, "missing": True}))
        return httpx.Response(200, json={"query": {"redirects": redirects, "pages": pages}})

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]

    def commons_queries(self) -> list[str]:
        return [r.url.params["gsrsearch"] for r in self.requests if r.url.host == COMMONS and "gsrsearch" in r.url.params]


@pytest.fixture
def wiki(monkeypatch) -> Wikimedia:
    w = Wikimedia()
    images.clear_cache()
    monkeypatch.setattr(images, "_transport", httpx.MockTransport(w.handle))
    yield w
    images.clear_cache()


CANDIDATE_RE = re.compile(r"^\s+([ab]\d+): (.*)$", re.MULTILINE)


def judge_by(*rules: tuple[str, str], default: str = "no") -> Callable[[Any], dict[str, str]]:
    """A fake judge: each candidate gets the verdict of the first rule whose needle is in its text."""

    def handler(call) -> dict[str, str]:
        assert call.schema == dict[str, Any] and call.model == images.JUDGE_MODEL
        return {
            cid: next((verdict for needle, verdict in rules if needle in text), default)
            for cid, text in CANDIDATE_RE.findall(call.user)
        }

    return handler


def judge_calls(fake_llm) -> list[Any]:
    return [c for c in fake_llm.calls if c.model == images.JUDGE_MODEL]


def spec(label: str, query: str, sublabel: str | None = None, wiki: str | None = None, **kw: Any) -> images.PhotoSpec:
    lang, _, title = (wiki or "").partition(":")
    return images.PhotoSpec(label=label, query=query, sublabel=sublabel, wiki_lang=lang or None,
                            wiki_title=title or None, **kw)


def korean_market(wiki: Wikimedia) -> tuple[images.PhotoSpec, images.PhotoSpec]:
    """Two Korean cards whose articles both have a lead image."""
    wiki.articles[("ko", "빈대떡")] = article("빈대떡", "Bindae-tteok.jpg", description="녹두 부침개")
    wiki.articles[("ko", "떡볶이")] = article("떡볶이", "Tteokbokki.JPG", description="한국의 길거리 음식")
    wiki.file_meta["Bindae-tteok.jpg"] = {"Artist": {"value": "Lee"}, "LicenseShortName": {"value": "CC BY 2.0"},
                                         "ImageDescription": {"value": "Mung bean pancakes"},
                                         "Categories": {"value": "Bindae-tteok|Taken with Nikon D40"}}
    wiki.file_meta["Tteokbokki.JPG"] = {"Artist": {"value": "Park"}, "LicenseShortName": {"value": "CC0"}}
    return (
        spec("Bindaetteok at Gwangjang Market", "bindaetteok", "광장시장 빈대떡", "ko:빈대떡"),
        spec("Tteokbokki from a pojangmacha", "tteokbokki", "포장마차 떡볶이", "ko:떡볶이"),
    )


# --------------------------------------------------------------------------- query analysis (Commons tier)


def test_terms_required_proper_nouns_and_fallbacks():
    t = images._terms("bowl of jjajangmyeon")
    assert t.tokens == ("jjajangmyeon",) and t.required == {"jjajangmyeon"}
    assert t.fallbacks == (("jjajangmyeon", images.MIN_RELEVANCE),)

    t = images._terms("Gwangjang Market bindaetteok")
    assert t.required == {"gwangjang", "market"} and t.head == "bindaetteok"
    assert t.fallbacks[0] == ("Gwangjang Market", images.MIN_RELEVANCE)  # multi-word proper name

    t = images._terms("Copacabana Beach")  # all Title Case: only the leading subject is required
    assert t.required == {"copacabana"}

    t = images._terms("Brazilian embroidery hoop")  # a lone nationality word is never a fallback
    assert [q for q, _ in t.fallbacks] == ["embroidery hoop"]

    # a single proper noun is never a fallback query ("Showa" alone finds Emperor Showa)
    assert [q for q, _ in images._terms("Showa kissaten").fallbacks] == ["kissaten"]
    assert [q for q, _ in images._terms("Seoul neighborhood street").fallbacks] == ["neighborhood street"]

    assert [q for q, _ in images._terms("pojangmacha tent").fallbacks] == ["pojangmacha"]


def test_relevance_matching_rules():
    t = images._terms("Han River park")
    assert images._relevance(t, "Puerto Princesa Subterranean River National Park") == 0  # "han" missing
    assert images._relevance(t, "Banpo Hangang Park") == 0  # "han" must be a whole word (short token)
    assert images._relevance(t, "Han River at dusk") == pytest.approx(2 / 3)
    # romanization drift and plurals still match; descriptions can't supply proper nouns
    assert images._relevance(images._terms("jjajangmyeon"), "Jajangmyeon 1") == 1
    assert images._relevance(images._terms("cassette tapes"), "Old cassette tape") == 1
    t = images._terms("Myeongdong street food")
    assert images._relevance(t, "Munich market stall", "Myeongdong street food") == 0


def test_head_noun_must_match_unless_a_multi_word_name_does():
    assert images._relevance(images._terms("Showa kissaten"), "Emperor Showa") == 0
    assert images._relevance(images._terms("Showa kissaten"), "Showa-era kissaten in Kanda") == 1
    assert images._relevance(images._terms("triangle kimbap"), "Triangle file.486") == 0
    assert images._relevance(images._terms("sticker sheet"), "FreeBSD Sticker") == 0
    assert images._relevance(images._terms("chest emblem"), "Old chest", "a carved emblem") == 1  # description counts
    assert images._relevance(images._terms("Gwangjang Market bindaetteok"), "Gwangjang Market 01") == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    ("query", "title", "detail"),
    [
        ("chin chin snack box", "Breakfast for lunch in the Boon Trunk Elephant Snack Box", ""),
        ("leather school satchel", "Satchel Paige seated next to bleachers", "at a school game"),
        ("coral gele headwrap", "Osun Osogbo", "women in gele headwrap"),
        ("Lagos street art", "Castiglione del Lago street art", ""),
    ],
)
def test_first_word_and_whole_proper_nouns_must_match(query, title, detail):
    assert images._relevance(images._terms(query), title, detail) == 0


def test_photos_naming_the_players_country_rank_first():
    qianjiang = images._parse_wikimedia({"query": {"pages": [commons_file("Han River (Qianjiang)")]}})
    seoul = images._parse_wikimedia({"query": {"pages": [
        {**commons_file("Seoul-Han.River.at.night", description="The Han River in Seoul, South Korea"), "index": 2},
    ]}})
    candidates = qianjiang + seoul
    assert images._best(candidates, images._terms("Han River picnic speaker")).title == "Han River (Qianjiang)"
    korean = images._terms("Han River picnic speaker", prefer=("korea", "korean"))
    assert images._best(candidates, korean).title == "Seoul-Han.River.at.night"
    by_script = images._terms("Han River picnic speaker", prefer=("korea", "한강 피크닉"))
    described = images._parse_wikimedia({"query": {"pages": [
        commons_file("Han River (Qianjiang)", description="汉江潜江段"),
        {**commons_file("Seoul-Han.River.at.night-01", description="여의도동, 서울"), "index": 2},
    ]}})
    assert images._best(described, by_script).title == "Seoul-Han.River.at.night-01"
    assert images._mentions_country(images._terms("street tacos", prefer=("mexico",)), "Mexican street tacos")
    assert not images._mentions_country(images._terms("x", prefer=("nigeria",)), "Niger River boats")


# --------------------------------------------------------------------------- the word blocklist (first gate)


@pytest.mark.parametrize(
    "title",
    [
        "WW2 Norway. German Luftwaffe uniforms Generaloberst Breast chest eagle Brustadler emblem",
        "Jongno-gu memorial for Seoul Halloween crowd crush victims street",
        "M. Venkaiah Naidu witnessing the signing ceremony of an MoU with Amar Chitra Katha comic",
        "Coat, U.S. Cold Weather Field Jacket, 82nd Airborne emblem",
        "Iglesia San Dieguito",
        "Nau toran temple, Navatorana mandir",
        "Fans wave a baseball cheering towel - DVIDS 7890123",
        "Royal Navy warship",
        "EFTA00001934 - Hand in a black glove holds open a blue spiral-bound notebook",
        "Tawang Monastery, Arunachal Pradesh",
        "Merkava4m-Windbreaker-0036a (cropped-02) Merkava Mk 4m main battle tank with Trophy",
        "Woodland Plantation Plaquemine Parish - Slave Cabin and Sugar Kettle",
        "Edward VIII with his staff wearing Happi 1922 Edward, Prince of Wales, bought the coat",
        "Mysore Dasara procession",
    ],
)
def test_blocklisted_words_are_sensitive(title):
    assert images._is_sensitive(title)


@pytest.mark.parametrize(
    "title", ["Emperor penguin colony", "Swiss Army knife", "Sugar skull", "Riot grrrl zine", "Orange Crush soda",
              "Churchill Downs", "Tea ceremony", "Navy blue tote bag", "Temple Street night market",
              "Shirley Temple mocktail", "Nike Air Force 1 sneakers", "Chiba Lotte Marines cheering",
              "Islamabad street food", "Ribbed tank top", "Monarch butterfly wings", "Slavic folk embroidery"],
)
def test_harmless_lookalikes_are_not_sensitive(title):
    assert not images._is_sensitive(title)


# --------------------------------------------------------------------------- tier 1 parsing


def test_lead_image_candidate_rules():
    ok = images._lead_candidate(article("빈대떡", "Bindae-tteok.jpg", description="부침개"), "ko", 0, False)
    assert ok is not None and ok.tier == 1 and ok.source == "Wikipedia"
    assert ok.source_url == "https://commons.wikimedia.org/wiki/File:Bindae-tteok.jpg"
    assert ok.article == "ko:빈대떡 (부침개)" and ok.url == thumb("Bindae-tteok.jpg")
    skip = [
        article("Adire", "Adire.jpg", disambiguation=True),  # disambiguation page
        article("Kia Tigers", "Kia Tigers logo.svg"),  # a logo, and the card isn't one
        article("Busan", "Busan location map.png"),
        article("Tiny", "Tiny.jpg", width=300, height=300),  # too small
        article("Skyline", "Skyline.jpg", width=4000, height=900),  # panorama
        article("Scan", "Old scan.tif"),
        {"ns": 0, "title": "Nothing", "missing": True},
        article("No image"),
    ]
    assert [images._lead_candidate(page, "en", 0, False) for page in skip] == [None] * len(skip)
    # a card that IS a crest may show the crest
    assert images._lead_candidate(article("Kia Tigers", "Kia Tigers logo.svg"), "ko", 0, True) is not None
    assert images._card_kind_ok(spec("Classic club crest", "football club crest"))
    assert not images._card_kind_ok(spec("Tigers orange jersey", "baseball jersey"))


def test_titles_resolve_through_normalization_and_redirects():
    data = {"query": {
        "normalized": [{"from": "jajangmyeon", "to": "Jajangmyeon"}],
        "redirects": [{"from": "Jajangmyeon", "to": "Jjajangmyeon"}],
        "pages": [{"title": "Jjajangmyeon", "pageimage": "J.jpg"}, {"title": "Missing", "missing": True}],
    }}
    resolved = images._resolve(data, ["jajangmyeon", "Missing", "Other"])
    assert resolved["jajangmyeon"]["pageimage"] == "J.jpg" and resolved["Missing"]["missing"] and "Other" not in resolved


def test_one_file_is_one_photo_whatever_the_thumbnail_size():
    keys = {images._file_key(u) for u in (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5a/Bindae_tteok.jpg/960px-Bindae_tteok.jpg",
        "https://thumb.wikimedia.org/wikipedia/commons/thumb/5/5a/Bindae_tteok.jpg/330px-Bindae_tteok.jpg?utm_source=x",
        "https://upload.wikimedia.org/wikipedia/commons/5/5a/Bindae_tteok.jpg",
    )}
    assert keys == {"file:bindae_tteok.jpg"}
    assert images._file_key("https://img.example/a.jpg") == "https://img.example/a.jpg"


def test_maintenance_categories_are_dropped():
    meta = {"Categories": {"value": "Bindae-tteok|Taken with Nikon D40|Images from Wiki Loves Folklore 2023|"
                                    "Files with coordinates missing SDC location of creation|People of Lagos, Nigeria|"
                                    "CC-BY-SA-4.0|Photographs taken on 2023-02-22"}}
    assert images._categories(meta) == "Bindae-tteok, People of Lagos, Nigeria"


# --------------------------------------------------------------------------- find_pair


async def test_lead_images_are_batched_and_judged_once_for_both_cards(wiki, fake_llm):
    a, b = korean_market(wiki)
    fake_llm.on("chat_json", judge_by(("Wikipedia", "yes")))
    img_a, img_b = await images.find_pair(a, b, context='Player\'s country: South Korea (KR). Question: "Snack run:"')
    assert img_a.url == thumb("Bindae-tteok.jpg") and img_b.url == thumb("Tteokbokki.JPG")
    assert img_a.credit == "Lee / CC BY 2.0 via Wikipedia"
    assert img_a.source_url == "https://commons.wikimedia.org/wiki/File:Bindae-tteok.jpg"
    # one ko.wikipedia request for both titles, one Commons file-details request, one Commons search per card
    assert wiki.hosts().count("ko.wikipedia.org") == 1
    ko = next(r for r in wiki.requests if r.url.host == "ko.wikipedia.org")
    assert ko.url.params["titles"] == "빈대떡|떡볶이" and ko.url.params["pilicense"] == "any"
    assert sorted(wiki.commons_queries()) == ["bindaetteok filetype:bitmap", "tteokbokki filetype:bitmap"]
    assert {r.headers["user-agent"] for r in wiki.requests} == {images.USER_AGENT} and "@" not in images.USER_AGENT
    [call] = judge_calls(fake_llm)  # ONE judge call for both cards
    assert "Card A:" in call.user and "Card B:" in call.user and "Snack run" in call.user
    assert "Mung bean pancakes" in call.user and "categories: Bindae-tteok" in call.user
    assert "Taken with" not in call.user  # maintenance categories never reach the judge
    assert call.kwargs["max_tokens"] == images.JUDGE_MAX_TOKENS and call.kwargs["hedge_after"] == images.JUDGE_HEDGE_S


async def test_unsafe_or_off_topic_photos_fall_through_to_the_next_candidate(wiki, fake_llm):
    a, b = korean_market(wiki)
    wiki.commons["bindaetteok"] = [
        commons_file("Bindaetteok memorial stall", description="2022"),  # blocklisted: never reaches the judge
        commons_file("Bindaetteok stall 위령", description="이태원 참사 추모 공간의 빈대떡"),  # judged unsafe
        commons_file("Bindaetteok frying", description="Korean pancakes"),
    ]
    fake_llm.on("chat_json", judge_by(("Bindae-tteok.jpg", "no"), ("추모", "unsafe"), ("frying", "yes"),
                                      ("Tteokbokki", "yes")))
    img_a, img_b = await images.find_pair(a, b)
    assert "Bindaetteok_frying" in img_a.url and img_a.credit == "Kim / CC BY-SA 4.0 via Wikimedia Commons"
    assert "Tteokbokki" in img_b.url
    [call] = judge_calls(fake_llm)
    assert "memorial" not in call.user and "이태원 참사 추모" in call.user  # judged in Korean


async def test_a_card_with_no_candidates_gets_its_next_tier_in_the_same_judge_call(wiki, fake_llm):
    a, b = korean_market(wiki)
    del wiki.articles[("ko", "떡볶이")]
    wiki.commons["포장마차 떡볶이"] = [commons_file("Pojangmacha 1", description="포장마차 떡볶이")]
    fake_llm.on("chat_json", judge_by(("Bindae", "yes"), ("Pojangmacha", "yes")))
    img_a, img_b = await images.find_pair(a, b)
    assert img_a is not None and "Pojangmacha_1" in img_b.url
    assert len(judge_calls(fake_llm)) == 1  # fetched before judging: still one call
    assert "포장마차 떡볶이 filetype:bitmap" in wiki.commons_queries()  # its native-script name
    assert ("ko", "포장마차 떡볶이") in {(r.url.host.split(".")[0], r.url.params.get("gsrsearch")) for r in wiki.requests}


async def test_a_card_whose_photos_were_all_turned_down_gets_its_next_tier(wiki, fake_llm):
    a, b = korean_market(wiki)
    wiki.articles[("en", "Tteokbokki")] = article("Tteokbokki", "Tteokbokki plate.jpg")
    wiki.searches[("en", "tteokbokki")] = ["Tteokbokki"]
    fake_llm.on("chat_json", judge_by(("Bindae", "yes"), ("Tteokbokki.JPG", "no"), ("plate", "yes")))
    img_a, img_b = await images.find_pair(a, b)
    assert img_a is not None and "Tteokbokki_plate" in img_b.url
    calls = judge_calls(fake_llm)
    assert len(calls) == 2 and "Card A" not in calls[1].user  # the second call judges only card B


async def test_one_card_with_nothing_safe_means_no_photos_at_all(wiki, fake_llm):
    a, b = korean_market(wiki)
    fake_llm.on("chat_json", judge_by(("Bindae", "yes"), ("Tteokbokki", "unsafe")))
    assert await images.find_pair(a, b) == (None, None)  # balanced cards: gradient on both


async def test_a_waiting_player_gives_the_next_tier_a_shorter_window(wiki, fake_llm, monkeypatch):
    monkeypatch.setattr(images, "HURRIED_GATHER_S", 0.1)
    a, b = korean_market(wiki)

    async def slow_next_tier(request):
        if "gsrsearch" in request.url.params and request.url.params["gsrsearch"] in ("tteokbokki", "포장마차 떡볶이"):
            await asyncio.sleep(2)
            return httpx.Response(200, json={})
        return None

    wiki.override = slow_next_tier
    fake_llm.on("chat_json", judge_by(("Bindae", "yes"), ("Tteokbokki", "no")))
    hurry = asyncio.Event()
    hurry.set()
    t0 = time.perf_counter()
    assert await images.find_pair(a, b, hurry=hurry) == (None, None)
    assert time.perf_counter() - t0 < 1


async def test_a_related_photo_is_the_last_resort(wiki, fake_llm):
    a, b = korean_market(wiki)
    fake_llm.on("chat_json", judge_by(("Bindae", "yes"), ("Tteokbokki", "partial")))
    img_a, img_b = await images.find_pair(a, b)
    assert img_a is not None and "Tteokbokki" in img_b.url
    # it still looked for a better one first (the next tier, which found nothing new here)
    assert ("en.wikipedia.org", "tteokbokki") in {(r.url.host, r.url.params.get("gsrsearch")) for r in wiki.requests}


async def test_without_the_judge_only_lead_images_are_trusted(wiki, fake_llm):
    a, b = korean_market(wiki)
    fake_llm.on("chat_json", lambda call: LLMError("down"))
    img_a, img_b = await images.find_pair(a, b)
    assert "Bindae-tteok" in img_a.url and "Tteokbokki" in img_b.url
    images.clear_cache()
    del wiki.articles[("ko", "떡볶이")]
    wiki.commons["tteokbokki"] = [commons_file("Tteokbokki plate")]
    assert await images.find_pair(a, b) == (None, None)  # an unjudged Commons photo is never used


async def test_photos_already_shown_or_on_the_other_card_are_never_reused(wiki, fake_llm):
    a, b = korean_market(wiki)
    wiki.commons["bindaetteok"] = [commons_file("Bindaetteok frying")]
    fake_llm.on("chat_json", judge_by(("Bindae", "yes"), ("Tteokbokki", "yes")))
    shown = ["https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Bindae-tteok.jpg/330px-Bindae-tteok.jpg"]
    img_a, _ = await images.find_pair(a, b, exclude=shown)  # same file, another thumbnail size
    assert "Bindaetteok_frying" in img_a.url
    same = spec("Bindaetteok, extra crispy", "bindaetteok", wiki="ko:빈대떡")
    img_a, img_b = await images.find_pair(a, same)
    assert img_a.url != img_b.url and {"Bindae-tteok", "Bindaetteok_frying"} == {
        "Bindae-tteok" if "Bindae-tteok" in u else "Bindaetteok_frying" for u in (img_a.url, img_b.url)
    }


async def test_missing_disambiguation_and_imageless_articles_have_fallbacks(wiki, fake_llm):
    wiki.articles[("ko", "PC방")] = article("PC방", "Korean PC bang.jpg")  # reached by a fuzzy search
    wiki.searches[("ko", "피씨방")] = ["PC방"]
    wiki.articles[("en", "Adire")] = article("Adire", disambiguation=True)
    wiki.articles[("en", "Adire (textile)")] = article("Adire (textile)", "Adire cloth.jpg")
    wiki.searches[("en", "Adire")] = ["Adire", "Adire (textile)"]
    wiki.articles[("pt", "Coxinha")] = article("Coxinha", english="Coxinha")  # no free image on ptwiki
    wiki.articles[("en", "Coxinha")] = article("Coxinha", "Coxinha snack.jpg")
    wiki.redirects[("en", "Jajangmyeon")] = "Jjajangmyeon"
    wiki.articles[("en", "Jjajangmyeon")] = article("Jjajangmyeon", "Jajangmyeon.jpg")
    refs = [("ko", "피씨방", False), ("en", "Adire", False), ("pt", "Coxinha", False), ("en", "Jajangmyeon", False)]
    found = await images._lead_images(refs)
    assert [[c.file for c in cands] for cands in found] == [
        ["Korean_PC_bang.jpg"], ["Adire_cloth.jpg"], ["Coxinha_snack.jpg"], ["Jajangmyeon.jpg"],
    ]
    count = len(wiki.requests)
    assert await images._lead_images(refs) == found and len(wiki.requests) == count  # cached per article


async def test_lookups_and_verdicts_are_cached(wiki, fake_llm):
    a, b = korean_market(wiki)
    fake_llm.on("chat_json", judge_by(("Wikipedia", "yes")))
    first = await images.find_pair(a, b)
    count = len(wiki.requests)
    assert await images.find_pair(a, b) == first
    assert len(wiki.requests) == count and len(judge_calls(fake_llm)) == 1


async def test_never_raises_on_network_errors_or_garbage(wiki, fake_llm):
    a, b = korean_market(wiki)
    wiki.override = lambda r: httpx.ConnectError("down", request=r)
    assert await images.find_pair(a, b) == (None, None)
    images.clear_cache()
    wiki.override = lambda r: httpx.Response(200, text="<html>not json</html>")
    assert await images.find_pair(a, b) == (None, None)
    images.clear_cache()
    wiki.override = lambda r: httpx.Response(200, json={"query": {"pages": "nonsense"}})
    assert await images.find_pair(a, b) == (None, None)
    assert judge_calls(fake_llm) == []  # nothing to judge


async def test_a_rate_limit_pauses_every_lookup(wiki, fake_llm):
    a, b = korean_market(wiki)
    wiki.override = lambda r: httpx.Response(429, headers={"Retry-After": "120"})
    assert await images.find_pair(a, b) == (None, None)
    count = len(wiki.requests)
    wiki.override = None
    assert await images.find_pair(spec("Hotteok", "hotteok", wiki="ko:호떡"), b) == (None, None)
    assert len(wiki.requests) == count  # paused: Wikimedia's limit is per client, across its sites


async def test_the_request_budget_skips_instead_of_queueing_forever(monkeypatch):
    budget = images._Budget(per_minute=60, burst=2)  # one token a second
    assert budget.reserve() == 0 and budget.reserve() == 0
    assert budget.reserve() == pytest.approx(1.0, abs=0.05)  # waits for the next token
    monkeypatch.setattr(images, "MAX_QUEUE_S", 0.5)
    assert budget.reserve() is None  # would wait ~2 s: skipped


async def test_slow_lookups_do_not_hold_up_the_judge(wiki, fake_llm, monkeypatch):
    monkeypatch.setattr(images, "GATHER_S", 0.1)
    a, b = korean_market(wiki)

    async def slow_commons(request):
        if request.url.host == COMMONS and "gsrsearch" in request.url.params:
            await asyncio.sleep(2)
            return httpx.Response(200, json={})
        return None

    wiki.override = slow_commons
    fake_llm.on("chat_json", judge_by(("Wikipedia", "yes")))
    t0 = time.perf_counter()
    img_a, img_b = await images.find_pair(a, b)
    assert time.perf_counter() - t0 < 1 and img_a is not None and img_b is not None


async def test_concurrent_lookups_share_one_commons_search(wiki, fake_llm):
    async def slow(request):
        await asyncio.sleep(0.05)
        return None

    wiki.override = slow
    wiki.commons["kimbap roll"] = [commons_file("Kimbap roll")]
    fake_llm.on("chat_json", judge_by(("Kimbap", "yes")))
    a = spec("Kimbap", "kimbap roll")
    b = spec("Kimbap again", "Kimbap roll")
    await asyncio.gather(images.find_pair(a, b), images.find_pair(a, b))
    assert wiki.commons_queries().count("kimbap roll filetype:bitmap") == 1
