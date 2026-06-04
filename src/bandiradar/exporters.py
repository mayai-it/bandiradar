"""Pure formatters for matches — JSON and RSS 2.0 (Phase-1 export).

No business logic, no intelligence dependency: turns a list of
``(Opportunity, Match)`` into the documented JSON shape or a valid RSS feed.
Managed delivery (WhatsApp/email/alerts) is out of scope — that lives in
bandiradar-pro; here we produce files a single user can consume or self-host.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from email.utils import format_datetime

from bandiradar.models import Match, Opportunity

_FEED_TITLE = "BandiRadar — matched opportunities"
_FEED_LINK = "https://github.com/mayai-it/bandiradar"


def match_payload(
    matches: list[tuple[Opportunity, Match]],
) -> list[dict]:
    """The documented match shape (shared by the CLI, MCP, and exporters)."""
    return [
        {
            "opportunity_id": opp.id,
            "score": match.score,
            "status": opp.status,
            "title": opp.title,
            "deadline": opp.deadline.isoformat() if opp.deadline else None,
            "reasons": match.reasons,
            "matched_capabilities": match.matched_capabilities,
            "source_url": opp.source_url,
        }
        for opp, match in matches
    ]


def to_json(matches: list[tuple[Opportunity, Match]]) -> str:
    """JSON array of the documented match shape."""
    return json.dumps(match_payload(matches), ensure_ascii=False, indent=2)


def _item_description(opp: Opportunity, match: Match) -> str:
    deadline = opp.deadline.strftime("%Y-%m-%d") if opp.deadline else "—"
    reasons = "; ".join(match.reasons) if match.reasons else "—"
    return f"Score {match.score}/100. Deadline: {deadline}. {reasons}"


def to_rss(
    matches: list[tuple[Opportunity, Match]],
    title: str = _FEED_TITLE,
    link: str = _FEED_LINK,
) -> str:
    """Render matches as an RSS 2.0 feed (valid XML)."""
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(
        channel, "description"
    ).text = "Italian public funding opportunities matched to a company profile."

    for opp, match in matches:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = opp.title
        if opp.source_url:
            ET.SubElement(item, "link").text = opp.source_url
        ET.SubElement(item, "description").text = _item_description(opp, match)
        guid = ET.SubElement(item, "guid")
        guid.text = opp.id
        guid.set("isPermaLink", "false")
        when = opp.published_at or opp.deadline
        if when is not None:
            ET.SubElement(item, "pubDate").text = format_datetime(when)

    return ET.tostring(rss, encoding="unicode", xml_declaration=True)
