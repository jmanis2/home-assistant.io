#!/usr/bin/env python3
"""Turn the City of The Hague's event-permit open data into an ICS file for Home Assistant.

Standard library only, so it runs inside the Home Assistant container
(HA OS, Container or Core) without installing anything.

Usage:
    python3 denhaag_festivals.py [--out PATH] [--dump PATH] [--file PATH]

Source: dataset "Evenementen Den Haag" (event permits from Key2Vergunningen) on
https://den-haag-opendata.opendatasoft.com, also listed on data.overheid.nl.
Each record is a permit: a free-text description such as "7Fest op 22 juli
2028", a location, a start date and an end date. There is no category field,
so withdrawn permits, private parties, small street parties and standing
multi-year permits (weekly markets, yearly memorials) are dropped by keyword
and by length. See EXCLUDE_RE and MAX_SPAN_DAYS.

If nothing is left the existing ICS is left untouched and the script exits
with status 1, so a source problem never wipes your calendar. Use --dump to
save the raw API JSON for debugging.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API = ("https://den-haag-opendata.opendatasoft.com/api/explore/v2.1"
       "/catalog/datasets/evenementendenhaag/records")
DATASET_URL = "https://den-haag-opendata.opendatasoft.com/explore/dataset/evenementendenhaag/"
DEFAULT_OUT = "/config/www/denhaag_festivals.ics"
TZ = ZoneInfo("Europe/Amsterdam")
PAGE = 100  # API maximum per request

# Permits longer than this are standing permits (weekly markets, yearly
# memorials registered for 5 to 13 years at a time), not events. The longest
# real events (fairs, a winter ice rink) run up to about 4 months.
MAX_SPAN_DAYS = 180

# Dropped by keyword in the permit description (case-insensitive):
#   withdrawn permits; private parties and school sports days; small
#   neighbourhood street parties; standing permits; placeholder reservations.
EXCLUDE_RE = re.compile(
    r"ingetrokken|nw aanvraag|nieuwe aanvraag"
    r"|verjaardag|geslaagd|bruiloft|huwelijk|besloten|priv[eé]|school\s*sportdag"
    r"|straatfeest|buurtfeest|burendag|straatspeeldag|buitenspeeldag"
    r"|jaarlijks?\s*te?r?e?u?gkerend|iedere\s+(maan|dins|woens|donder|vrij|zater|zon)dag"
    r"|reserveren voor",
    re.I)

MONTHS = ("januari|februari|maart|april|mei|juni|juli|augustus|september"
          "|oktober|november|december")
# Trailing date text, e.g. " op 22 juli 2028", " van 4 tot en met 6 september
# 2026", " 15 en 16 mei 2027", " op 1 en 2 november".
DATE_SUFFIX_RE = re.compile(
    r"\s+(?:op|van|vanaf|d\.d\.)?\s*\d{1,2}(?:\s+(?:%s))?"
    r"(?:\s*(?:,|en|t/m|tot en met|-|tot)\s*\d{1,2})*\s+(?:%s)\b.*$" % (MONTHS, MONTHS),
    re.I)
PREFIX_RE = re.compile(r"^\s*(?:verleend|muziekvergunning|herdenking)\s*", re.I)
COMPANY_RE = re.compile(r"\s+(?:b\.?v\.?|stichting)$", re.I)


def fetch_json(params):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "home-assistant-denhaag-festivals/2.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_records(today):
    """All permits that start no more than MAX_SPAN_DAYS before today.

    einddatum is a text field, which ODSQL can't compare, so the server-side
    filter is on ingangsdatum and the end date is checked in parse_record.
    """
    since = today - dt.timedelta(days=MAX_SPAN_DAYS)
    records, offset = [], 0
    while True:
        data = fetch_json({
            "where": f"ingangsdatum >= date'{since.isoformat()}'",
            "order_by": "ingangsdatum, dossiernr",
            "limit": PAGE,
            "offset": offset,
        })
        batch = data.get("results", [])
        records += batch
        offset += len(batch)
        if not batch or offset >= data.get("total_count", 0):
            return records


def squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


def parse_day(value):
    """'2028-07-22T00:00:00+00:00' or '2028/07/22 00:00:00' -> date.

    The dataset stores plain local dates at midnight, so only the date part
    is meaningful.
    """
    if not isinstance(value, str):
        return None
    m = re.match(r"(\d{4})[-/](\d{2})[-/](\d{2})", value.strip())
    if not m:
        return None
    try:
        return dt.date(*map(int, m.groups()))
    except ValueError:
        return None


def clean_title(desc):
    t = PREFIX_RE.sub("", squash(desc))
    t = DATE_SUFFIX_RE.sub("", t)
    t = COMPANY_RE.sub("", t).strip(" -,.")
    return t or squash(desc)


def location_of(rec):
    loc = squash(rec.get("lokatie"))
    street = squash(rec.get("straat"))
    if street and street.lower() not in loc.lower():
        nr = squash(rec.get("huisnr"))
        if nr and nr != "0":  # 0 means no house number
            street = f"{street} {nr}"
        loc = f"{loc}, {street}" if loc else street
    return f"{loc}, Den Haag" if loc else "Den Haag"


def parse_record(rec, today):
    """Return (event, None) or (None, reason it was skipped)."""
    desc = squash(rec.get("registr_oms"))
    start = parse_day(rec.get("ingangsdatum"))
    if not desc or not start:
        return None, "no description or start date"
    end = parse_day(rec.get("einddatum")) or start
    if end < start:
        end = start
    if end < today:
        return None, "already ended"
    if (end - start).days > MAX_SPAN_DAYS:
        return None, f"standing permit longer than {MAX_SPAN_DAYS} days"
    m = EXCLUDE_RE.search(desc)
    if m:
        return None, f"keyword '{m.group(0).lower()}'"

    details = [desc]
    area = ", ".join(x for x in (squash(rec.get("wijk")).title(),
                                 squash(rec.get("stadsdeel")).title()) if x)
    if area:
        details.append(f"Area: {area}")
    if rec.get("bezoekersaantal"):
        details.append(f"Expected visitors: {int(rec['bezoekersaantal'])}")
    if rec.get("dossiernr"):
        details.append(f"Permit {rec['dossiernr']}")
    details.append("Source: Gemeente Den Haag open data (event permits)")

    geo = rec.get("geo_point_2d") or {}
    return {
        "title": clean_title(desc),
        "start": (start, True),
        "end": (end, True),
        "url": DATASET_URL,
        "location": location_of(rec),
        "description": "\n".join(details)[:500],
        "geo": (geo.get("lat"), geo.get("lon")) if geo.get("lat") is not None else None,
    }, None


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
        if ev.get("geo"):
            lines.append(f"GEO:{ev['geo'][0]:.6f};{ev['geo'][1]:.6f}")
        if ev["description"]:
            lines.append(f"DESCRIPTION:{esc(ev['description'])}")
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
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dump", help="save the raw API records (JSON) here for debugging")
    ap.add_argument("--file", help="parse saved records (JSON list) instead of fetching")
    ap.add_argument("-v", "--verbose", action="store_true", help="list skipped permits")
    args = ap.parse_args()

    today = dt.datetime.now(TZ).date()
    try:
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                records = json.load(f)
        else:
            records = fetch_records(today)
    except Exception as e:
        print(f"ERROR fetching {args.file or API}: {e}", file=sys.stderr)
        records = None
    if records is not None and args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=1)

    found, seen, skipped = [], set(), {}
    for rec in records or []:
        ev, reason = parse_record(rec, today)
        if not ev:
            skipped[reason] = skipped.get(reason, 0) + 1
            if args.verbose:
                print(f"skip ({reason}): {squash(rec.get('registr_oms'))}")
            continue
        key = (ev["title"].lower(), ev["start"][0])
        if key in seen:
            skipped["duplicate"] = skipped.get("duplicate", 0) + 1
            continue
        seen.add(key)
        found.append(ev)

    if records is not None:
        print(f"{len(records)} permits fetched, {len(found)} events kept"
              + "".join(f"; {n} skipped: {r}" for r, n in sorted(skipped.items())))

    if not found:
        print("No events found; leaving existing calendar untouched. "
              "Run with --dump /config/www/denhaag_debug.json to inspect the data.",
              file=sys.stderr)
        return 1

    found.sort(key=lambda e: (e["start"][0], e["title"]))
    write_atomic(args.out, build_ics(found))
    print(f"Wrote {len(found)} events to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
