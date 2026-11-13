#!/usr/bin/env python3
"""Builds iCalendar feeds of market events.

Feeds written to docs/:
  earnings.ics  large company earnings reports (Nasdaq)
  ipos.ics      upcoming and priced IPOs (Nasdaq)
  macro.ics     Fed and ECB rate decisions, FOMC minutes, US GDP, PCE, CPI, jobs and more
  all.ics       everything above in one calendar

Every source is fetched independently. When a source fails, the events it
produced last time are kept (data/cache.json), so a flaky website never wipes
the calendar. Python standard library only.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
CACHE_FILE = ROOT / "data" / "cache.json"
CONFIG = json.loads((ROOT / "config.json").read_text())

NY = ZoneInfo("America/New_York")
CHICAGO = ZoneInfo("America/Chicago")
FRANKFURT = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc
HOME = ZoneInfo(CONFIG.get("home_timezone", "Europe/Amsterdam"))

TODAY = dt.datetime.now(NY).date()
NOW_STAMP = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

SSL_CTX = ssl.create_default_context()
if sys.platform == "darwin":  # python.org builds on macOS ship without root certificates: use the system ones
    import subprocess
    pem = subprocess.run(["security", "find-certificate", "-a", "-p",
                          "/System/Library/Keychains/SystemRootCertificates.keychain"],
                         capture_output=True, text=True).stdout
    if pem:
        SSL_CTX.load_verify_locations(cadata=pem)

# Most sites want an identifying user agent; FRED only answers Python's default one.
AGENT = "market-calendar/1.0 (personal iCalendar feed)"

FEEDS = {
    "earnings": ("Earnings", "Earnings reports of the largest listed companies. Source: Nasdaq."),
    "ipos": ("IPOs", "Upcoming and recently priced IPOs. Source: Nasdaq."),
    "macro": ("Fed, ECB & US data", "Fed and ECB rate decisions, FOMC minutes and major US economic releases."),
}


# ----------------------------------------------------------------- helpers

def fetch(url: str, accept: str = "*/*", tries: int = 3) -> str:
    last = None
    headers = {"Accept": accept}
    if "stlouisfed.org" not in url:
        headers["User-Agent"] = AGENT
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def money(s: str | None) -> float:
    try:
        return float(re.sub(r"[^\d.]", "", s or "") or 0)
    except ValueError:
        return 0.0


def short_money(v: float) -> str:
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if v >= size:
            return f"${v / size:.2f}{suffix}".replace(".00", "")
    return f"${v:,.0f}"


def nice_name(name: str) -> str:
    """Nasdaq lists many IPO names in capitals; make them readable."""
    if not name or name.upper() != name:
        return name
    keep = {"AG", "SA", "NV", "PLC", "LLC", "LP", "SE", "PBC", "BDC", "REIT", "ETF", "II", "III", "IV", "AB", "ASA", "SPA", "BV"}
    out = []
    for w in name.split():
        core = w.strip(",.()")
        out.append(w if core in keep or len(core) <= 2 and not core.isalpha() else w.capitalize())
    return " ".join(out)


def local_times(start: dt.datetime) -> str:
    """'16:00 New York time (22:00 Amsterdam)' style hint for descriptions."""
    home = start.astimezone(HOME)
    return f"{start.astimezone(NY):%H:%M} New York time ({home:%H:%M} {str(HOME).split('/')[-1].replace('_', ' ')})"


def timed(uid, source, feed, start: dt.datetime, minutes: int, summary, description="", url="") -> dict:
    s = start.astimezone(UTC)
    return {
        "uid": uid, "source": source, "feed": feed, "allday": False,
        "day": start.date().isoformat(),
        "start": s.strftime("%Y%m%dT%H%M%SZ"),
        "end": (s + dt.timedelta(minutes=minutes)).strftime("%Y%m%dT%H%M%SZ"),
        "summary": summary, "description": description, "url": url,
    }


def allday(uid, source, feed, day: dt.date, summary, description="", url="") -> dict:
    return {
        "uid": uid, "source": source, "feed": feed, "allday": True,
        "day": day.isoformat(), "start": day.strftime("%Y%m%d"),
        "end": (day + dt.timedelta(days=1)).strftime("%Y%m%d"),
        "summary": summary, "description": description, "url": url,
    }


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()


# ----------------------------------------------------------------- sources
# Each source returns (events, coverage). Coverage is a list of (first_day, last_day)
# ranges the source answered for; cached events of that source inside those ranges
# are replaced, everything else in the cache is kept.

def src_earnings():
    cfg = CONFIG["earnings"]
    min_cap = cfg["min_market_cap_usd"]
    always = {s.upper() for s in cfg.get("always_include", [])}
    events, coverage, failures = [], [], 0
    day = TODAY - dt.timedelta(days=cfg["days_back"])
    end = TODAY + dt.timedelta(days=cfg["days_ahead"])
    while day <= end:
        if day.weekday() < 5:
            try:
                data = json.loads(fetch(f"https://api.nasdaq.com/api/calendar/earnings?date={day}", "application/json"))
                rows = ((data.get("data") or {}).get("rows")) or []
                coverage.append((day, day))
                for r in rows:
                    sym = (r.get("symbol") or "").upper()
                    cap = money(r.get("marketCap"))
                    if not sym or (cap < min_cap and sym not in always):
                        continue
                    events.append(earnings_event(r, sym, cap, day))
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"  earnings {day}: {e}", file=sys.stderr)
            time.sleep(0.4)
        day += dt.timedelta(days=1)
    if not coverage:
        raise RuntimeError(f"all {failures} earnings requests failed")
    return events, coverage


def earnings_event(r, sym, cap, day):
    when = r.get("time") or ""
    if "pre-market" in when:
        tag, hint = "before open", "Before the US market opens (results usually out by " + local_times(dt.datetime.combine(day, dt.time(8, 0), NY)) + ")"
    elif "after-hours" in when:
        tag, hint = "after close", "After the US market closes at " + local_times(dt.datetime.combine(day, dt.time(16, 0), NY))
    else:
        tag, hint = "", "Time of day not announced"
    lines = [r.get("name") or sym, hint]
    if r.get("fiscalQuarterEnding"):
        lines.append(f"Fiscal quarter ending {r['fiscalQuarterEnding']}")
    if r.get("epsForecast"):
        n = r.get("noOfEsts")
        lines.append(f"EPS forecast {r['epsForecast']}" + (f" ({n} analyst estimates)" if n else ""))
    if r.get("lastYearEPS") and r["lastYearEPS"] != "N/A":
        lines.append(f"Same quarter last year {r['lastYearEPS']} (reported {r.get('lastYearRptDt')})")
    lines.append(f"Market cap {short_money(cap)}")
    lines.append("Date can still move until the company confirms it.")
    q = slug(r.get("fiscalQuarterEnding") or day.isoformat())
    return allday(
        f"earnings-{slug(sym)}-{q}@market-calendar", "earnings", "earnings", day,
        f"{sym} earnings" + (f" ({tag})" if tag else ""),
        "\n".join(lines), f"https://www.nasdaq.com/market-activity/stocks/{sym.lower()}/earnings",
    )


def src_ipos():
    min_offer = CONFIG["ipos"]["min_offer_usd"]
    first = TODAY.replace(day=1)
    months = [first, (first + dt.timedelta(days=32)).replace(day=1)]
    seen, events = set(), []
    for m in months:
        data = json.loads(fetch(f"https://api.nasdaq.com/api/ipo/calendar?date={m:%Y-%m}", "application/json"))["data"]
        upcoming = ((data.get("upcoming") or {}).get("upcomingTable") or {}).get("rows") or []
        priced = (data.get("priced") or {}).get("rows") or []
        for r, kind in [(r, "upcoming") for r in upcoming] + [(r, "priced") for r in priced]:
            deal = r.get("dealID")
            date_s = r.get("expectedPriceDate") if kind == "upcoming" else r.get("pricedDate")
            if not deal or deal in seen or not date_s:
                continue
            offer = money(r.get("dollarValueOfSharesOffered"))
            if offer < min_offer:
                continue
            seen.add(deal)
            day = dt.datetime.strptime(date_s, "%m/%d/%Y").date()
            name = nice_name(r.get("companyName") or "")
            sym = r.get("proposedTickerSymbol") or ""
            price = r.get("proposedSharePrice") or ""
            lines = [
                name,
                (f"Expected to price at ${price} per share" if kind == "upcoming" else f"Priced at ${price} per share") if price else "",
                f"Raising about {short_money(offer)}" + (f" ({r['sharesOffered']} shares)" if r.get("sharesOffered") else ""),
                f"Exchange: {r.get('proposedExchange')}" if r.get("proposedExchange") else "",
                "Expected dates often move; trading usually starts the day after pricing." if kind == "upcoming" else "",
            ]
            events.append(allday(
                f"ipo-{slug(deal)}@market-calendar", "ipos", "ipos", day,
                f"IPO {'expected' if kind == 'upcoming' else 'priced'}: {name}" + (f" ({sym})" if sym else ""),
                "\n".join(x for x in lines if x),
                "https://www.nasdaq.com/market-activity/ipos",
            ))
    last = (months[-1] + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    latest = max([dt.date.fromisoformat(e["day"]) for e in events] + [last])
    return events, [(first, latest)]


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def src_fomc():
    page = fetch("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "text/html")
    heads = list(re.finditer(r"(\d{4}) FOMC Meetings", page))
    if not heads:
        raise RuntimeError("no FOMC year sections found")
    events, years = [], []
    for i, h in enumerate(heads):
        year = int(h.group(1))
        section = page[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(page)]
        blocks = re.split(r'(?=<div class="[^"]*fomc-meeting__month)', section)
        found = False
        for b in blocks:
            m = re.search(r"fomc-meeting__month[^>]*><strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<", b, re.S)
            if not m:
                continue
            month_txt, date_txt = m.group(1).strip(), html.unescape(m.group(2)).strip()
            d = re.match(r"^(\d{1,2})(?:-(\d{1,2}))?(\*)?\s*(\(.*\))?$", date_txt)
            if not d or d.group(4):  # skip notation votes and unscheduled meetings
                continue
            parts = [MONTHS[p.strip()[:3].lower()] for p in month_txt.split("/")]
            start_day, end_day = int(d.group(1)), int(d.group(2) or d.group(1))
            end_month = parts[-1]
            start_month = parts[0]
            end_year = year + 1 if end_month < start_month else year
            decision = dt.date(end_year, end_month, end_day)
            begin = dt.date(year, start_month, start_day)
            sep = bool(d.group(3))
            found = True
            title = "Fed rate decision + press conference" + (" (with projections)" if sep else "")
            desc = [
                f"FOMC meeting {begin:%d %b} to {decision:%d %b %Y}.",
                "Statement at " + local_times(dt.datetime.combine(decision, dt.time(14, 0), NY)) + ", press conference 30 minutes later.",
            ]
            if sep:
                desc.append("Includes the Summary of Economic Projections (dot plot).")
            events.append(timed(f"fomc-{decision}@market-calendar", "fomc", "macro",
                                dt.datetime.combine(decision, dt.time(14, 0), NY), 90, title, "\n".join(desc),
                                "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"))
            rel = re.search(r"\(Released ([A-Z][a-z]+ \d{1,2}, \d{4})\)", b)
            minutes_day = dt.datetime.strptime(rel.group(1), "%B %d, %Y").date() if rel else decision + dt.timedelta(days=21)
            events.append(timed(f"fomc-minutes-{decision}@market-calendar", "fomc", "macro",
                                dt.datetime.combine(minutes_day, dt.time(14, 0), NY), 30,
                                "FOMC minutes", f"Minutes of the {decision:%d %B %Y} Fed meeting.",
                                "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"))
        if found:
            years.append(year)
    return events, [(dt.date(min(years), 1, 1), dt.date(max(years) + 1, 1, 31))]


def src_ecb():
    page = fetch("https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html", "text/html")
    events, days = [], []
    for m in re.finditer(r"<dt>\s*(\d{2}/\d{2}/\d{4})\s*</dt>\s*<dd>(.*?)</dd>", page, re.S):
        day = dt.datetime.strptime(m.group(1), "%d/%m/%Y").date()
        days.append(day)
        text = re.sub(r"<[^>]+>|\s+", " ", html.unescape(m.group(2))).strip()
        low = text.lower()
        if "monetary policy meeting" not in low or "non-monetary" in low or "press conference" not in low:
            continue
        start = dt.datetime.combine(day, dt.time(14, 15), FRANKFURT)
        events.append(timed(
            f"ecb-{day}@market-calendar", "ecb", "macro", start, 90,
            "ECB rate decision + press conference",
            f"Decision at {start:%H:%M} Frankfurt time, press conference at 14:45.\n{text}",
            "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html",
        ))
    if not days:
        raise RuntimeError("no ECB dates found")
    return events, [(min(days), max(days))]


def bea_title(s: str) -> str | None:
    if s.startswith("GDP (") or s.startswith("Gross Domestic Product,"):
        return "US " + s.replace("Gross Domestic Product", "GDP")
    if s.startswith("Personal Income and Outlays"):
        return "US PCE inflation (" + s + ")"
    if s.startswith("U.S. International Trade in Goods and Services"):
        rest = s.split(",", 1)[1].strip() if "," in s else ""
        return "US trade balance" + (f" ({rest})" if rest else "")
    return None


def src_bea():
    text = fetch("https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics", "text/calendar")
    text = re.sub(r"\r?\n[ \t]", "", text)
    events, days = [], []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        get = lambda k: (re.search(rf"^{k}[^:\n]*:(.*)$", block, re.M) or [None, ""])[1].strip()  # noqa: E731
        summary = get("SUMMARY").replace("\\,", ",").replace("\\;", ";")
        start = get("DTSTART")
        if not start:
            continue
        when = dt.datetime.strptime(start.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        days.append(when.astimezone(NY).date())
        title = bea_title(summary)
        if not title:
            continue
        events.append(timed(f"bea-{slug(get('UID') or summary + start)}@market-calendar", "bea", "macro", when, 30,
                            title, f"{summary}\nReleased by the Bureau of Economic Analysis at {local_times(when)}.",
                            "https://www.bea.gov/news/schedule"))
    if not days:
        raise RuntimeError("no BEA events found")
    return events, [(min(days), max(days))]


def make_fred_source(rid: str, label: str):
    def src():
        end = TODAY + dt.timedelta(days=150)
        page = fetch(f"https://fred.stlouisfed.org/releases/calendar?rid={rid}&vs={TODAY}&ve={end}", "text/html")
        body = page[page.find("<tbody>"): page.find("</tbody>")]
        events, current = [], None
        for m in re.finditer(r'font-weight: bold;">([^<]+)</span>|<td nowrap[^>]*>\s*([^<]*?)\s*</td>\s*<td[^>]*>\s*<a href="/release\?rid=(\d+)">', body):
            if m.group(1):
                current = dt.datetime.strptime(m.group(1).strip(), "%A %B %d, %Y").date()
                continue
            if m.group(3) != rid or current is None:
                continue
            t = m.group(2).strip()
            try:
                clock = dt.datetime.strptime(t, "%I:%M %p").time()
            except ValueError:
                clock = dt.time(7, 30)
            start = dt.datetime.combine(current, clock, CHICAGO)  # FRED lists Central Time
            events.append(timed(f"fred-{rid}-{current}@market-calendar", f"fred-{rid}", "macro", start, 30, label,
                                f"Scheduled release at {local_times(start)}.\nOnly the next release date is known ahead.",
                                f"https://fred.stlouisfed.org/releases/calendar?rid={rid}"))
        last = max([dt.date.fromisoformat(e["day"]) for e in events], default=TODAY)
        return events, [(TODAY, last)]
    return src


SOURCES = {
    "earnings": src_earnings,
    "ipos": src_ipos,
    "fomc": src_fomc,
    "ecb": src_ecb,
    "bea": src_bea,
    **{f"fred-{rid}": make_fred_source(rid, label) for rid, label in CONFIG["us_data_fred"].items()},
}


# ----------------------------------------------------------------- merge + write

def content_hash(e: dict) -> str:
    keys = ("allday", "start", "end", "summary", "description", "url")
    return hashlib.sha1(json.dumps([e[k] for k in keys]).encode()).hexdigest()


def merge(cache: dict, source: str, fresh: list[dict], coverage) -> None:
    def covered(day: str) -> bool:
        d = dt.date.fromisoformat(day)
        return any(a <= d <= b for a, b in coverage)

    old = dict(cache)
    for uid in [u for u, e in cache.items() if e["source"] == source and covered(e["day"])]:
        del cache[uid]
    for e in fresh:
        h = content_hash(e)
        prev = old.get(e["uid"])
        e["hash"] = h
        e["stamp"] = prev["stamp"] if prev and prev.get("hash") == h else NOW_STAMP
        cache[e["uid"]] = e


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def fold(line: str) -> str:
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


def write_feed(path: Path, name: str, desc: str, events: list[dict]) -> None:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//market-calendar//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(name)}", f"X-WR-CALDESC:{esc(desc)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
    ]
    for e in sorted(events, key=lambda e: (e["start"], e["summary"])):
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{e['stamp']}"]
        if e["allday"]:
            lines += [f"DTSTART;VALUE=DATE:{e['start']}", f"DTEND;VALUE=DATE:{e['end']}"]
        else:
            lines += [f"DTSTART:{e['start']}", f"DTEND:{e['end']}"]
        lines += [f"SUMMARY:{esc(e['summary'])}", "TRANSP:TRANSPARENT", f"CATEGORIES:{esc(FEEDS[e['feed']][0])}"]
        if e["description"]:
            lines.append(f"DESCRIPTION:{esc(e['description'])}")
        if e["url"]:
            lines.append(f"URL:{e['url']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(fold(l) for l in lines) + "\r\n", encoding="utf-8")


def main() -> int:
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    ok = 0
    only = set(sys.argv[1:])
    for name, fn in SOURCES.items():
        if only and name not in only:
            continue
        try:
            events, coverage = fn()
            merge(cache, name, events, coverage)
            ok += 1
            print(f"ok    {name:10} {len(events):4} events")
        except Exception as e:  # noqa: BLE001
            kept = sum(1 for ev in cache.values() if ev["source"] == name)
            print(f"FAIL  {name:10} keeping {kept} cached events ({e})", file=sys.stderr)

    cutoff = (TODAY - dt.timedelta(days=CONFIG["history_days"])).isoformat()
    cache = {u: e for u, e in cache.items() if e["day"] >= cutoff}

    CACHE_FILE.parent.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(dict(sorted(cache.items())), indent=1, ensure_ascii=False) + "\n")
    DOCS.mkdir(exist_ok=True)
    events = list(cache.values())
    for feed, (name, desc) in FEEDS.items():
        write_feed(DOCS / f"{feed}.ics", name, desc, [e for e in events if e["feed"] == feed])
    write_feed(DOCS / "all.ics", "Markets", "Earnings, IPOs, central bank decisions and US economic data.", events)
    print(f"wrote {len(events)} events")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
