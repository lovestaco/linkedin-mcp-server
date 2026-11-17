"""msgkit_profile: the PT-button fields read from profile page text."""

from linkedin_mcp_server.tools.msgkit_profile import (
    build_fields,
    parse_degree,
    parse_latest_experience_entry,
    parse_top_card,
)

BODY = """Home
Ada Example
· 2nd
Staff Engineer | Distributed systems
Acme Robotics
Pune, Maharashtra, India
·
Contact info
Acme Robotics
4,321 followers
500+ connections
Message
Connect
More
"""


def test_top_card_fields():
    f = parse_top_card(BODY, "Ada Example")
    assert f["followers"] == "4321"
    assert f["place"] == "Pune, Maharashtra, India"
    assert f["country"] == "India"
    assert f["headline"] == "Staff Engineer | Distributed systems"
    assert f["company"] == "Acme Robotics"


def test_top_card_missing_everything():
    f = parse_top_card("nothing useful here", "Ada Example")
    assert f == {
        "followers": None,
        "place": None,
        "country": None,
        "company": None,
        "headline": None,
    }


def test_place_without_comma_stays_whole():
    body = (
        "Ada Example\nHead of Sales\nGreater Chicago Area\n·\nContact info\nZeta Corp\n"
    )
    f = parse_top_card(body, "Ada Example")
    assert f["place"] == "Greater Chicago Area"
    assert f["country"] == "Greater Chicago Area"


def test_experience_single_position():
    text = "Experience\nSenior Engineer\nAcme Robotics · Full-time\nJan 2022 - Present · 3 yrs\nRemote"
    assert parse_latest_experience_entry(text) == {
        "company": "Acme Robotics",
        "role": "Senior Engineer",
    }


def test_experience_grouped_company():
    text = (
        "Experience\nPaisabazaar\n8 yrs 11 mos\nGurugram, Haryana, India\n"
        "Engineering Manager\nFull-time\nJan 2020 - Present · 5 yrs\n"
        "Senior Engineer\nJan 2016 - Dec 2019 · 4 yrs"
    )
    assert parse_latest_experience_entry(text) == {
        "company": "Paisabazaar",
        "role": "Engineering Manager",
    }


def test_experience_empty():
    assert parse_latest_experience_entry("") == {"company": None, "role": None}


def test_degree():
    assert parse_degree(["Ada", "· 2nd", "x"]) == "2nd"
    assert parse_degree(["Ada", "• 1st"]) == "1st"
    assert parse_degree(["Ada", "Message"]) is None


def test_build_fields_prefers_experience_over_top_card():
    raw = {
        "name": "Ada Example",
        "text": BODY,
        "experience_text": "Experience\nPrincipal Engineer\nGlobex · Full-time\n2023 - Present",
        "experience_company_url": "https://www.linkedin.com/company/12345/",
    }
    f = build_fields(raw)
    assert f["company"] == "Globex"
    assert f["company_position"] == "Principal Engineer"
    assert f["company_url"] == "https://www.linkedin.com/company/12345/"
    assert f["degree"] == "2nd"
    assert f["name"] == "Ada Example"


def test_build_fields_name_from_title_and_non_company_link():
    raw = {
        "title": "(3) Ada Example | LinkedIn",
        "text": "",
        "experience_company_url": "https://www.linkedin.com/school/9/",
    }
    f = build_fields(raw)
    assert f["name"] == "Ada Example"
    assert f["company_url"] is None


def test_thread_url_is_the_message_buttons_link():
    compose = "https://www.linkedin.com/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AABC&screenContext=NON_SELF_PROFILE_VIEW"
    f = build_fields(
        {"name": "Ada Example", "text": BODY, "message_url": compose + "#x"}
    )
    assert f["thread_url"] == compose
    thread = "https://www.linkedin.com/messaging/thread/2-abc==/"
    assert (
        build_fields({"name": "Ada", "text": BODY, "message_url": thread})["thread_url"]
        == thread
    )


def test_thread_url_missing_or_foreign_link_is_none():
    assert build_fields({"name": "Ada", "text": BODY})["thread_url"] is None
    assert (
        build_fields(
            {"name": "Ada", "text": BODY, "message_url": "https://example.com/x"}
        )["thread_url"]
        is None
    )
