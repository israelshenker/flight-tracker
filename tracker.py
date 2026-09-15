"""Flight price check.

Reads the tracked route/dates from config.json, looks up every airline's lowest
nonstop fare on each, sends alerts on changes, and saves the results.

Run on GitHub in two steps so overlapping checks can't overwrite each other:
  python tracker.py search [--new]   search Google, send alerts, write run_results.json
  python tracker.py merge            add run_results.json to the newest data files
Locally, `python tracker.py` does both.
"""
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

import google_flights
import notify
import vault

HERE = Path(__file__).parent
CONFIG = HERE / "config.json"
DATA = HERE / "data"
LATEST = DATA / "latest.json"    # current fares: key -> {checked_at, price, airline, airlines, times}
HISTORY = DATA / "history.csv"   # change log: a row whenever an airline's fare on a route changes
STATE = DATA / "state.json"
RESULTS = HERE / "run_results.json"
PAGE_URL = "https://israelshenker.github.io/flight-tracker/"

FAILURE_ALERT_EVERY = timedelta(hours=12)
HEALTH_STALE = timedelta(hours=3)          # no full check this long = checks were missed
HEALTH_ALERT_EVERY = timedelta(hours=6)
DEAD_ROUTE_EVERY = timedelta(hours=23)     # routes with no nonstops are checked daily
TIME_BUDGET_SECONDS = 20 * 60  # the GitHub job is killed at 30 min and would save nothing
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
SKIP_EVERY = {1: timedelta(minutes=50), 2: timedelta(hours=3) - timedelta(minutes=10)}
SKIP_MAX_SEARCHES = 60  # per run; anything left goes first next run
SKIP_SPLIT_UP_TO = 200  # dollars; see hidden_search
NORTH = {"JFK", "LGA", "EWR", "HPN", "ACY", "PHL", "TTN", "SWF", "ISP", "BOS"}
BEYOND_NORTH = ["BOS", "BUF", "ROC", "SYR", "ALB", "BTV", "PWM", "BDL", "PVD", "MHT",
                "PIT", "CLE", "DTW", "ORD", "MDW", "CMH", "CVG", "IND", "GRR", "MKE",
                "MSP", "STL", "MCI", "DEN", "RDU", "CLT", "GSO", "RIC", "ORF", "DCA",
                "IAD", "BWI", "BNA", "SDF", "AUS", "DFW", "IAH", "PHX", "LAS", "LAX",
                "SFO", "SEA", "SAN", "SLC"]
BEYOND_SOUTH = ["TPA", "JAX", "RSW", "SRQ", "EYW", "MCO", "PNS", "TLH", "SAV", "CHS",
                "MSY", "IAH", "HOU", "DFW", "AUS", "ATL", "BNA", "SJU", "STT", "STX"]
INTL_NORTH = ["YYZ", "YUL", "YOW", "YHZ", "YQB"]
INTL_SOUTH = ["NAS", "CUN", "MBJ", "SDQ", "PUJ", "GCM", "AUA", "CUR", "SXM", "BGI",
              "PTY", "SJO", "BOG", "MDE", "GUA", "SAL", "LIM", "HAV", "PLS", "UVF"]
INTERNATIONAL = set(INTL_NORTH + INTL_SOUTH)
# US territories fly as domestic (no passport), so they are never "international" here.
US_TERRITORIES = {"SJU", "BQN", "PSE", "STT", "STX", "GUM", "SPN"}


def is_international(code):
    """Airports needing a passport from the US: the lists above, plus Canadian codes (Y..)."""
    return code not in US_TERRITORIES and (code in INTERNATIONAL or (len(code) == 3 and code.startswith("Y")))
# Second connections allowed on two-stop tickets (plus the tracked destinations).
SECOND_STOPS = ["CLE", "ORD", "IAD", "DTW", "CLT", "BOS", "PIT", "BUF", "PHL", "ATL",
                "DCA", "BWI", "MIA", "MCO", "TPA", "IAH", "DFW", "DEN"]
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
# "t6-12" leaving 6 AM to noon, "fUA1832" one specific flight (always last; upper case).
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
    if w.get("flight"):
        code += "f" + re.sub(r"[^A-Z0-9]", "", str(w["flight"]).upper())
    return code


def parse_options(code):
    code, _, flight = code.partition("f")
    c = re.search(r"c(\d)", code)
    b = re.search(r"b(\d)", code)
    t = re.search(r"t(\d+)-(\d+)", code)
    return {"carry_on": int(c.group(1)) if c else 0, "checked": int(b.group(1)) if b else 0,
            "time_from": int(t.group(1)) if t else 0, "time_to": int(t.group(2)) if t else 24,
            "flight": flight}


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
        g = (dep, ret, opt["carry_on"], opt["checked"])
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
    o, d, dep, ret, opts = split_key(key)
    text = f"{o}-{d} {nice_date(dep)}" + (f", return {nice_date(ret)}" if ret else "")
    label = options_label(opts)
    return text + (f" ({label})" if label else "")


# ---- searching ------------------------------------------------------------

class EmptyResults(Exception):
    """Google returned no flights where there clearly should be some."""


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
                groups[(dep, opt["carry_on"], d in NORTH, intl_ok, stops)].append((o, d, dep, ret, opts))
    budget = {"left": SKIP_MAX_SEARCHES}

    def age(g):
        return min((latest.get(key_of(*r)) or {}).get(f"hidden{g[0][4]}_checked_at", "") for r in g[1])

    for (dep, carry, north, intl, stops), routes in sorted(groups.items(), key=lambda g: (g[0][4], age(g))):
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
                    tickets, whole = feed_tickets(dep, os_, fs, via, adults, carry, stops, need, budget)
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
        time.sleep(3 + random.random() * 4)  # be gentle with Google
        try:
            tickets = google_flights.feed_search(dep, origins, finals, adults, carry, 0, stops, via)
            break
        except google_flights.Blocked as e:
            if attempt == 2:
                raise
            log(f"  retrying skiplagged search after: {e}", "  retrying a skiplagged search")
            time.sleep(10 + random.random() * 10)
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
    last_full = (state.get("last_run") or {}).get("at")
    if not only_new and last_full and now - datetime.fromisoformat(last_full) > HEALTH_STALE:
        last_alert = state.get("last_health_alert")
        if not last_alert or now - datetime.fromisoformat(last_alert) >= HEALTH_ALERT_EVERY:
            hours = (now - datetime.fromisoformat(last_full)).total_seconds() / 3600
            notify.send("Flight tracker: checks were missed",
                        f"The last successful full check before this one was {hours:.0f} hours ago. "
                        f"Checks are running again now.\n\nAll fares: {PAGE_URL}", click=PAGE_URL)
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
            back_opts = options_code({"carry_on": opt["carry_on"], "checked": opt["checked"]})
            extras.add((o, d, dep, "", opts))
            extras.add((d, o, ret, "", back_opts))
    extras -= set(routes)
    searches = plan(sorted(set(todo) | extras))
    # Longest-unchecked first, so anything cut off by the time budget goes first next run.
    searches.sort(key=lambda s: min((latest.get(key_of(*r)) or {}).get("checked_at", "") for r in s[6]))
    print(f"{len(todo)} of {len(routes)} route/dates in {len(searches)} searches"
          f"{f' (plus {len(extras)} one-way legs for round trips)' if extras else ''}"
          f"{f'; {len(routes) - len(todo)} with no nonstops wait for their daily check' if not only_new and len(todo) < len(routes) else ''}")

    started = time.monotonic()
    fares, errors, done = {}, [], 0
    for i, (dep, ret, carry, checked, origins, dests, covered) in enumerate(searches):
        if time.monotonic() - started > TIME_BUDGET_SECONDS:
            print(f"  Time budget reached; {len(searches) - i} searches left for next run.")
            break
        if i:
            time.sleep(3 + random.random() * 4)  # be gentle with Google
        bags = f" carry-on {carry} checked {checked}" if carry or checked else ""
        label = f"{','.join(origins)} -> {','.join(dests)} {dep}{' / ' + ret if ret else ''}{bags}"
        try:
            had_fares = any((latest.get(key_of(*r)) or {}).get("price") for r in covered)
            itineraries = run_search(dep, ret, origins, dests, adults, carry, checked,
                                     had_fares or len(covered) >= 10)
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
        back = leg_of(key_of(d, o, ret, "", options_code({"carry_on": opt["carry_on"], "checked": opt["checked"]})))
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
    lines, counts = [], defaultdict(int)

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
                was = f"${prev} -> " if prev else ""
                lines.append(f"{', '.join(tags)}: {describe(key)}: {was}${new}, {detail}{link}")
        ow = f.get("one_ways")
        if ow and new and ow["total"] < new:
            before_ow = (before or {}).get("one_ways") or {}
            if not before_ow or is_flagged(before_ow.get("total", 0), ow["total"], cfg):
                lines.append(f"TWO ONE-WAYS ${ow['total']}, ${new - ow['total']} under the round trip: {describe(key)}: "
                             f"{ow['out']['airline']} out at {time_label(ow['out']['departs'])} + "
                             f"{ow['back']['airline']} back at {time_label(ow['back']['departs'])}{link}")
                counts["two one-ways"] += 1
        h = combined_hidden(f, before)
        if h and h.get("international") and not is_international(split_key(key)[1]) and not cfg.get("skiplagged_intl_domestic"):
            h = None  # international ending on a domestic trip, with that option off
        if h and key not in no_skip and (not new or h["price"] < new):
            prior = (before or {}).get("hidden") or {}
            if not prior.get("price") or is_flagged(prior["price"], h["price"], cfg):
                vs = f", ${new - h['price']} under the nonstop" if new else ""
                intl = " (international ticket: passport needed)" if h.get("international") else ""
                lines.append(f"SKIPLAGGED ${h['price']}{vs}: {describe(key)}: {h['airline']} at {time_label(h['departs'])}, "
                             f"ticket to {h['final']}, get off at {split_key(key)[1]}{intl}{link}")
                counts["skiplagged"] += 1
    if lines:
        first_key = next((k for k in fares if route_link(k) in lines[0]), None)
        notify.send("Flight prices: " + "; ".join(f"{n} {what}" for what, n in counts.items()),
                    "\n".join(lines) + f"\n\nAll fares: {PAGE_URL}",
                    click=route_link(first_key) if len(lines) == 1 and first_key else PAGE_URL)

    # If most searches failed, Google is probably blocking us. Say so, but not every hour.
    attempted = done + len(errors)
    failure_alert = False
    if attempted and len(errors) >= max(1, attempted / 2):
        last = state.get("last_failure_alert")
        if not last or now - datetime.fromisoformat(last) >= FAILURE_ALERT_EVERY:
            notify.send(
                "Flight tracker: searches failing",
                f"{len(errors)} of {attempted} Google searches failed. Google may be blocking "
                "the free reader.\n\n" + "\n".join(errors[:20]),
            )
            failure_alert = True

    write_json(RESULTS, {
        "stamp": stamp, "fares": fares, "failure_alert": failure_alert, "health_alert": health_alert,
        "last_run": {"at": stamp, "searched": len(fares), "total": len(routes), "searches": attempted,
                     "errors": errors[:20], "only_new": only_new},
    })
    print(f"{len(lines)} alert lines ({dict(counts)}), {len(errors)} errors")
    return 0


def dead_recently(entry_, now):
    """A route with no nonstop flights that was looked at within the last day."""
    if not entry_ or entry_.get("price") is not None or not entry_.get("checked_at"):
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
    notify.send("Flight tracker: a check failed",
                f"The hourly price check crashed, so fares weren't updated this time. It will try again "
                f"next hour; you'll hear again only if it's still failing in {HEALTH_ALERT_EVERY.seconds // 3600} hours."
                f"\n\nDetails: {run}", click=run)
    state["last_health_alert"] = now.isoformat(timespec="seconds")
    write_json(STATE, state)
    return 0


def entry(stamp, f, prev=None):
    airlines = f["airlines"]
    cheapest = min(airlines, key=airlines.get) if airlines else ""
    e = {"checked_at": stamp, "airlines": airlines, "times": f.get("times", {}),
         "flights": f.get("flights", []), "price": airlines.get(cheapest), "airline": cheapest,
         "prev_price": (prev or {}).get("price")}  # the page colors the fare by this change
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
        if not f["airlines"] and (prev is None or before):
            rows.append([stamp, origin, dest, depart, ret, "", "", opts])
        latest[key] = entry(stamp, f, prev)

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
    drop_past_dates(cfg)

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
