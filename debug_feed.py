"""One-off: find how Google's results feed wants the bags field (run from GitHub, not locally).

The feed request our skiplagged and 1-stop searches use ignored the carry-on setting, so fares
on routes tracked with a carry-on came back without the bag fee. This tries a few shapes and
prints the cheapest one-stop FLL-PHL fare for each; the right one should match Google's page
(Frontier $95 without a carry-on, $140 with).
"""
import json
import urllib.parse

from primp import Client

import google_flights as g

ROUTE = ("2026-12-02", "FLL", "PHL")


def feed(bags_at, value, adults=1):
    depart, origin, dest = ROUTE
    segment = [[[[origin, 0]]], [[[dest, 0]]], None, 2, None, None, depart, None, None, None, None, None, None, None, 3]
    main = ([None, None, 2, None, [], 1, [adults, 0, 0, 0], None, None, None, None, None, None, [segment]]
            + [None] * 3 + [1] + [None] * 10 + [0])
    if bags_at is not None:
        main[bags_at] = value
    body = [[], main, 2, 1, 0, 1]
    data = "f.req=" + urllib.parse.quote(json.dumps([None, json.dumps(body, separators=(",", ":"))],
                                                    separators=(",", ":")))
    client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
    res = client.post(g.FEED_URL + "?hl=en&curr=USD&gl=us", content=data.encode(),
                      headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"})
    text = res.text.lstrip()[4:].lstrip()
    if text[:1].isdigit():
        text = text.split("\n", 1)[1]
    inner = json.loads(json.JSONDecoder().raw_decode(text)[0][0][2])
    out = []
    for section in (2, 3):
        block = inner[section] if len(inner) > section else None
        for item in (block[0] if block and block[0] else []):
            try:
                if len(item[0][2]) == 2:
                    out.append(int(item[1][0][1]))
            except Exception:
                pass
    return sorted(out)[:2]


def through_feed_search(adults, carry, checked=0):
    t = [x for x in g.feed_search(ROUTE[0], [ROUTE[1]], [ROUTE[2]], adults, carry, checked, max_stops=1)
         if len(x["legs"]) == 2]
    return sorted((x["price"], x["airline"]) for x in t)[:2]


TRIES = [("no bags", None, None),
         ("10=[checked,carry] (what we send now)", 10, [0, 1]),
         ("10=[carry,checked]", 10, [1, 0]),
         ("10=[[carry,checked]]", 10, [[1, 0]]),
         ("10=[1,1]", 10, [1, 1]),
         ("10=[0,1,0]", 10, [0, 1, 0]),
         ("10=[1]", 10, [1]),
         ("9=[checked,carry]", 9, [0, 1]),
         ("11=[checked,carry]", 11, [0, 1]),
         ("12=[checked,carry]", 12, [0, 1])]

if __name__ == "__main__":
    for adults in (1, 4):
        for carry in (0, 1):
            print(f"feed_search adults={adults} carry_on={carry}: {through_feed_search(adults, carry)}")
    for label, at, value in TRIES:
        try:
            print(f"{label}: {feed(at, value)}")
        except Exception as e:
            print(f"{label}: ERROR {type(e).__name__}")
