"""Send alerts by Gmail and ntfy push. Credentials come from environment variables only."""
import os
import smtplib
import urllib.request
from email.message import EmailMessage


def send_email(subject, body):
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not password:
        print("  (email skipped: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set)")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = os.environ.get("ALERT_EMAIL_TO") or sender
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def send_push(title, body, click=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("  (push skipped: NTFY_TOPIC not set)")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        # Pushes skip the per-route links; ntfy caps messages at 4 KB. Tapping the notification
        # opens it in the ntfy app; the "Open alert" button in it opens the alert on the page.
        data="\n".join(l for l in body.splitlines() if not l.startswith("    http")).encode("utf-8")[:4000],
        headers={"Title": title.encode("ascii", "replace").decode(), "Tags": "airplane",
                 **({"Actions": f"view, Open alert, {click}"} if click else {})},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30).close()


def send(title, body, click=None):
    """click: page the push notification's "Open alert" button opens."""
    for channel in (send_email, send_push):
        try:
            channel(title, body, click) if channel is send_push else channel(title, body)
        except Exception as e:  # one channel failing shouldn't stop the other
            print(f"  {channel.__name__} failed: {e}")


if __name__ == "__main__":
    send("Flight tracker test", "If you can read this, alerts are working.\n\n"
         "All fares: https://israelshenker.github.io/flight-tracker/",
         click="https://israelshenker.github.io/flight-tracker/")
    print("Test alert sent.")
