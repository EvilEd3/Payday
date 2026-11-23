"""Web Push: send a notification to a browser or installed app (RFC 8030, 8291, 8292).

Payloads are encrypted for the one device that subscribed (aes128gcm), and each request is signed with this
site's VAPID key so the push service knows it came from Payday. Uses only `cryptography` and `httpx`.
"""
import base64
import json
import os
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import vault
from sqlmodel import Session

from .db import set_site_setting, site_engine, site_setting

PURPOSE = "vapid-key"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _raw_public(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _private_key() -> ec.EllipticCurvePrivateKey:
    """This site's VAPID key, made the first time it's needed and kept encrypted in the site database."""
    sealed = site_setting("vapid_private")
    pem = vault.unseal(PURPOSE, sealed) if sealed else ""
    if pem:
        return serialization.load_pem_private_key(pem.encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    with Session(site_engine) as s:
        set_site_setting(s, "vapid_private", vault.seal(PURPOSE, pem))
    return key


def public_key() -> str:
    """The key a browser needs to subscribe (applicationServerKey)."""
    return b64u(_raw_public(_private_key()))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """RFC 8291 aes128gcm body for one subscriber, as a single record."""
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(server_key)
    shared = server_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    record_size = 4096
    return salt + record_size.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + body


def _subject() -> str:
    """Who to contact about this sender. Apple refuses pushes without a real-looking subject."""
    public = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if public.startswith("https://"):
        return public
    contact = (site_setting("contact_email") or "").strip()
    return f"mailto:{contact}" if "@" in contact else "mailto:notifications@payday.invalid"


def vapid_header(endpoint: str, key: ec.EllipticCurvePrivateKey | None = None, now: float | None = None) -> str:
    key = key or _private_key()
    url = urlparse(endpoint)
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{url.scheme}://{url.netloc}", "exp": int((now or time.time()) + 12 * 3600),
                              "sub": _subject()}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{claims}.{signature}, k={b64u(_raw_public(key))}"


class Gone(Exception):
    """The device unsubscribed or the app was removed: forget this subscription."""


def send(endpoint: str, p256dh: str, auth: str, message: dict, ttl: int = 24 * 3600,
         client: httpx.Client | None = None) -> None:
    """Send one notification. Raises Gone when the subscription no longer exists, httpx errors otherwise."""
    if urlparse(endpoint).scheme != "https":
        raise Gone("not an https push endpoint")
    body = encrypt(json.dumps(message, separators=(",", ":")).encode(), p256dh, auth)
    headers = {"Authorization": vapid_header(endpoint), "Content-Encoding": "aes128gcm",
               "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": "normal"}
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        r = client.post(endpoint, content=body, headers=headers)
    finally:
        if own:
            client.close()
    if r.status_code in (404, 410):
        raise Gone(f"push service says {r.status_code}")
    r.raise_for_status()
