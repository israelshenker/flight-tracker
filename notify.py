"""Send alerts by Gmail and ntfy push. Credentials come from environment variables only."""
import os
import smtplib
import time
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid


def send_email(subject, body, thread=None, first=False, to=None):
    """thread: emails with the same thread id and subject land in one Gmail conversation.
    The first email of a thread carries the thread's Message-ID; later ones reply to it."""
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not password:
        print("  (email skipped: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set)")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to or os.environ.get("ALERT_EMAIL_TO") or sender  # to: a friend on a shared trip (friends.py)
    if thread and first:
        msg["Message-ID"] = f"<{thread}@flight-tracker>"
    elif thread:
        msg["Message-ID"] = make_msgid(domain="flight-tracker")
        msg["In-Reply-To"] = msg["References"] = f"<{thread}@flight-tracker>"
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def send_push(title, body, click=None, buttons=None):
    """buttons: [(label, url)] shown on the notification (ntfy allows 3); replaces "Open alert"."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("  (push skipped: NTFY_TOPIC not set)")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        # Pushes skip the per-route links; ntfy caps messages at 4 KB. Tapping the notification
        # opens it in the ntfy app; the "Open alert" button in it opens the alert on the page.
        data="\n".join(l for l in body.splitlines() if not l.startswith(("    http", "    Book "))).encode("utf-8")[:4000],
        headers={"Title": title.encode("ascii", "replace").decode(), "Tags": "airplane",
                 **({"Actions": "; ".join(f"view, {label}, {url}" for label, url in buttons[:3])} if buttons
                    else {"Actions": f"view, Open alert, {click}"} if click else {})},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30).close()


# The push goes out first and the email this many seconds later, so the phone's email sound
# doesn't cut off the push's notification sound (user's request).
EMAIL_AFTER_PUSH_SECONDS = 15


def send(title, body, click=None, thread=None, push_body=None):
    """click: page the push notification's "Open alert" button opens.
    thread: (id, subject, first) to put the email in a shared conversation; the push keeps
    `title` and the email body starts with it. push_body: different text for the push
    (price alerts put one word such as UP or DOWN in front of each line); defaults to body."""
    for channel in (send_push, send_email):
        try:
            if channel is send_push:
                send_push(title, push_body or body, click)
                continue
            if os.environ.get("NTFY_TOPIC"):
                time.sleep(EMAIL_AFTER_PUSH_SECONDS)
            if thread:
                send_email(thread[1], f"{title}\n\n{body}", thread[0], thread[2])
            else:
                send_email(title, body)
        except Exception as e:  # one channel failing shouldn't stop the other
            print(f"  {channel.__name__} failed: {e}")


if __name__ == "__main__":
    send("Flight tracker test", "If you can read this, alerts are working.\n\n"
         "All fares: https://israelshenker.github.io/flight-tracker/",
         click="https://israelshenker.github.io/flight-tracker/")
    print("Test alert sent.")
