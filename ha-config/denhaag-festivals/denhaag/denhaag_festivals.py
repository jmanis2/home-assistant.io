#!/usr/bin/env python3
"""Scrape the denhaag.com festival agenda into an ICS file for Home Assistant.

Standard library only, so it runs inside the Home Assistant container
(HA OS, Container or Core) without installing anything.

Usage:
    python3 denhaag_festivals.py [--out PATH] [--dump PATH] [URL ...]

It looks for event data in two places, in order:
  1. schema.org JSON-LD (<script type="application/ld+json">)
  2. embedded JSON app state (__NEXT_DATA__, __NUXT_DATA__ or any
     <script type="application/json">), scanning for objects that look
     like events (a title plus a start date)

If nothing is found the existing ICS is left untouched and the script exits
with status 1, so a site change never wipes your calendar. Use --dump to save
the fetched HTML for debugging.
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import urllib.request
from zoneinfo import ZoneInfo

DEFAULT_URLS = ["https://denhaag.com/nl/agenda/festivals"]
DEFAULT_OUT = "/config/www/denhaag_festivals.ics"
TZ = ZoneInfo("Europe/Amsterdam")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

EVENT_TYPES = {"Event", "Festival", "MusicEvent", "TheaterEvent", "FoodEvent",
               "SocialEvent", "ExhibitionEvent", "SportsEvent", "DanceEvent",
               "ComedyEvent", "ChildrensEvent", "LiteraryEvent", "ScreeningEvent",
               "EducationEvent", "BusinessEvent", "VisualArtsEvent"}
TITLE_KEYS = ("name", "title", "heading", "label")
START_KEYS = ("startDate", "start_date", "start", "dateFrom", "date_from",
              "from", "startsAt", "starts_at", "beginDate", "begin")
END_KEYS = ("endDate", "end_date", "end", "dateTo", "date_to", "to",
            "endsAt", "ends_at", "untilDate", "until")
URL_KEYS = ("url", "href", "link", "slug", "path", "uri")
LOC_KEYS = ("location", "venue", "place", "locationName", "address")
DESC_KEYS = ("description", "summary", "intro", "teaser", "excerpt")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def scripts(page, type_re):
    pat = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
    for attrs, body in pat.findall(page):
        if re.search(type_re, attrs, re.I):
            yield attrs, body


def load_json(text):
    text = text.strip()
    if text.startswith("<!--"):
        text = text[4:]
    if text.endswith("-->"):
        text = text[:-3]
    try:
        return json.loads(text)
    except ValueError:
        return None


def parse_date(value):
    """Return (datetime_or_date, is_all_day) or None."""
    if isinstance(value, (int, float)) and value > 10**9:
        secs = value / 1000 if value > 10**11 else value
        return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc), False
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not DATE_RE.match(s):
        return None
    if len(s) == 10:
        return dt.date.fromisoformat(s), True
    s = s.replace(" ", "T").replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d, False


def first(obj, keys):
    for k in keys:
        if k in obj and obj[k] not in (None, "", [], {}):
            return obj[k]
    return None


def text_of(value):
    if isinstance(value, str):
        return re.sub(r"<[^>]+>", "", html.unescape(value)).strip()
    if isinstance(value, dict):
        for k in ("name", "title", "text", "value", "nl", "en"):
            if k in value:
                return text_of(value[k])
        addr = value.get("address")
        if addr:
            return text_of(addr)
    if isinstance(value, list) and value:
        return text_of(value[0])
    return ""


def location_of(value):
    if isinstance(value, dict):
        parts = [text_of(value.get("name", ""))]
        addr = value.get("address")
        if isinstance(addr, dict):
            parts.append(", ".join(x for x in (
                addr.get("streetAddress"), addr.get("addressLocality")) if x))
        elif isinstance(addr, str):
            parts.append(addr)
        return ", ".join(p for p in parts if p)
    return text_of(value)


def absolute(url, base):
    if not isinstance(url, str) or not url:
        return base
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return "https://denhaag.com" + url
    return base


def to_event(obj, base_url, strict):
    if not isinstance(obj, dict):
        return None
    if strict:
        t = obj.get("@type")
        types = set(t) if isinstance(t, list) else {t}
        if not types & EVENT_TYPES:
            return None
    title = text_of(first(obj, TITLE_KEYS))
    start = parse_date(first(obj, START_KEYS))
    if not title or not start:
        return None
    end = parse_date(first(obj, END_KEYS))
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": absolute(first(obj, URL_KEYS), base_url),
        "location": location_of(first(obj, LOC_KEYS)),
        "description": text_of(first(obj, DESC_KEYS))[:500],
    }


def walk(node, base_url, strict, out):
    if isinstance(node, list):
        for n in node:
            walk(n, base_url, strict, out)
    elif isinstance(node, dict):
        ev = to_event(node, base_url, strict)
        if ev:
            out.append(ev)
            # JSON-LD events can carry subEvent lists; app state usually not.
            if strict and "subEvent" in node:
                walk(node["subEvent"], base_url, strict, out)
            return
        for v in node.values():
            walk(v, base_url, strict, out)


def extract(page, base_url):
    events = []
    for _, body in scripts(page, r"application/ld\+json"):
        data = load_json(body)
        if data is not None:
            walk(data, base_url, True, events)
    if events:
        return events, "json-ld"
    for _, body in scripts(page, r"application/json|__NEXT_DATA__|__NUXT"):
        data = load_json(body)
        if data is not None:
            walk(data, base_url, False, events)
    return events, "app-state" if events else "none"


# ---------- ICS output ----------

def esc(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;") \
        .replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def fold(line):
    """Fold lines to 75 octets as RFC 5545 requires."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not parts else 74
        if len(cur) + len(b) > limit:
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def fmt(value, all_day):
    if all_day:
        return ";VALUE=DATE:" + value.strftime("%Y%m%d")
    return ":" + value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(events):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//home-assistant//denhaag-festivals//NL",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Den Haag festivals",
             "X-WR-TIMEZONE:Europe/Amsterdam"]
    for ev in events:
        start, all_day = ev["start"]
        if ev["end"]:
            end, end_all_day = ev["end"]
        else:
            end, end_all_day = start, all_day
        # Mixed precision: fall back to all-day on both ends.
        if all_day != end_all_day:
            start = start.date() if isinstance(start, dt.datetime) else start
            end = end.date() if isinstance(end, dt.datetime) else end
            all_day = True
        if all_day:
            end = end + dt.timedelta(days=1)  # DTEND is exclusive
        elif end <= start:
            end = start + dt.timedelta(hours=2)
        uid = hashlib.sha1(f"{ev['title']}|{start.isoformat()}".encode()).hexdigest()
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@denhaag-festivals",
            f"DTSTAMP:{now}",
            f"DTSTART{fmt(start, all_day)}",
            f"DTEND{fmt(end, all_day)}",
            f"SUMMARY:{esc(ev['title'])}",
        ]
        if ev["location"]:
            lines.append(f"LOCATION:{esc(ev['location'])}")
        desc = "\n\n".join(x for x in (ev["description"], ev["url"]) if x)
        if desc:
            lines.append(f"DESCRIPTION:{esc(desc)}")
        if ev["url"]:
            lines.append(f"URL:{ev['url']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def write_atomic(path, content):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".denhaag_", suffix=".ics")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("urls", nargs="*", default=DEFAULT_URLS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dump", help="save fetched HTML here for debugging")
    ap.add_argument("--file", help="parse a saved HTML file instead of fetching")
    args = ap.parse_args()

    found, seen = [], set()
    sources = [(args.file, None)] if args.file else [(None, u) for u in args.urls]
    for path, url in sources:
        try:
            if path:
                with open(path, encoding="utf-8") as f:
                    page = f.read()
                url = DEFAULT_URLS[0]
            else:
                page = fetch(url)
        except Exception as e:
            print(f"ERROR fetching {url or path}: {e}", file=sys.stderr)
            continue
        if args.dump:
            with open(args.dump, "w", encoding="utf-8") as f:
                f.write(page)
        events, how = extract(page, url)
        print(f"{url}: {len(events)} events via {how}")
        for ev in events:
            key = (ev["title"].lower(), str(ev["start"][0]))
            if key not in seen:
                seen.add(key)
                found.append(ev)

    if not found:
        print("No events found; leaving existing calendar untouched. "
              "Run with --dump /config/www/denhaag_debug.html to inspect the page.",
              file=sys.stderr)
        return 1

    found.sort(key=lambda e: (str(e["start"][0]), e["title"]))
    write_atomic(args.out, build_ics(found))
    print(f"Wrote {len(found)} events to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
