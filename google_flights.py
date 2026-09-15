"""Reads nonstop fares from Google Flights.

One search can cover several departure and arrival airports at once (Google
allows up to 7 on each side), so a whole day of Florida <-> New York routes
takes a handful of searches instead of one per route.

Replaces fast-flights' reader, which skipped Google's "Top flights" list
(where the cheapest fares usually are) and crashed on days with no flights.
"""
import base64
import urllib.parse
import json

from primp import Client
from selectolax.lexbor import LexborHTMLParser

URL = "https://www.google.com/travel/flights"
MAX_AIRPORTS_PER_SIDE = 7


class Blocked(Exception):
    """Google returned something other than a flight results page."""


def _varint(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _field(num, data):  # length-delimited protobuf field
    return _varint(num << 3 | 2) + _varint(len(data)) + data


def _int(num, value):
    return _varint(num << 3) + _varint(value)


def build_tfs(depart, origins, dests, ret="", adults=1, carry_on=0, checked=0, max_stops=0, via=()):
    """Google's `tfs` search parameter: economy, nonstop unless max_stops says otherwise.
    Bags make Google include each airline's bag fees in the fare. `via` limits
    connections to those airports (outbound flight only)."""
    def leg(date, froms, tos, connect=()):
        data = _field(2, date.encode())
        data += b"".join(_field(13, _field(2, a.encode())) for a in froms)
        data += b"".join(_field(14, _field(2, a.encode())) for a in tos)
        data += b"".join(_field(15, a.encode()) for a in connect)
        return _field(3, data + _int(5, max_stops))

    info = leg(depart, origins, dests, via)
    if ret:
        info += leg(ret, dests, origins)
    info += b"".join(_int(8, 1) for _ in range(adults))  # adult passengers
    info += _int(9, 1)  # economy
    if carry_on or checked:
        info += _field(13, (_int(2, carry_on) if carry_on else b"") + (_int(3, checked) if checked else b""))
    info += _int(19, 1 if ret else 2)  # round trip / one-way
    return base64.b64encode(info).decode()


def search(depart, origins, dests, ret="", adults=1, carry_on=0, checked=0, max_stops=0, via=()):
    """Returns a list of itineraries with exactly max_stops + 1 flights:
    {"origin", "dest", "airline", "price", "departs", "final"} where origin, dest and
    departs (HH:MM) describe the first flight and final is where the ticket ends.
    An empty list means Google has no such flights for that search.
    Raises Blocked if Google didn't return a results page."""
    client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
    html = client.get(URL, params={"tfs": build_tfs(depart, origins, dests, ret, adults, carry_on, checked, max_stops, via),
                                   "hl": "en", "curr": "USD"}).text
    return [it for it in parse(html) if it["flights"] == max_stops + 1]


def flight_id(seg):
    """'UA1832' from a flight segment, or '' if Google didn't include it."""
    try:
        return f"{seg[22][0]}{seg[22][1]}"
    except (IndexError, TypeError):
        return ""


def parse(html):
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None or "Google Flights" not in html[:20000]:
        raise Blocked("not a Google Flights results page")
    js = script.text()
    if "data:" not in js:
        raise Blocked("results page had no data")
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []
    payload = json.loads(data)

    out = []
    for section in (2, 3):  # 2 = "Top flights", 3 = "Other flights"
        block = payload[section] if len(payload) > section else None
        for item in (block[0] if block and block[0] else []):
            try:
                flight, price = item[0], item[1][0][1]
                legs = flight[2]
            except (IndexError, TypeError):
                continue
            if price is None or not legs:
                continue
            seg = legs[0]
            hour, minute = ([*(seg[8] or []), None, None])[:2]
            out.append({
                "origin": seg[3],
                "dest": seg[6],
                "airline": ", ".join(flight[1]) if flight[1] else "",
                "price": int(price),
                "departs": f"{hour or 0:02d}:{minute or 0:02d}",
                "final": legs[-1][6],
                "flights": len(legs),
                "flight": flight_id(seg),
            })
    return out


# ---- Google's full results feed -------------------------------------------------
# The results page only includes what Google shows first: when a city has its own
# nonstops, connecting tickets through other airports are left out. The feed the page
# loads as you scroll has every ticket (up to 300, cheapest first) and accepts about
# 10 arrival cities per search. Request shape follows the open-source "fli" project
# (github.com/punitarani/fli, MIT).
FEED_URL = ("https://www.google.com/_/FlightsFrontendUi/data/"
            "travel.frontend.flights.FlightsFrontendService/GetShoppingResults")
FEED_CAP = 300
FEED_MAX_CITIES = 10


def feed_search(depart, origins, dests, adults=1, carry_on=0, checked=0, max_stops=2, via=()):
    """One-way tickets with up to max_stops connections, cheapest first:
    [{"price", "airline", "legs": [(from, to, "HH:MM", flight), ...]}].
    Raises Blocked if Google sends back nothing usable."""
    stops = {0: 1, 1: 2, 2: 3}[max_stops]
    segment = [[[[a, 0] for a in origins]], [[[a, 0] for a in dests]], None, stops, None, None,
               depart, None, None, list(via) or None, None, None, None, None, 3]
    main = ([None, None, 2, None, [], 1, [adults, 0, 0, 0], None, None, None,
             [checked, carry_on] if carry_on or checked else None, None, None, [segment]]
            + [None] * 3 + [1] + [None] * 10 + [0])
    body = [[], main, 2, 1, 0, 1]  # 2 = cheapest first, 1 = all results
    data = "f.req=" + urllib.parse.quote(json.dumps([None, json.dumps(body, separators=(",", ":"))],
                                                      separators=(",", ":")))
    client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
    res = client.post(FEED_URL + "?hl=en&curr=USD&gl=us", content=data.encode(),
                      headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"})
    text = res.text.lstrip()
    if not text.startswith(")]}'"):
        raise Blocked(f"feed returned status {res.status_code}")
    text = text[4:].lstrip()
    if text[:1].isdigit():  # length-prefixed chunk
        text = text.split("\n", 1)[1]
    try:
        outer = json.JSONDecoder().raw_decode(text)[0]
        inner = json.loads(outer[0][2]) if outer[0][2] else None
    except (ValueError, IndexError, TypeError):
        inner = None
    if inner is None:
        raise Blocked("feed returned no data")
    out = []
    for section in (2, 3):
        block = inner[section] if len(inner) > section else None
        for item in (block[0] if isinstance(block, list) and block and block[0] else []):
            try:
                legs = [(l[3], l[6], "%02d:%02d" % tuple(([*(l[8] or []), 0, 0])[:2]), flight_id(l)) for l in item[0][2]]
                price = item[1][0][1]
            except (IndexError, TypeError):
                continue
            if price is not None and legs:
                out.append({"price": int(price), "airline": ", ".join(item[0][1] or []), "legs": legs})
    return out
