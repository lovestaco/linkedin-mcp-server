"""
Profile-tracking fields read off one rendered LinkedIn profile page (fork-only).

A port of the msgkit Chrome extension's PT button (content.js:
extractProfileMetrics and parseLatestExperienceEntry), kept as plain functions
over text so they can be tested without a browser. msgkit_connect passes in
what the page showed and gets back the same fields the PT button writes to the
profile-tracking sheet: followers, place, country, company, company position
and headline.
"""

import re
from typing import Any

_SCHOOL_RE = re.compile(
    r"(university|college|school|institute|academy|studies|faculty|campus)", re.I
)
_LOCATION_RE = re.compile(
    r"(?:,|\b(?:area|region|district|metropolitan|remote|hybrid)\b|\b(?:india|united states|usa|"
    r"singapore|london|delhi|bangalore|mumbai|new york|san francisco|california|england|germany|"
    r"france|canada|australia|uae)\b)",
    re.I,
)
_EMPLOYMENT_RE = re.compile(
    r"^(Full-time|Part-time|Self-employed|Freelance|Contract|Internship|Trainee|Seasonal|"
    r"Apprenticeship)$",
    re.I,
)
_DATE_OR_DURATION_RE = re.compile(r"\d{4}|present|\byrs?\b|\bmos?\b", re.I)
_DOT_RE = re.compile(r"^[·•]$")
_ACTION_RE = re.compile(
    r"^(More|Message|Connect|Follow|Pending|Following|Save in Sales Navigator|Accept|Ignore)$",
    re.I,
)
_COMPANY_SKIP_RE = re.compile(
    r"^(Message|More|Connect|Follow|Pending|Following|MsgKit Copy|Find Email|Save Lead|Track All|"
    r"PT\b|PT Like|PT Comment|Comment Tracker|Like Blacklist|Blacklisted)",
    re.I,
)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def lines_of(text: str | None) -> list[str]:
    return [c for c in (clean(s) for s in (text or "").split("\n")) if c]


def is_likely_location(text: str | None) -> bool:
    if not text:
        return False
    if re.search(r"\d+\+?\s*(?:connections?|followers?)", text, re.I):
        return False
    if _SCHOOL_RE.search(text):
        return False
    if re.search(r"\s+at\s+", text, re.I):
        return False
    return bool(_LOCATION_RE.search(text))


def _is_date_or_duration(line: str) -> bool:
    return bool(_DATE_OR_DURATION_RE.search(line))


def parse_latest_experience_entry(text: str | None) -> dict[str, str | None]:
    """Company and role of the most recent Experience entry (both layouts).

    Single position:  <Role> / <Company> · <Type> / <dates>
    Grouped company:  <Company> / <aggregate duration> / ... / <Role> / <dates>
    """
    lines = lines_of(text)
    header = next(
        (i for i, ln in enumerate(lines) if re.fullmatch(r"Experience", ln, re.I)), -1
    )
    start = 0 if header == -1 else header + 1
    if start + 1 >= len(lines):
        return {"company": None, "role": None}
    line0, line1 = lines[start], lines[start + 1]
    parts = [clean(p) for p in line1.split(" · ")] if " · " in line1 else [line1]
    first = parts[0]
    looks_like_company = (
        bool(first)
        and not _EMPLOYMENT_RE.match(first)
        and not _is_date_or_duration(first)
    )
    if len(parts) >= 2 and looks_like_company:
        return {"company": first, "role": line0 or None}
    if _is_date_or_duration(line1) or _EMPLOYMENT_RE.match(first):
        company = line0 or None
        for i in range(start + 2, min(start + 7, len(lines))):
            cand = lines[i]
            if (
                _EMPLOYMENT_RE.match(cand)
                or _is_date_or_duration(cand)
                or is_likely_location(cand)
            ):
                continue
            j = i + 1
            if j < len(lines) and _EMPLOYMENT_RE.match(lines[j]):
                j += 1
            if j < len(lines) and _is_date_or_duration(lines[j]):
                return {"company": company, "role": cand}
        return {"company": company, "role": None}
    return {"company": None, "role": None}


def parse_degree(lines: list[str]) -> str | None:
    """'1st' / '2nd' / '3rd' from the '· 2nd' line under the name."""
    for ln in lines[:80]:
        m = re.match(r"^[•·]\s*(1st|2nd|3rd)\s*$", ln, re.I)
        if m:
            return m.group(1).lower()
    return None


def parse_top_card(body_text: str | None, name: str | None) -> dict[str, Any]:
    """followers, place, country, company (top card), headline."""
    text = body_text or ""
    lines = lines_of(text)

    m = re.search(r"([\d,]+)\s*followers?\b", text, re.I)
    followers = m.group(1).replace(",", "") if m else None

    contact = next(
        (i for i, ln in enumerate(lines) if re.match(r"^Contact info\b", ln, re.I)), -1
    )
    place = company = None
    if contact != -1:
        prev = lines[contact - 1] if contact - 1 >= 0 else None
        if prev is not None and _DOT_RE.match(prev):
            cand = lines[contact - 2] if contact - 2 >= 0 else None
            if cand and not _DOT_RE.match(cand) and len(cand) <= 80:
                place = cand
        elif prev and is_likely_location(prev):
            place = prev
        for i in range(contact + 1, min(len(lines), contact + 5)):
            ln = lines[i]
            if _DOT_RE.match(ln) or re.match(r"^https?://", ln, re.I):
                continue
            if re.search(r"\b(?:followers?|connections?)\b", ln, re.I):
                continue
            if re.search(r"\bmutual connection", ln, re.I) or "👀" in ln:
                continue
            if _COMPANY_SKIP_RE.match(ln):
                continue
            company = ln
            break

    country = None
    if place:
        segs = [s.strip() for s in place.split(",") if s.strip()]
        country = segs[-1] if segs else None

    headline = None
    name_line = clean(name)
    idx = lines.index(name_line) if name_line and name_line in lines else -1
    if idx != -1:
        for i in range(idx + 1, min(len(lines), idx + 7)):
            ln = lines[i]
            if (
                ln.startswith("👀")
                or re.fullmatch(r"\([^)]*\)", ln)
                or re.match(r"^[•·]\s*(1st|2nd|3rd)\b", ln, re.I)
                or _ACTION_RE.match(ln)
            ):
                continue
            headline = ln
            break
    return {
        "followers": followers,
        "place": place,
        "country": country,
        "company": company,
        "headline": headline,
    }


def build_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """The profile-tracking fields from what the page showed.

    raw: {name, title, text (body innerText), experience_text, experience_company_url}
    The Experience entry's company and role win over the top-card guess, as in
    the extension; every field is None when it could not be found.
    """
    name = clean(raw.get("name")) or None
    if not name:
        t = re.sub(r"\s*\|\s*LinkedIn\b.*$", "", raw.get("title") or "", flags=re.I)
        name = clean(re.sub(r"^\(\d+\+?\)\s*", "", t)) or None
    fields = parse_top_card(raw.get("text"), name)
    entry = parse_latest_experience_entry(raw.get("experience_text"))
    fields["company"] = entry["company"] or fields["company"]
    fields["company_position"] = entry["role"]
    url = raw.get("experience_company_url") or None
    fields["company_url"] = url if url and "/company/" in url else None
    fields["name"] = name
    # The top card's Message button: a conversation (/messaging/thread/...) when one exists,
    # otherwise the compose link for this person. Whatever the button holds.
    msg = (raw.get("message_url") or "").split("#")[0].strip()
    fields["thread_url"] = msg if "/messaging/" in msg else None
    fields["degree"] = parse_degree(lines_of(raw.get("text")))
    return fields
