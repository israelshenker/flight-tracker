"""Flight price check.

Reads the tracked route/dates from config.json, looks up every airline's lowest
nonstop fare on each, sends alerts on changes, and saves the results.

Run on GitHub in two steps so overlapping checks can't overwrite each other:
  python tracker.py search [--new]   search Google, send alerts, write run_results.json
  python tracker.py merge            add run_results.json to the newest data files
Locally, `python tracker.py` does both.
"""
import base64
import csv
import io
import json
import random
import re
import sys
import os
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import google_flights
import notify
import vault

HERE = Path(__file__).parent
CONFIG = HERE / "config.json"
DATA = HERE / "data"
LATEST = DATA / "latest.json"    # current fares: key -> {checked_at, price, airline, airlines, times}
HISTORY = DATA / "history.csv"   # change log: a row whenever an airline's fare on a route changes
STATE = DATA / "state.json"
ALERTS = DATA / "alerts.json"    # the last ALERTS_KEPT push alerts, shown by the page's Alert panel
ALERTS_KEPT = 50
RESULTS = HERE / "run_results.json"
PAGE_URL = "https://israelshenker.github.io/flight-tracker/"

FAILURE_ALERT_EVERY = timedelta(hours=12)
HEALTH_STALE = timedelta(hours=3)          # no full check this long = checks were missed
HEALTH_ALERT_EVERY = timedelta(hours=6)
DEAD_ROUTE_EVERY = timedelta(hours=23)
EMPTY_CHECKS_TO_ACCEPT = 3  # empty results this many checks in a row = the flights really are gone     # routes with no nonstops are checked daily
TIME_BUDGET_SECONDS = 28 * 60  # the GitHub job is killed at 40 min and would save nothing
LOCAL_TZ = ZoneInfo("America/New_York")  # GitHub runs in UTC; dates are Eastern
# Google's search box stops at 7 airports per side, but its search accepts all 8 of ours
# (checked against one-route-at-a-time searches: identical fares).
MAX_AIRPORTS = 8
CODE_ALIASES = {"PBI": "DJT"}  # Palm Beach was renamed PBI -> DJT in Aug 2026
# Skiplagged (hidden-city) fares: a ticket past your destination whose first flight is your
# nonstop, when it's cheaper to book it and get off at the connection. Found through
# Google's full results feed (~10 final cities per search, up to 300 tickets, cheapest
# first), in two passes:
#   one-stop tickets: connections limited to your destinations, so the 300 cheapest
#     reach well past typical nonstop fares; checked every hourly run.
#   two-stop tickets: also allow common second stops (like Newark then Cleveland); more
#     tickets compete for the 300, so it's a wider net that runs every 3 hours.
SKIP_EVERY = {1: timedelta(minutes=50), 2: timedelta(minutes=50)}  # both passes every hourly run
SKIP_MAX_SEARCHES = 200  # per run; anything left goes first next run
SKIP_SPLIT_UP_TO = 200  # dollars; see hidden_search
NORTH = {"JFK", "LGA", "EWR", "HPN", "ACY", "PHL", "TTN", "SWF", "ISP", "BOS"}
BEYOND_NORTH = ["BOS", "BUF", "ROC", "SYR", "ALB", "BTV", "PWM", "BDL", "PVD", "MHT",
                "PIT", "CLE", "DTW", "ORD", "MDW", "CMH", "CVG", "IND", "GRR", "MKE",
                "MSP", "STL", "MCI", "DEN", "RDU", "CLT", "GSO", "RIC", "ORF", "DCA",
                "IAD", "BWI", "BNA", "SDF", "AUS", "DFW", "IAH", "PHX", "LAS", "LAX",
                "SFO", "SEA", "SAN", "SLC", "MSY",
                "CHS", "SAV", "MYR", "GSP", "CAE", "JAX", "DAY", "OMA", "MEM", "BHM",
                "TYS", "SAT", "PDX", "SJC", "SNA", "SMF", "BGR", "ITH", "MDT", "ABE",
                "HNL", "OGG", "OAK", "BUR", "LGB", "ONT", "ABQ", "TUS", "ELP", "BOI",
                "DAL", "AVL", "ILM", "CHA", "CHO", "ROA", "DSM", "MSN", "OKC", "TUL",
                "LIT", "XNA", "ICT", "SJU", "BQN", "PSE", "ACK", "MVY"]
BEYOND_SOUTH = ["TPA", "JAX", "RSW", "SRQ", "EYW", "MCO", "PNS", "TLH", "SAV", "CHS",
                "MSY", "IAH", "HOU", "DFW", "AUS", "ATL", "BNA", "SJU", "STT", "STX", "CLT",
                "BQN", "PSE", "PIE", "PGD", "VPS", "ECP", "GNV", "DAB", "DAL", "BWI",
                "MDW", "STL", "MCI", "MEM", "RDU", "RIC", "ORF", "IND", "CMH"]
INTL_NORTH = ["YYZ", "YUL", "YOW", "YHZ", "YQB", "LHR", "DUB"]
INTL_SOUTH = ["NAS", "CUN", "MBJ", "SDQ", "PUJ", "GCM", "AUA", "CUR", "SXM", "BGI",
              "PTY", "SJO", "BOG", "MDE", "GUA", "SAL", "LIM", "HAV", "PLS", "UVF", "GRU",
              "KIN", "POS", "GND", "ANU", "BZE", "RTB", "SAP", "MGA", "CZM", "SJD",
              "SCL", "EZE", "UIO", "GYE"]
INTERNATIONAL = set(INTL_NORTH + INTL_SOUTH)
# US territories fly as domestic (no passport), so they are never "international" here.
US_TERRITORIES = {"SJU", "BQN", "PSE", "STT", "STX", "GUM", "SPN"}


def is_international(code):
    """Airports needing a passport from the US: the lists above, plus Canadian codes (Y..)."""
    return code not in US_TERRITORIES and (code in INTERNATIONAL or (len(code) == 3 and code.startswith("Y")))
# Second connections allowed on two-stop tickets (plus the tracked destinations).
SECOND_STOPS = ["CLE", "ORD", "IAD", "DTW", "CLT", "BOS", "PIT", "BUF", "PHL", "ATL",
                "DCA", "BWI", "MIA", "MCO", "TPA", "IAH", "DFW", "DEN"]
# Same list as FLORIDA_AIRPORTS in docs/index.html: a trip's direction ("Going" / "Coming back")
# is decided by which side of this line each leg starts on.
FLORIDA = {"FLL", "DJT", "PBI", "MIA", "MCO", "SFB", "MLB", "RSW", "APF", "TPA", "JAX", "SRQ", "PIE", "PGD",
           "EYW", "MTH", "PNS", "VPS", "ECP", "TLH", "GNV", "DAB", "LAL", "OCF", "BOW"}
TRIP_PAGES = HERE / "docs" / "t"  # one locked file per shared trip, read by docs/trip.html
HISTORY_HEADER = ["checked_at", "origin", "destination", "depart", "return", "price", "airline", "options"]


def local_today():
    return datetime.now(LOCAL_TZ).date()


def read_json(path, default):
    text = vault.read_text(path) if path != RESULTS else (path.read_text(encoding="utf-8") if path.exists() else None)
    return json.loads(text) if text is not None else default


def write_json(path, value):
    text = json.dumps(value, indent=2, sort_keys=True)
    if path == RESULTS:  # stays on the GitHub runner, never committed
        path.write_text(text, encoding="utf-8")
    else:
        vault.write_text(path, text)


def log(detail, generic=None):
    """Print a progress line. GitHub run logs are public, so when the data is encrypted
    lines naming airports or dates are replaced by the generic version (or skipped)."""
    if not vault.enabled():
        print(detail)
    elif generic:
        print(generic)


# ---- search options -------------------------------------------------------
# A watch's options are packed into a short code that is part of its key, so the
# same route can be tracked several ways: "c1" carry-on, "b2" two checked bags,
# "t6-12" leaving 6 AM to noon, "p2" two passengers (routes without it use the Settings
# number), "fUA1832" one specific flight (always last; upper case). Fares are per person.
# No options = "" (plain fare, any time).
# docs/index.html builds the same code; keep them in sync.

def options_code(w):
    code = ""
    if w.get("carry_on"):
        code += f"c{int(w['carry_on'])}"
    if w.get("checked"):
        code += f"b{int(w['checked'])}"
    t_from, t_to = int(w.get("time_from") or 0), int(w.get("time_to") or 24)
    if t_from > 0 or t_to < 24:
        code += f"t{t_from}-{t_to}"
    if w.get("adults"):
        code += f"p{int(w['adults'])}"
    if w.get("flight"):
        code += "f" + re.sub(r"[^A-Z0-9]", "", str(w["flight"]).upper())
    return code


def parse_options(code):
    code, _, flight = code.partition("f")
    c = re.search(r"c(\d)", code)
    b = re.search(r"b(\d)", code)
    t = re.search(r"t(\d+)-(\d+)", code)
    a = re.search(r"p(\d)", code)
    return {"carry_on": int(c.group(1)) if c else 0, "checked": int(b.group(1)) if b else 0,
            "time_from": int(t.group(1)) if t else 0, "time_to": int(t.group(2)) if t else 24,
            "adults": int(a.group(1)) if a else 0, "flight": flight}


def options_label(code):
    o = parse_options(code)
    parts = []
    if o["carry_on"]:
        parts.append("carry-on")
    if o["checked"]:
        parts.append(f"{o['checked']} checked bag{'s' if o['checked'] > 1 else ''}")
    if o["time_from"] and o["time_to"] < 24:
        parts.append(f"leaving {hour_label(o['time_from'])}-{hour_label(o['time_to'])}")
    elif o["time_to"] < 24:
        parts.append(f"leaving before {hour_label(o['time_to'])}")
    elif o["time_from"]:
        parts.append(f"leaving {hour_label(o['time_from'])} or later")
    if o["adults"]:
        parts.append(f"{o['adults']} passenger{'s' if o['adults'] > 1 else ''}")
    if o["flight"]:
        parts.append(f"flight {o['flight'][:2]} {o['flight'][2:]}")
    return ", ".join(parts)


def hour_label(h):
    h = h % 24 if h != 24 else 24
    if h in (0, 24):
        return "midnight"
    if h == 12:
        return "noon"
    return f"{h % 12} {'AM' if h < 12 else 'PM'}"


def time_label(hhmm):
    h, m = map(int, hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


# ---- routes ---------------------------------------------------------------

def key_of(origin, dest, depart, ret, opts=""):
    return f"{origin}-{dest}|{depart}|{ret}" + (f"|{opts}" if opts else "")


def split_key(key):
    parts = key.split("|")
    origin, dest = parts[0].split("-")
    return origin, dest, parts[1], parts[2], parts[3] if len(parts) > 3 else ""


def build_searches(cfg):
    """Tracked (origin, dest, depart, return, options) tuples, past dates dropped."""
    today = local_today().isoformat()
    seen = set()
    for w in cfg.get("watches", []):
        s = (w["origin"], w["dest"], w["depart"], w.get("return", ""), options_code(w))
        if s[2] >= today:
            seen.add(s)
    return sorted(seen, key=lambda s: (s[2], s[3], s[0], s[1], s[4]))


def chunks(items, size):
    items = sorted(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


def plan(routes):
    """Group routes into as few Google searches as possible. Departure-time windows
    are applied to the results, so only bags need separate searches.
    Returns [(depart, return, carry_on, checked, origins, dests, [routes covered])]."""
    # Per day and bag choice, departure airports that fly to the same set of destinations
    # share one search (e.g. every Florida airport -> every New York airport).
    groups = defaultdict(lambda: defaultdict(set))  # (dep, ret, carry, checked) -> origin -> dests
    wanted = defaultdict(list)                       # (dep, ret, carry, checked, o, d) -> routes
    for r in routes:
        o, d, dep, ret, opts = r
        opt = parse_options(opts)
        g = (dep, ret, opt["carry_on"], opt["checked"], opt["adults"])
        groups[g][o].add(d)
        wanted[g + (o, d)].append(r)
    out = []
    for g, by_origin in sorted(groups.items()):
        by_dests = defaultdict(set)
        for o, ds in by_origin.items():
            by_dests[frozenset(ds)].add(o)
        # Then fold small searches together when no airport would be both a departure
        # and an arrival (so FLL->JFK and MIA->PHL become one search); extra airport
        # pairs that come back are ignored.
        merged = []
        for dests, origins in sorted(by_dests.items(), key=lambda x: (-len(x[1]), sorted(x[1]))):
            for m in merged:
                os_, ds_ = m[0] | origins, m[1] | dests
                if len(os_) <= MAX_AIRPORTS and len(ds_) <= MAX_AIRPORTS and not os_ & ds_:
                    m[0], m[1] = os_, ds_
                    break
            else:
                merged.append([set(origins), set(dests)])
        for origins, dests in merged:
            for os_ in chunks(origins, MAX_AIRPORTS):
                for ds_ in chunks(dests, MAX_AIRPORTS):
                    covered = [r for o in os_ for d in ds_ for r in wanted[g + (o, d)]]
                    if covered:
                        out.append((*g, os_, ds_, covered))
    return out


def is_flagged(old, new, cfg):
    change = new - old
    dollars, percent = cfg.get("alert_dollars", 0), cfg.get("alert_percent", 0)
    if dollars and abs(change) >= dollars:
        return True
    if percent and old and abs(change) / old * 100 >= percent:
        return True
    return False


def nice_date(iso):
    d = date.fromisoformat(iso)
    return f"{d:%a %b} {d.day}"


def describe(key):
    """Alert line start, date first (user's order: date, route, price, change): "Thu Sep 24 · FLL-TTN (carry-on)"."""
    o, d, dep, ret, opts = split_key(key)
    label = options_label(opts)
    return (f"{nice_date(dep)}" + (f", return {nice_date(ret)}" if ret else "")
            + f" · {o}-{d}" + (f" ({label})" if label else ""))


# ---- searching ------------------------------------------------------------

class EmptyResults(Exception):
    """Google returned no flights where there clearly should be some."""


def per_person(itineraries, n):
    """Google prices a search for n passengers as the total; the tracker keeps fares per person."""
    return itineraries if n <= 1 else [{**it, "price": round(it["price"] / n)} for it in itineraries]


def run_search(dep, ret, origins, dests, adults, carry_on, checked, expect_flights):
    for attempt in range(3):
        try:
            found = google_flights.search(dep, origins, dests, ret, adults, carry_on, checked)
            # Google occasionally sends an empty page; don't record that as "no nonstops".
            if not found and expect_flights:
                raise EmptyResults("no flights returned")
            return found
        except Exception as e:
            if attempt == 2:
                raise
            log(f"  retrying after {type(e).__name__}: {e}", f"  retrying after {type(e).__name__}")
            time.sleep(8 + random.random() * 8)


def in_window(opt, departs, flight):
    """Does a flight fit a route's options: its specific flight if it has one, else its time window."""
    if opt["flight"]:
        return flight == opt["flight"]
    h, m = map(int, departs.split(":"))
    return opt["time_from"] * 60 <= h * 60 + m < opt["time_to"] * 60


def fares_for(route, itineraries):
    """Each airline's lowest fare on this route within its departure window (or its one
    flight), the departure times at that fare, and every matching flight for the page."""
    o, d, _, _, opts = route
    opt = parse_options(opts)
    airlines, times, flights = {}, {}, {}
    for it in itineraries:
        if (CODE_ALIASES.get(it["origin"], it["origin"]), CODE_ALIASES.get(it["dest"], it["dest"])) != (o, d):
            continue
        if not in_window(opt, it["departs"], it.get("flight", "")):
            continue
        fid = it.get("flight") or f'{it["airline"]} {it["departs"]}'
        if it["price"] < flights.get(fid, {}).get("price", 10**9):
            flights[fid] = {"flight": it.get("flight", ""), "airline": it["airline"], "departs": it["departs"], "price": it["price"]}
        a, p = it["airline"], it["price"]
        if p < airlines.get(a, 10**9):
            airlines[a], times[a] = p, []
        if p == airlines[a] and it["departs"] not in times[a]:
            times[a].append(it["departs"])
    return {"airlines": airlines, "times": {a: sorted(t) for a, t in times.items()},
            "flights": sorted(flights.values(), key=lambda f: f["departs"])}


def hidden_search(cfg, latest, fares, errors, started, adults, stamp):
    """Add skiplagged fares to fares[key]["hidden1"] (one-stop tickets) and
    fares[key]["hidden2"] (two-stop tickets) for routes due a check.
    Only one-way routes without checked bags qualify: a checked bag would fly to the
    ticket's final city, and skipping a flight cancels the rest of a round trip."""
    now = datetime.fromisoformat(stamp)
    no_skip = skiplagged_off_keys(cfg)
    groups = defaultdict(list)  # (dep, carry, northbound, pass) -> routes due
    for key in fares:
        o, d, dep, ret, opts = split_key(key)
        opt = parse_options(opts)
        if ret or opt["checked"] or key in no_skip:
            continue
        for stops in (1, 2):
            last = (latest.get(key) or {}).get(f"hidden{stops}_checked_at")
            if not last or now - datetime.fromisoformat(last) >= SKIP_EVERY[stops]:
                # International endings: always for international trips (a passport is needed
                # anyway); for domestic trips only if the user turned that on (off by default).
                # US territories count as domestic.
                intl_ok = is_international(d) or bool(cfg.get("skiplagged_intl_domestic"))
                groups[(dep, opt["carry_on"], opt["adults"] or adults, d in NORTH, intl_ok, stops)].append((o, d, dep, ret, opts))
    budget = {"left": SKIP_MAX_SEARCHES}
    skip_started = time.monotonic()

    def age(g):
        return min((latest.get(key_of(*r)) or {}).get(f"hidden{g[0][5]}_checked_at", "") for r in g[1])

    for (dep, carry, pax, north, intl, stops), routes in sorted(groups.items(), key=lambda g: (g[0][5], age(g))):
        origins = sorted({r[0] for r in routes})
        dests = sorted({r[1] for r in routes})
        finals = (BEYOND_NORTH + (INTL_NORTH if intl else [])) if north else (BEYOND_SOUTH + (INTL_SOUTH if intl else []))
        finals = [f for f in finals if f not in dests and f not in origins]
        via = dests if stops == 1 else sorted(set(dests) | {c for c in SECOND_STOPS if c not in origins})
        nonstops = [min(fares[key_of(*r)]["airlines"].values()) for r in routes if fares[key_of(*r)]["airlines"]]
        # Tickets at or above every nonstop fare don't matter. Chasing very expensive routes
        # costs many extra searches for little, so stop splitting above SKIP_SPLIT_UP_TO.
        need = min(max(nonstops), SKIP_SPLIT_UP_TO) if nonstops and stops == 1 else 0
        planned = len(chunks(finals, google_flights.FEED_MAX_CITIES)) * len(chunks(origins, MAX_AIRPORTS))
        if budget["left"] < planned:
            log(f"  skiplagged {stops}-stop {dep}: waits for the next run (search limit)", "  some skiplagged searches wait for the next run (search limit)")
            continue
        found, complete = [], True
        try:
            for os_ in chunks(origins, MAX_AIRPORTS):
                for fs in chunks(finals, google_flights.FEED_MAX_CITIES):
                    if time.monotonic() - started > TIME_BUDGET_SECONDS:
                        print("  Time budget reached; skipping remaining skiplagged searches.")
                        return
                    tickets, whole = feed_tickets(dep, os_, fs, via, pax, carry, stops, need, budget)
                    found += tickets
                    complete &= whole
        except SearchLimit:
            log(f"  skiplagged {stops}-stop {dep}: waits for the next run (search limit)", "  some skiplagged searches wait for the next run (search limit)")
            continue
        except Exception as e:
            errors.append(f"skiplagged {stops}-stop {dep}: {type(e).__name__}")
            log(f"  ERROR skiplagged {stops}-stop {dep}: {e}", f"  ERROR in a skiplagged search: {type(e).__name__}")
            continue  # keep each route's previous skiplagged fare rather than claim there is none
        cheaper = 0
        for r in routes:
            o, d, _, _, opts = r
            opt = parse_options(opts)
            best = None
            for it in found:
                first = it["legs"][0]
                if len(it["legs"]) != stops + 1 or (CODE_ALIASES.get(first[0], first[0]), CODE_ALIASES.get(first[1], first[1])) != (o, d):
                    continue
                if is_international(it["legs"][-1][1]) and not intl:
                    continue
                if in_window(opt, first[2], first[3] if len(first) > 3 else "") and (best is None or it["price"] < best["price"]):
                    final = it["legs"][-1][1]
                    best = {"price": it["price"], "airline": it["airline"], "departs": first[2], "final": final,
                            "stops": stops, "via": [leg[1] for leg in it["legs"][1:-1]],
                            "flights": [leg[3] for leg in it["legs"] if len(leg) > 3],
                            "international": is_international(final)}
            f = fares[key_of(*r)]
            f[f"hidden{stops}"] = best
            nonstop = min(f["airlines"].values()) if f["airlines"] else None
            cheaper += bool(best and (not nonstop or best["price"] < nonstop))
        note = "" if complete else " (search limit reached; some pricier tickets may be missing)"
        log(f"  skiplagged {stops}-stop {dep}: {len(finals)} cities, {len(found)} tickets, "
              f"cheaper than the nonstop on {cheaper} of {len(routes)} routes{note}",
            f"  skiplagged {stops}-stop search: {len(found)} tickets, cheaper on {cheaper} of {len(routes)} routes{note}")
    print(f"  skiplagged: {SKIP_MAX_SEARCHES - budget['left']} of {SKIP_MAX_SEARCHES} searches used, "
          f"{time.monotonic() - skip_started:.0f}s")


class SearchLimit(Exception):
    """This run's skiplagged searches are used up."""


def feed_tickets(dep, origins, finals, via, adults, carry, stops, need, budget):
    """Tickets from origins to finals, and whether nothing relevant was cut off.
    Google returns at most 300, cheapest first; if the cutoff falls below the highest
    nonstop fare in play (`need`), cheaper-than-nonstop tickets might be missing, so the
    search is split in half and repeated while the search limit allows."""
    for attempt in range(3):
        if budget["left"] <= 0:
            raise SearchLimit()
        budget["left"] -= 1
        time.sleep(1.5 + random.random() * 1.5)  # be gentle with Google, but fit every date in the hour
        try:
            tickets = google_flights.feed_search(dep, origins, finals, adults, carry, 0, stops, via)
            break
        except google_flights.Blocked as e:
            if attempt == 2:
                raise
            log(f"  retrying skiplagged search after: {e}", "  retrying a skiplagged search")
            time.sleep(10 + random.random() * 10)
    tickets = per_person(tickets, adults)  # `need` is a per-person nonstop fare
    if len(tickets) < google_flights.FEED_CAP:
        return tickets, True
    cutoff = sorted(t["price"] for t in tickets)[-2]
    if cutoff >= need or budget["left"] < 2 or (len(finals) == 1 and len(origins) == 1):
        return tickets, cutoff >= need
    if len(finals) > 1:
        halves = [(origins, finals[:len(finals) // 2]), (origins, finals[len(finals) // 2:])]
    else:
        halves = [(origins[:len(origins) // 2], finals), (origins[len(origins) // 2:], finals)]
    out, whole = [], True
    for os_, fs in halves:
        t, w = feed_tickets(dep, os_, fs, via, adults, carry, stops, need, budget)
        out += t
        whole &= w
    return out, whole


def skiplagged_off_keys(cfg):
    """Routes whose skiplagged fares are turned off: one route at a time, or a whole date."""
    off_dates = set(cfg.get("skiplagged_off_dates", []))
    return {key_of(w["origin"], w["dest"], w["depart"], w.get("return", ""), options_code(w))
            for w in cfg.get("watches", []) if w.get("no_skiplagged") or w["depart"] in off_dates}


def combined_hidden(f, prev):
    """The cheaper of a route's one-stop and two-stop skiplagged fares, using this run's
    result for a pass that ran and the saved one for a pass that didn't."""
    options = []
    for stops in (1, 2):
        k = f"hidden{stops}"
        h = f[k] if k in f else (prev or {}).get(k)
        if h:
            options.append(h)
    return min(options, key=lambda h: h["price"]) if options else None


def new_alert(kind, title, items=None, text="", details=""):
    """A saved copy of a push alert. The push's "Open alert" button opens it on the page (#a=<id>).
    items: one per line, {key, what, tone, price, sub}. text: a plain explanation instead."""
    stamp = datetime.now(timezone.utc)
    alert = {"id": stamp.strftime("%Y%m%dT%H%M%S") + f"-{kind}", "at": stamp.isoformat(timespec="seconds"),
             "kind": kind, "title": title, "items": items or [], "text": text}
    if details:
        alert["details"] = details
    return alert


def alert_link(alert):
    return f"{PAGE_URL}#a={alert['id']}"


def save_alerts(new):
    """Add alerts to data/alerts.json, newest first, keeping the last ALERTS_KEPT."""
    if not new:
        return
    old = read_json(ALERTS, [])
    ids = {a["id"] for a in new}
    write_json(ALERTS, (sorted(new, key=lambda a: a["at"], reverse=True) + [a for a in old if a["id"] not in ids])[:ALERTS_KEPT])


def route_link(key):
    """Page link that opens this route's date and card."""
    return f"{PAGE_URL}#r={urllib.parse.quote(key, safe='')}"


def search(only_new=False):
    """Search fares, send alerts, and write this run's results to RESULTS."""
    if not CONFIG.exists():
        print("No config.json yet. Add flights on the web page first.")
        return 0
    cfg = read_json(CONFIG, {})
    latest = read_json(LATEST, {})
    state = read_json(STATE, {})
    adults = cfg.get("adults", 1)
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")

    # If the last full check is old, checks were missed (GitHub or the tracker had trouble).
    health_alert = False
    alerts_out = []
    last_full = (state.get("last_run") or {}).get("at")
    if not only_new and last_full and now - datetime.fromisoformat(last_full) > HEALTH_STALE:
        last_alert = state.get("last_health_alert")
        if not last_alert or now - datetime.fromisoformat(last_alert) >= HEALTH_ALERT_EVERY:
            hours = (now - datetime.fromisoformat(last_full)).total_seconds() / 3600
            text = (f"The last successful full check before this one was {hours:.0f} hours ago. "
                    f"Checks are running again now.")
            alert = new_alert("missed", "Checks were missed", text=text)
            alerts_out.append(alert)
            notify.send("Flight tracker: checks were missed", f"{text}\n\nAll fares: {PAGE_URL}", click=alert_link(alert))
            health_alert = True

    routes = build_searches(cfg)
    if only_new:
        todo = [r for r in routes if key_of(*r) not in latest]
    else:
        # Routes with no nonstop flights only need a daily look.
        todo = [r for r in routes if not dead_recently(latest.get(key_of(*r)), now)]
    # Round trips are also priced as two one-way tickets; those searches ride along.
    extras = set()
    for o, d, dep, ret, opts in todo:
        if ret:
            opt = parse_options(opts)
            back_opts = options_code({"carry_on": opt["carry_on"], "checked": opt["checked"], "adults": opt["adults"]})
            extras.add((o, d, dep, "", opts))
            extras.add((d, o, ret, "", back_opts))
    extras -= set(routes)
    searches = plan(sorted(set(todo) | extras))
    # Longest-unchecked first, so anything cut off by the time budget goes first next run.
    searches.sort(key=lambda s: min((latest.get(key_of(*r)) or {}).get("checked_at", "") for r in s[7]))
    print(f"{len(todo)} of {len(routes)} route/dates in {len(searches)} searches"
          f"{f' (plus {len(extras)} one-way legs for round trips)' if extras else ''}"
          f"{f'; {len(routes) - len(todo)} with no nonstops wait for their daily check' if not only_new and len(todo) < len(routes) else ''}")

    started = time.monotonic()
    fares, errors, done, empty_streaks = {}, [], 0, {}
    for i, (dep, ret, carry, checked, pax, origins, dests, covered) in enumerate(searches):
        if time.monotonic() - started > TIME_BUDGET_SECONDS:
            print(f"  Time budget reached; {len(searches) - i} searches left for next run.")
            break
        if i:
            time.sleep(3 + random.random() * 4)  # be gentle with Google
        bags = f" carry-on {carry} checked {checked}" if carry or checked else ""
        label = f"{','.join(origins)} -> {','.join(dests)} {dep}{' / ' + ret if ret else ''}{bags}"
        try:
            had_fares = any((latest.get(key_of(*r)) or {}).get("price") for r in covered)
            n = pax or adults  # route's own passenger count, or the Settings number
            itineraries = per_person(run_search(dep, ret, origins, dests, n, carry, checked,
                                     had_fares or len(covered) >= 10), n)
        except EmptyResults:
            # Google sometimes sends an empty page by mistake, so routes that had fares aren't
            # marked "no nonstops" on the first empty result. After EMPTY_CHECKS_TO_ACCEPT empty
            # checks in a row they are (their flights are gone: sold out, canceled, departed).
            done += 1
            for r in covered:
                k = key_of(*r)
                streak = (latest.get(k) or {}).get("empty_streak", 0) + 1
                if streak >= EMPTY_CHECKS_TO_ACCEPT:
                    fares[k] = fares_for(r, [])
                else:
                    empty_streaks[k] = streak
            log(f"  {label}: no flights returned (counted toward {EMPTY_CHECKS_TO_ACCEPT} empty checks in a row)",
                f"  search {i + 1}: no flights returned (counted toward {EMPTY_CHECKS_TO_ACCEPT} empty checks in a row)")
            continue
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}")
            log(f"  ERROR {label}: {e}", f"  ERROR in a search: {type(e).__name__}")
            continue
        done += 1
        priced = 0
        for r in covered:
            fares[key_of(*r)] = f = fares_for(r, itineraries)
            priced += bool(f["airlines"]) and r not in extras
        log(f"  {label}: {len(itineraries)} flights, {priced} of {len([r for r in covered if r not in extras])} tracked routes have nonstops",
            f"  search {i + 1}: {len(itineraries)} flights, {priced} tracked routes with nonstops")

    legs = {k: fares.pop(k) for k in [key_of(*r) for r in extras] if k in fares}
    for key, f in fares.items():
        o, d, dep, ret, opts = split_key(key)
        if not ret:
            continue
        opt = parse_options(opts)
        leg_of = lambda k: legs.get(k) or fares.get(k)  # a leg may also be a route you track
        out = leg_of(key_of(o, d, dep, "", opts))
        back = leg_of(key_of(d, o, ret, "", options_code({"carry_on": opt["carry_on"], "checked": opt["checked"], "adults": opt["adults"]})))
        if out and back and out["airlines"] and back["airlines"]:
            pick = lambda leg: min(leg["flights"], key=lambda x: x["price"]) if leg["flights"] else None
            po, pb = pick(out), pick(back)
            if po and pb:
                f["one_ways"] = {"out": po, "back": pb, "total": po["price"] + pb["price"]}

    if cfg.get("skiplagged", True):
        hidden_search(cfg, latest, fares, errors, started, adults, stamp)

    # ---- alerts: one line per route, only for what changed ----
    targets = {}
    for w in cfg.get("watches", []):
        if w.get("alert_below"):
            targets[key_of(w["origin"], w["dest"], w["depart"], w.get("return", ""), options_code(w))] = float(w["alert_below"])
    no_skip = skiplagged_off_keys(cfg)
    # Pushes: the same lines as the email with one word in front (user's request): UP, DOWN,
    # BELOW, NEW (nonstop), SKIPLAGGED or ONE-WAYS.
    lines, push_lines, items, counts, price_email_day = [], [], [], defaultdict(int), None

    def item(key, what, tone, price, sub):
        o, d, dep, ret, opts = split_key(key)
        label = options_label(opts)
        date = nice_date(dep) + (f", return {nice_date(ret)}" if ret else "")
        items.append({"key": key, "what": what, "tone": tone, "price": price,
                      "sub": " · ".join(x for x in (date, label, sub) if x)})

    def at(times):
        return f" at {', '.join(time_label(t) for t in times)}" if times else ""

    for key, f in sorted(fares.items(), key=lambda kv: min(kv[1]["airlines"].values()) if kv[1]["airlines"] else 10**9):
        before = latest.get(key)
        prev = (before or {}).get("price")
        new = min(f["airlines"].values()) if f["airlines"] else None
        link = f"\n    {route_link(key)}"
        if new:
            cheapest = min(f["airlines"], key=f["airlines"].get)
            detail = f"{cheapest}{at(f['times'].get(cheapest, []))}"
            target = targets.get(key)
            low = (before or {}).get("low") or prev
            tags = []
            if before is not None and prev is None and before.get("checked_at"):
                tags.append("NEW NONSTOP")
                counts["new nonstop"] += 1
            if target and new < target and not (prev and prev < target):
                tags.append(f"BELOW ${target:g}")
                counts["below target"] += 1
            if cfg.get("alert_new_low", True) and low and new < low:
                tags.append("NEW LOW")
                counts["new low"] += 1
            if prev and new != prev and is_flagged(prev, new, cfg):
                tags.append(f"{'DOWN' if new < prev else 'UP'} ${abs(new - prev)}")
                counts["down" if new < prev else "up"] += 1
            if tags:
                was = f" (was ${prev})" if prev else ""
                lines.append(f"{describe(key)}: ${new} · {', '.join(tags)}{was} · {detail}{link}")
                word = "DOWN" if prev and new < prev else "UP" if prev and new > prev else tags[0].split()[0]
                push_lines.append(f"{word} {describe(key)}: ${new} · {', '.join(tags)}{was} · {detail}")
                tone = "up" if tags == [t for t in tags if t.startswith("UP")] else "down"
                item(key, " · ".join(tags), tone, new, detail + (f" · was ${prev}" if prev else ""))
        ow = f.get("one_ways")
        if ow and new and ow["total"] < new:
            before_ow = (before or {}).get("one_ways") or {}
            if not before_ow or is_flagged(before_ow.get("total", 0), ow["total"], cfg):
                lines.append(f"{describe(key)}: ${ow['total']} · TWO ONE-WAYS, ${new - ow['total']} under the round trip · "
                             f"{ow['out']['airline']} out at {time_label(ow['out']['departs'])} + "
                             f"{ow['back']['airline']} back at {time_label(ow['back']['departs'])}{link}")
                push_lines.append("ONE-WAYS " + lines[-1].removesuffix(link))
                item(key, f"TWO ONE-WAYS ${ow['total']}", "down", ow["total"],
                     f"{ow['out']['airline']} out {time_label(ow['out']['departs'])} + {ow['back']['airline']} back "
                     f"{time_label(ow['back']['departs'])} · ${new - ow['total']} under the round trip")
                counts["two one-ways"] += 1
        h = combined_hidden(f, before)
        if h and h.get("international") and not is_international(split_key(key)[1]) and not cfg.get("skiplagged_intl_domestic"):
            h = None  # international ending on a domestic trip, with that option off
        if h and key not in no_skip and (not new or h["price"] < new):
            prior = (before or {}).get("hidden") or {}
            if not prior.get("price") or is_flagged(prior["price"], h["price"], cfg):
                vs = f", ${new - h['price']} under the nonstop" if new else ""
                intl = " (international ticket: passport needed)" if h.get("international") else ""
                lines.append(f"{describe(key)}: ${h['price']} · SKIPLAGGED{vs} · {h['airline']} at {time_label(h['departs'])}, "
                             f"ticket to {h['final']}, get off at {split_key(key)[1]}{intl}{link}")
                push_lines.append("SKIPLAGGED " + lines[-1].removesuffix(link))
                via = f" via {', '.join(h['via'])}" if h.get("via") else ""
                item(key, f"SKIPLAGGED ${h['price']}", "skip", h["price"],
                     f"{h['airline']} {time_label(h['departs'])} · ticket to {h['final']}{via}"
                     + (f" · ${new - h['price']} under the nonstop" if new else "")
                     + (" · international: passport needed" if h.get("international") else ""))
                counts["skiplagged"] += 1
    if lines:
        title = "; ".join(f"{n} {what}" for what, n in counts.items())
        alert = new_alert("prices", title, items=items)
        alerts_out.append(alert)
        # Emails keep a link per line; the push's "Open alert" button opens this alert on the page.
        # Price emails for one day (Eastern) share a Gmail conversation (user's request).
        day = local_today().isoformat()
        price_email_day = day
        notify.send("Flight prices: " + title, "\n".join(lines) + f"\n\nAll fares: {PAGE_URL}", click=alert_link(alert),
                    push_body="\n".join(push_lines) + f"\n\nAll fares: {PAGE_URL}", thread=(f"prices-{day}", f"Flight prices {nice_date(day)}", state.get("price_email_day") != day))

    # If most searches failed, Google is probably blocking us. Say so, but not every hour.
    attempted = done + len(errors)
    failure_alert = False
    if attempted and len(errors) >= max(1, attempted / 2):
        last = state.get("last_failure_alert")
        if not last or now - datetime.fromisoformat(last) >= FAILURE_ALERT_EVERY:
            text = f"{len(errors)} of {attempted} Google searches failed. Google may be blocking the free reader."
            alert = new_alert("searches", "Searches failing", text=text)
            alerts_out.append(alert)
            notify.send("Flight tracker: searches failing", text + "\n\n" + "\n".join(errors[:20]), click=alert_link(alert))
            failure_alert = True

    write_json(RESULTS, {
        "stamp": stamp, "fares": fares, "price_email_day": price_email_day, "failure_alert": failure_alert, "health_alert": health_alert,
        "empty_streaks": empty_streaks,
        "alerts": alerts_out,
        "last_run": {"at": stamp, "searched": len(fares), "total": len(routes), "searches": attempted,
                     "errors": errors[:20], "only_new": only_new},
    })
    print(f"{len(lines)} alert lines ({dict(counts)}), {len(errors)} errors")
    return 0


def dead_recently(entry_, now):
    """A route with no nonstop flights that was looked at within the last day. Routes that
    had fares before (no longer available) stay hourly, since flights can come back."""
    if not entry_ or entry_.get("price") is not None or not entry_.get("checked_at"):
        return False
    if entry_.get("low") or entry_.get("last_price"):
        return False
    return now - datetime.fromisoformat(entry_["checked_at"]) < DEAD_ROUTE_EVERY


def failed():
    """Run by GitHub when a check crashes: tell the user, at most every HEALTH_ALERT_EVERY."""
    state = read_json(STATE, {})
    now = datetime.now(timezone.utc)
    last = state.get("last_health_alert")
    if last and now - datetime.fromisoformat(last) < HEALTH_ALERT_EVERY:
        print("Failure already reported recently.")
        return 0
    run = "{}/{}/actions/runs/{}".format(os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
                                        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""))
    text = (f"The hourly price check crashed, so fares weren't updated this time. It will try again "
            f"next hour; you'll hear again only if it's still failing in {HEALTH_ALERT_EVERY.seconds // 3600} hours.")
    alert = new_alert("failed", "A check failed", text=text, details=run)
    save_alerts([alert])
    notify.send("Flight tracker: a check failed", f"{text}\n\nDetails: {run}", click=alert_link(alert))
    state["last_health_alert"] = now.isoformat(timespec="seconds")
    write_json(STATE, state)
    return 0


def day_moves(stamp, price, prev):
    """The nonstop fare's changes over the last 24 hours, as [time, price] points, plus the
    point in effect 24 hours ago. The page colors fares by it and marks fares that moved."""
    trail = list((prev or {}).get("moves") or
                 ([[prev["checked_at"], prev.get("price")]] if (prev or {}).get("checked_at") else []))
    if not trail or trail[-1][1] != price:
        trail.append([stamp, price])
    return last_day(trail, stamp)


def last_day(trail, stamp):
    cutoff = (datetime.fromisoformat(stamp) - timedelta(hours=24)).isoformat(timespec="seconds")
    before = [i for i, (at, _) in enumerate(trail) if at <= cutoff]
    return trail[before[-1]:] if before else trail


def utc(at):
    """Change-log times and the page's history_from times in one comparable form."""
    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")


def log_trails(rows):
    """Replay the change log: each route key's cheapest nonstop over time, as [time, price]
    points where it changed. A row with an airline and no price = that airline dropped out;
    no airline and no price = no nonstops at all."""
    fares, trails = defaultdict(dict), defaultdict(list)
    for row in rows:
        k = key_of(row["origin"], row["destination"], row["depart"], row["return"], row["options"])
        if not row["price"]:
            if row["airline"]:
                fares[k].pop(row["airline"], None)
            else:
                fares[k].clear()
        else:
            fares[k][row["airline"]] = int(float(row["price"]))
        cheapest = min(fares[k].values()) if fares[k] else None
        at, t = utc(row["checked_at"]), trails[k]
        if t and t[-1][0] == at:
            t[-1][1] = cheapest  # several airlines logged in the same check
            if len(t) > 1 and t[-2][1] == cheapest:
                t.pop()
        elif not t or t[-1][1] != cheapest:
            t.append([at, cheapest])
    return trails


def watch_trail(w, trails):
    """A route's fare points under its current bags/times/passengers. Earlier settings aren't
    included: a settings change isn't a market move (user's choice), so moves restart there."""
    return trails.get(key_of(w["origin"], w["dest"], w["depart"], w.get("return", ""), options_code(w)), [])


def moves_from_log(e, trail):
    """`moves` for an entry from its log points, ending on the entry's current fare."""
    t = [list(p) for p in trail if p[0] <= e["checked_at"]]
    if not t or t[-1][1] != e.get("price"):
        t.append([e["checked_at"], e.get("price")])
    return last_day(t, e["checked_at"])


def read_log():
    return list(csv.DictReader(io.StringIO(vault.read_text(HISTORY) or "")))


def moves_from_history(latest, cfg):
    """One-time rebuild of `moves` from the change log (including earlier bag/time settings)."""
    trails = log_trails(read_log())
    m_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    for w in cfg.get("watches", []):
        e = latest.get(key_of(w["origin"], w["dest"], w["depart"], w.get("return", ""), options_code(w)))
        if e and e.get("checked_at"):
            e["moves"] = moves_from_log(e, watch_trail(w, trails))
    m = [e["moves"] for e in latest.values() if e.get("moves") and e.get("price") is not None]
    print(f"  24-hour moves filled from the change log: {len(m)} priced routes, "
          f"{sum(t[0][1] is not None and t[0][1] != t[-1][1] for t in m)} changed, "
          f"{sum(len(t) > 1 for t in m)} moved; "
          f"{sum(1 for w in cfg.get('watches', []) for h in w.get('history_from', []) if utc(h['until']) > m_cutoff)} "
          f"bag/time/passenger changes in the last 24 hours")


def entry(stamp, f, prev=None):
    airlines = f["airlines"]
    cheapest = min(airlines, key=airlines.get) if airlines else ""
    e = {"checked_at": stamp, "airlines": airlines, "times": f.get("times", {}),
         "flights": f.get("flights", []), "price": airlines.get(cheapest), "airline": cheapest,
         "prev_price": (prev or {}).get("price")}  # the page colors the fare by this change
    # When fares disappear, remember the last fare and when it was seen ("No Longer Available").
    if e["price"] is not None:
        e["last_price"], e["last_seen_at"] = e["price"], stamp
    else:
        for k in ("last_price", "last_seen_at"):
            if (prev or {}).get(k) is not None:
                e[k] = prev[k]
        if (prev or {}).get("price") is not None and "last_price" not in e:
            e["last_price"], e["last_seen_at"] = prev["price"], prev.get("checked_at")
    e["moves"] = day_moves(stamp, e["price"], prev)
    lows = [p for p in ((prev or {}).get("low"), (prev or {}).get("price"), e["price"]) if p]
    if lows:
        e["low"] = min(lows)  # lowest fare seen since tracking started
    if f.get("one_ways"):
        e["one_ways"] = f["one_ways"]
    # A skiplagged pass that didn't run this time keeps its last result.
    for stops in (1, 2):
        k = f"hidden{stops}"
        if k in f:
            checked_at, h = stamp, f[k]
        else:
            checked_at, h = (prev or {}).get(f"{k}_checked_at"), (prev or {}).get(k)
        if checked_at:
            e[f"{k}_checked_at"] = checked_at
        if h:
            e[k] = h
    hidden = combined_hidden(f, prev)
    if hidden:
        e["hidden"] = hidden
    return e


def trip_going(legs):
    """{watch key: True if "Going"}: legs that start on the same side (Florida or not) as the
    trip's earliest leg are Going; the rest are Coming back. Round trips count as Going."""
    if not legs:
        return {}
    first = min(legs, key=lambda w: (w["depart"], w["origin"]))
    side = first["origin"] in FLORIDA
    return {watch_key(w): bool(w.get("return")) or (w["origin"] in FLORIDA) == side for w in legs}


def watch_key(w):
    return key_of(w["origin"], w["dest"], w["depart"], w.get("return", ""), options_code(w))


def google_link(w, adults):
    o = parse_options(options_code(w))
    tfs = google_flights.build_tfs(w["depart"], [w["origin"]], [w["dest"]], w.get("return", ""),
                                   o["adults"] or adults, o["carry_on"], o["checked"])
    return f"{google_flights.URL}/search?" + urllib.parse.urlencode({"tfs": tfs, "hl": "en", "curr": "USD"})


def trip_key(t):
    k = t["share"]["key"]
    return base64.urlsafe_b64decode(k + "=" * (-len(k) % 4))


def trip_legs(cfg, latest, t, trails):
    """The trip's route/dates (today on) as the friend page and friend emails show them."""
    today = local_today().isoformat()
    adults = cfg.get("adults", 1)
    no_skip = skiplagged_off_keys(cfg)
    legs = [w for w in cfg.get("watches", []) if t["id"] in (w.get("trips") or []) and w["depart"] >= today]
    going = trip_going(legs)
    out = []
    for w in legs:
        k = watch_key(w)
        e = latest.get(k) or {}
        leg = {"key": k, "o": w["origin"], "d": w["dest"], "dep": w["depart"], "ret": w.get("return", ""),
               "opts": options_label(options_code(w)), "going": going.get(k, True),
               "checked_at": e.get("checked_at"), "price": e.get("price"), "airline": e.get("airline"),
               "times": (e.get("times") or {}).get(e.get("airline"), []),
               "airlines": e.get("airlines") or {}, "moves": e.get("moves") or [],
               "history": [p for p in trails.get(k, []) if p[0] <= (e.get("checked_at") or "9999")],
               "low": e.get("low"), "last_price": e.get("last_price"), "link": google_link(w, adults)}
        h = e.get("hidden")
        if (t.get("skiplagged") and h and cfg.get("skiplagged", True) and k not in no_skip
                and not (h.get("international") and not is_international(w["dest"])
                         and not cfg.get("skiplagged_intl_domestic"))
                and (e.get("price") is None or h["price"] < e["price"])):
            leg["skip"] = {x: h.get(x) for x in ("price", "airline", "departs", "final", "via", "stops", "international")}
        out.append(leg)
    return out


def write_trip_pages(cfg, latest):
    """For every shared trip, docs/t/<share id>.json: that trip's fares only, AES-256-GCM with
    the trip's own key. The key is only in config.json (encrypted) and in the link the user
    shares (after the #, which browsers never send to a server). Files of trips no longer
    shared are deleted, so old links stop working."""
    trips = [t for t in cfg.get("trips", []) if (t.get("share") or {}).get("id") and t["share"].get("key")]
    TRIP_PAGES.mkdir(parents=True, exist_ok=True)
    keep = set()
    if trips:
        trails = log_trails(read_log())
        for t in trips:
            snap = {"v": 1, "name": t.get("name", ""), "person": t.get("person", ""),
                    "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "skiplagged": bool(t.get("skiplagged")), "legs": trip_legs(cfg, latest, t, trails),
                    "signup": cfg.get("signup_topic")}  # where the page's "Email me updates" form posts (see friends.py)
            iv = os.urandom(12)
            box = {"v": 1, "iv": base64.b64encode(iv).decode(),
                   "data": base64.b64encode(AESGCM(trip_key(t)).encrypt(iv, json.dumps(snap).encode(), None)).decode()}
            name = re.sub(r"[^a-z0-9]", "", t["share"]["id"].lower())
            (TRIP_PAGES / f"{name}.json").write_text(json.dumps(box), encoding="utf-8")
            keep.add(f"{name}.json")
    for f in TRIP_PAGES.glob("*.json"):
        if f.name not in keep:
            f.unlink()
    print(f"  trip pages: {len(keep)} shared")


def drop_past_dates(cfg):
    """Remove flights whose departure date has passed (Eastern time) from config.json and
    their rows from history.csv, so neither keeps growing. Git history still has them."""
    today = local_today().isoformat()
    watches = [w for w in cfg.get("watches", []) if w["depart"] >= today]
    off = [d for d in cfg.get("skiplagged_off_dates", []) if d >= today]
    if len(watches) != len(cfg.get("watches", [])) or off != cfg.get("skiplagged_off_dates", off):
        print(f"  dropping {len(cfg.get('watches', [])) - len(watches)} past route/dates")
        cfg["watches"] = watches
        if "skiplagged_off_dates" in cfg:
            cfg["skiplagged_off_dates"] = off
        vault.write_text(CONFIG, json.dumps(cfg, indent=2) + "\n")
    text = vault.read_text(HISTORY)
    if text is not None:
        rows = list(csv.reader(io.StringIO(text)))
        keep = rows[:1] + [r for r in rows[1:] if len(r) > 3 and r[3] >= today]
        if len(keep) != len(rows):
            out = io.StringIO()
            csv.writer(out, lineterminator="\n").writerows(keep)
            vault.write_text(HISTORY, out.getvalue())


def merge():
    """Apply RESULTS to the newest data files. Safe to repeat after re-syncing with GitHub."""
    if not RESULTS.exists():
        return 0
    res = read_json(RESULTS, {})
    DATA.mkdir(exist_ok=True)
    cfg = read_json(CONFIG, {})
    latest = read_json(LATEST, {})
    state = read_json(STATE, {})
    stamp = res["stamp"]

    rows = []
    for key, f in res["fares"].items():
        prev = latest.get(key)
        if prev and prev.get("checked_at", "") > stamp:
            continue  # a newer check already saved this route
        origin, dest, depart, ret, opts = split_key(key)
        before = (prev or {}).get("airlines", {})
        # Log only what changed: new or re-priced airlines, or "no nonstops" when that starts.
        for airline, price in sorted(f["airlines"].items()):
            if before.get(airline) != price:
                rows.append([stamp, origin, dest, depart, ret, price, airline, opts])
        if f["airlines"]:  # an airline that dropped out while others still fly: logged with no price
            for airline in sorted(set(before) - set(f["airlines"])):
                rows.append([stamp, origin, dest, depart, ret, "", airline, opts])
        if not f["airlines"] and (prev is None or before):
            rows.append([stamp, origin, dest, depart, ret, "", "", opts])
        latest[key] = entry(stamp, f, prev)
    if state.get("moves_from_history") != 4:
        moves_from_history(latest, cfg)
        state["moves_from_history"] = 4
    for key, streak in res.get("empty_streaks", {}).items():
        if key in latest and latest[key].get("checked_at", "") <= stamp:
            latest[key]["empty_streak"] = streak  # entry() above starts every new result at 0

    # Forget routes/dates no longer tracked (stopped on the web page, or in the past).
    wanted = {key_of(*r) for r in build_searches(cfg)}
    latest = {k: v for k, v in latest.items() if k in wanted}

    out = io.StringIO()
    old = vault.read_text(HISTORY)
    if old:
        out.write(old if old.endswith("\n") else old + "\n")
    w = csv.writer(out, lineterminator="\n")
    if not old:
        w.writerow(HISTORY_HEADER)
    w.writerows(rows)
    vault.write_text(HISTORY, out.getvalue())
    write_json(LATEST, latest)
    save_alerts(res.get("alerts", []))
    drop_past_dates(cfg)
    # The sign-up relay for shared trip pages' "Email me updates" (friends.py). The page creates it
    # when a trip is first shared; this covers trips shared from an older copy of the page.
    if any((t.get("share") or {}).get("id") for t in cfg.get("trips", [])) and not cfg.get("signup_topic"):
        cfg["signup_topic"] = "trip-signup-" + os.urandom(12).hex()
        vault.write_text(CONFIG, json.dumps(cfg, indent=2) + "\n")
    write_trip_pages(cfg, latest)

    if res.get("price_email_day"):
        state["price_email_day"] = res["price_email_day"]  # later price emails that day join its thread
    if res["failure_alert"]:
        state["last_failure_alert"] = stamp
    if res.get("health_alert"):
        state["last_health_alert"] = stamp
    # A quick "new flights only" run shouldn't replace the last full check's summary.
    if not res["last_run"]["only_new"] or not state.get("last_run"):
        state["last_run"] = res["last_run"]
    write_json(STATE, state)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    only_new = "--new" in sys.argv
    if cmd == "search":
        sys.exit(search(only_new))
    if cmd == "merge":
        sys.exit(merge())
    if cmd == "failed":
        sys.exit(failed())
    search(only_new)
    sys.exit(merge())
