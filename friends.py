"""Email updates for friends and family on shared trips.

Friends sign up on the trip page (docs/trip.html): the page locks {email, schedule} with the
trip's key and posts it to the ntfy topic in config.json's `signup_topic` (ntfy.sh keeps
messages 12 hours). Each hourly check reads new messages, keeps the ones that open with a
shared trip's key (so only people with the trip link can sign up), and emails whoever is due:
  changes  after a check where any of the trip's fares changed since their last email
  daily    once a day, at their chosen hour (their own time zone)
  weekly   once a week, on their chosen day and hour
The first email after signing up has the current fares. Every email has a link to stop them.

Run on GitHub after the price check is saved, in two steps like tracker.py:
  python friends.py send    read sign-ups, send due emails, write friends_results.json
  python friends.py merge   save the sign-ups and "last sent" record to data/subscribers.json
"""
import base64
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import notify
import tracker

SUBSCRIBERS = tracker.DATA / "subscribers.json"   # encrypted like the other data files
RESULTS = tracker.HERE / "friends_results.json"   # stays on the runner
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SCHEDULES = {"changes", "daily", "weekly"}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def shared_trips(cfg):
    return [t for t in cfg.get("trips", []) if (t.get("share") or {}).get("id") and t["share"].get("key")]


def read_signups(topic, since):
    """New messages on the sign-up topic: (messages, newest message id)."""
    if not topic:
        return [], since
    url = f"https://ntfy.sh/{topic}/json?poll=1&since={since or '13h'}"
    with urllib.request.urlopen(url, timeout=30) as res:
        lines = res.read().decode("utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    msgs = [e for e in events if e.get("event") == "message"]
    return msgs, (msgs[-1]["id"] if msgs else since)


def open_signup(message, trips):
    """(trip, request) if the message opens with a shared trip's key, else None."""
    try:
        box = json.loads(message)
        trip = next(t for t in trips if t["share"]["id"] == box["t"])
        data = AESGCM(tracker.trip_key(trip)).decrypt(base64.b64decode(box["iv"]), base64.b64decode(box["data"]), None)
        return trip, json.loads(data)
    except Exception:
        return None


def trip_link(t, stop_id=None):
    return f"{tracker.PAGE_URL}trip.html#{t['share']['id']}.{t['share']['key']}" + (f".stop.{stop_id}" if stop_id else "")


def due(sub, prices, now):
    """Why this subscriber gets an email now ("welcome", "changes", "daily", "weekly"), or None."""
    if not sub.get("last_sent"):
        return "welcome"
    if sub["schedule"] == "changes":
        return "changes" if prices != sub.get("last_prices") else None
    local = now.astimezone(ZoneInfo(sub.get("tz") or "America/New_York"))
    last = datetime.fromisoformat(sub["last_sent"]).astimezone(local.tzinfo)
    if local.hour < int(sub.get("hour", 8)) or last.date() == local.date():
        return None
    if sub["schedule"] == "weekly" and local.weekday() != int(sub.get("day", 0)):
        return None
    return sub["schedule"]


def email_body(t, legs, sub, reason):
    last = sub.get("last_prices") or {}
    lines = []
    intro = {"welcome": "You're signed up for fare updates on this trip. Current fares:",
             "changes": "Fares changed on this trip:",
             "daily": "Your daily fare update:", "weekly": "Your weekly fare update:"}[reason]
    lines.append(intro)
    for label, part in (("Going", [x for x in legs if x["going"]]), ("Coming back", [x for x in legs if not x["going"]])):
        if not part:
            continue
        if any(not x["going"] for x in legs):
            lines += ["", label.upper()]
        for x in sorted(part, key=lambda x: (x["dep"], x["price"] if x["price"] is not None else 1e9)):
            date = tracker.nice_date(x["dep"]) + (f", back {tracker.nice_date(x['ret'])}" if x["ret"] else "")
            route = f"{x['o']}-{x['d']}" + (f" ({x['opts']})" if x["opts"] else "")
            if x["price"] is None:
                what = "no nonstop flights right now" if not x.get("last_price") else f"no longer available (was ${x['last_price']})"
                lines.append(f"{date} · {route}: {what}")
                continue
            was = last.get(x["key"])
            change = "" if reason == "welcome" or was is None or was == x["price"] else \
                f" · {'UP' if x['price'] > was else 'DOWN'} ${abs(x['price'] - was)} since your last email (was ${was})"
            when = f" at {', '.join(tracker.time_label(h) for h in x['times'])}" if x.get("times") else ""
            lines.append(f"{date} · {route}: ${x['price']}{change} · {x['airline']}{when}")
    lines += ["", "Prices are per person, nonstop, checked about every hour.",
              f"See the trip, price history and Google Flights links: {trip_link(t)}", "",
              f"Stop these emails: {trip_link(t, sub['id'])}"]
    return "\n".join(lines)


def send():
    cfg = tracker.read_json(tracker.CONFIG, {})
    latest = tracker.read_json(tracker.LATEST, {})
    book = tracker.read_json(SUBSCRIBERS, {"subs": {}})
    subs = book.setdefault("subs", {})
    trips = shared_trips(cfg)
    by_id = {t["id"]: t for t in cfg.get("trips", [])}
    now = datetime.now(timezone.utc)

    try:
        msgs, book["since"] = read_signups(cfg.get("signup_topic"), book.get("since"))
    except Exception as e:
        print(f"  couldn't read sign-ups: {type(e).__name__}")
        msgs = []
    added = stopped = 0
    for m in msgs:
        opened = open_signup(m.get("message", ""), trips)
        if not opened:
            continue
        t, req = opened
        sid = re.sub(r"[^a-z0-9]", "", str(req.get("id", "")).lower())[:32]
        if not sid:
            continue
        if req.get("a") == "stop":
            stopped += subs.pop(sid, None) is not None
        elif req.get("a") == "sub" and EMAIL_RE.match(str(req.get("email", ""))) and req.get("schedule") in SCHEDULES:
            subs[sid] = {"id": sid, "trip": t["id"], "email": req["email"].strip()[:200], "schedule": req["schedule"],
                         "hour": max(0, min(23, int(req.get("hour", 8)))), "day": max(0, min(6, int(req.get("day", 0)))),
                         "tz": str(req.get("tz") or "America/New_York")[:60], "since": now.isoformat(timespec="seconds")}
            try:
                ZoneInfo(subs[sid]["tz"])
            except Exception:
                subs[sid]["tz"] = "America/New_York"
            added += 1

    # Removed on the owner's page, or the trip is gone / no longer shared.
    for sid, sub in list(subs.items()):
        t = by_id.get(sub["trip"])
        if not t or sid in (t.get("remove_subs") or []):
            subs.pop(sid)

    trails = tracker.log_trails(tracker.read_log())
    sent = 0
    shared = {t["id"]: t for t in trips}
    for sub in subs.values():
        t = shared.get(sub["trip"])
        if not t:
            continue  # sharing is off: no emails, but keep the sign-up in case it's turned back on
        legs = tracker.trip_legs(cfg, latest, t, trails)
        prices = {x["key"]: x["price"] for x in legs}
        reason = due(sub, prices, now)
        if not reason:
            continue
        try:
            notify.send_email(f"{t.get('name') or 'Trip'}: fare update {tracker.nice_date(now.astimezone(tracker.LOCAL_TZ).date().isoformat())}",
                              email_body(t, legs, sub, reason), to=sub["email"])
        except Exception as e:
            print(f"  a friend email failed: {type(e).__name__}")
            continue
        sub["last_sent"], sub["last_prices"] = now.isoformat(timespec="seconds"), prices
        sent += 1
    RESULTS.write_text(json.dumps(book), encoding="utf-8")
    print(f"  friend emails: {added} new sign-ups, {stopped} stopped, {sent} sent, {len(subs)} signed up")
    return 0


def merge():
    """Only this job writes subscribers.json (and it never runs twice at once), so the
    runner's copy simply replaces the saved one."""
    if not RESULTS.exists():
        return 0
    tracker.write_json(SUBSCRIBERS, json.loads(RESULTS.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(send() if cmd == "send" else merge() if cmd == "merge" else print(__doc__) or 1)
