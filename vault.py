"""Passphrase encryption for the data files, so the public repo and page show nothing
readable without the passphrase.

The passphrase lives only in the DATA_KEY GitHub secret and in the browsers it's typed
into. Files keep their names; an encrypted file holds
  {"encrypted": "v1", "iv": ..., "data": ...}
AES-256-GCM, key from PBKDF2-SHA256 over the passphrase with the salt in data/vault.json.
docs/index.html does the same with the browser's Web Crypto; keep the two in sync.

Without DATA_KEY set, files are read and written as plain text (local testing).

  python vault.py encrypt    encrypt config.json and the data files in place (needs DATA_KEY)
"""
import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

HERE = Path(__file__).parent
VAULT = HERE / "data" / "vault.json"  # public: salt + a check value, no secrets
ITERATIONS = 310_000
_key = None


def enabled():
    return bool(os.environ.get("DATA_KEY"))


def _b64(b):
    return base64.b64encode(b).decode()


def key():
    """The AES key, derived once per run."""
    global _key
    if _key is None:
        passphrase = os.environ.get("DATA_KEY", "")
        if not passphrase:
            raise RuntimeError("DATA_KEY is not set")
        if VAULT.exists():
            info = json.loads(VAULT.read_text(encoding="utf-8"))
            salt = base64.b64decode(info["salt"])
        else:
            salt = os.urandom(16)
            info = None
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
        _key = kdf.derive(passphrase.encode("utf-8"))
        if info is None:
            VAULT.parent.mkdir(exist_ok=True)
            VAULT.write_text(json.dumps({"salt": _b64(salt), "iterations": ITERATIONS,
                                         "check": _seal("flight-tracker")}, indent=2), encoding="utf-8")
        else:
            try:
                ok = _open(info["check"]) == "flight-tracker"
            except Exception:
                ok = False
            if not ok:
                _key = None
                raise RuntimeError("DATA_KEY doesn't match the passphrase these files were encrypted with")
    return _key


def _seal(text):
    iv = os.urandom(12)
    return {"encrypted": "v1", "iv": _b64(iv), "data": _b64(AESGCM(key()).encrypt(iv, text.encode("utf-8"), None))}


def _open(box):
    return AESGCM(key()).decrypt(base64.b64decode(box["iv"]), base64.b64decode(box["data"]), None).decode("utf-8")


def is_sealed(text):
    return text.lstrip().startswith('{"encrypted"') or '"encrypted": "v1"' in text[:40]


def read_text(path):
    """File contents as plain text (decrypted if needed), or None if missing."""
    path = Path(path)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    return _open(json.loads(text)) if is_sealed(text) else text


def write_text(path, text):
    """Write text, encrypted when DATA_KEY is set."""
    path = Path(path)
    path.write_text(json.dumps(_seal(text)) if enabled() else text, encoding="utf-8")


def encrypt_all():
    if not enabled():
        sys.exit("Set DATA_KEY first.")
    for rel in ("config.json", "data/latest.json", "data/state.json", "data/history.csv"):
        path = HERE / rel
        text = read_text(path)
        if text is not None:
            write_text(path, text)
            print(f"encrypted {rel}")


if __name__ == "__main__":
    if sys.argv[1:] == ["encrypt"]:
        encrypt_all()
    else:
        print(__doc__)
