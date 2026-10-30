"""Mock ID Austria - a SIMULATION, not real ID Austria; it never claims an eIDAS level of assurance.
Models what matters about the real IdP for this flow:

* An identity is a person with a credential: every persona has a 6-digit PIN (= confirming in the ID Austria
  app). Nobody can claim someone else's identity: a typed name that would pass as a PROTECTED person (any
  persona, any person in the mock registers, the named recipient of any open request) is refused unless that
  persona's PIN is given. Free entry is allowed only for genuinely new, unprotected identities.
* The IdP signs an ID token with ES256 (P-256); the public key is published as a JWKS
  (/eid/.well-known/jwks.json), so the token stored in Provenance.signature is verifiable by anyone.
* Data minimisation in the token: sub = salted pairwise bPK hash (HMAC-SHA256(PGK_BPK_SALT, bPK)), given and
  family name (needed for the named-person check), acr = the eid-simulated policy URI (never an eIDAS LoA),
  aud = SHA-256 of the request's link token, iat, exp,
  nonce/jti, simulated=true. No clear bPK, no birth date.
Swapping in the real ID Austria = replace this module with the OIDC redirect and verify the real IdP's token.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

import fhirmap
from fhirmap.identity import rule_tokens

from .keys import ES256Key, b64u, sign_jws, unb64u

ACR_SIMULATED = fhirmap.POLICY_EID_SIMULATED  # https://pgk-bridge.example/fhir/policy/eid-simulated
_BPK_KEY = b"pgk-bridge-mock-bpk-sector-GH"  # mock bPK derivation (stable per identity); the bPK never leaves the IdP
PIN_RE = re.compile(r"\d{6}")


def bpk(given: str, family: str, birthdate: str) -> str:
    tok = lambda x: " ".join(rule_tokens(x))
    ident = "|".join([tok(given), tok(family), birthdate.strip()])
    mac = hmac.new(_BPK_KEY, ident.encode(), hashlib.sha256).digest()
    return "GH:" + b64u(mac[:20])


def pairwise_sub(bpk_value: str, salt: str) -> str:
    """Salted pairwise subject: what the service and FHIR see instead of the bPK."""
    return "bpk-hmac:" + hmac.new(salt.encode(), bpk_value.encode(), hashlib.sha256).hexdigest()[:32]


class IdPError(Exception):
    pass


def _same_name(a_given: str, a_family: str, b_given: str, b_family: str) -> bool:
    return rule_tokens(a_given) == rule_tokens(b_given) and rule_tokens(a_family) == rule_tokens(b_family)


class MockIdP:
    def __init__(self, store, key: ES256Key, pin_key: bytes, bpk_salt: str, issuer: str, personas_file: Path,
                 protected_extra=lambda: [], ttl_s: int = 300):
        self.store, self.key, self.pin_key, self.salt, self.iss, self.ttl = store, key, pin_key, bpk_salt, issuer, ttl_s
        self.protected_extra = protected_extra
        self._fails: dict[str, list[float]] = {}
        self._userinfo: dict[str, tuple[float, str]] = {}  # jti -> (exp, birthdate): server-side only, one use
        self._lock = threading.Lock()
        store.exec("CREATE TABLE IF NOT EXISTS personas (key TEXT PRIMARY KEY, given TEXT NOT NULL, family TEXT NOT NULL, "
                   "birthdate TEXT NOT NULL, pin_hash TEXT NOT NULL, origin TEXT NOT NULL, created TEXT NOT NULL)")
        cols = {r["name"] for r in store.q("PRAGMA table_info(personas)")}
        if "demo_pin" not in cols:  # migration: the PIN of a DEMO-enrolled persona, to show Dr. Hofer again
            store.exec("ALTER TABLE personas ADD COLUMN demo_pin TEXT")
        for p in json.loads(personas_file.read_text(encoding="utf-8"))["personas"]:
            store.exec("INSERT OR REPLACE INTO personas (key, given, family, birthdate, pin_hash, origin, created) "
                       "VALUES (?,?,?,?,?,'seed',datetime('now'))",
                       (p["key"], p["given"], p["family"], p["birthdate"], self._pin_hash(p["pin"])))

    # ------------------------------------------------------------ personas
    def _pin_hash(self, pin: str) -> str:
        return hmac.new(self.pin_key, pin.encode(), hashlib.sha256).hexdigest()

    def personas(self) -> list[dict]:
        return self.store.q("SELECT key, given, family, birthdate, origin FROM personas ORDER BY origin DESC, key")

    def demo_persona(self, name: str) -> dict | None:
        """The DEMO-enrolled persona for the person `name` (with its PIN), if there is one."""
        for p in self.store.q("SELECT key, given, family, demo_pin FROM personas WHERE origin='demo' ORDER BY created"):
            if p["demo_pin"] and fhirmap.identity_matches(name, p["given"], p["family"]):
                return p
        return None

    def has_persona(self, name: str) -> bool:
        """Is there a persona that would sign in as the person `name` (same rule as the named-person check)?"""
        return any(fhirmap.identity_matches(name, p["given"], p["family"]) for p in self.personas())

    def add_persona(self, given: str, family: str, birthdate: str, pin: str | None = None, origin: str = "operator") -> dict:
        """Operator path (API key / CLI) and demo enrolment: enrol a new test person; returns the PIN once."""
        if not given.strip() or not family.strip() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birthdate or ""):
            raise IdPError("given, family and birthdate (YYYY-MM-DD) are required")
        pin = pin or f"{secrets.randbelow(10**6):06d}"
        if not PIN_RE.fullmatch(pin):
            raise IdPError("PIN must be 6 digits")
        key = ("demo-" if origin == "demo" else "op-") + secrets.token_hex(4)
        demo_pin = pin if origin == "demo" else None  # only demo personas keep their PIN (shown to Dr. Hofer again)
        self.store.exec("INSERT INTO personas (key, given, family, birthdate, pin_hash, origin, created, demo_pin) "
                        "VALUES (?,?,?,?,?,?,datetime('now'),?)", (key, given.strip(), family.strip(), birthdate,
                                                                 self._pin_hash(pin), origin, demo_pin))
        return {"key": key, "given": given.strip(), "family": family.strip(), "birthdate": birthdate, "pin": pin}

    def protected_names(self) -> list[str]:
        return [f"{p['given']} {p['family']}" for p in self.personas()] + list(self.protected_extra())

    # ------------------------------------------------------------ authentication
    @staticmethod
    def _tkey(client: str, given: str, family: str) -> str:
        """Throttle per client AND claimed identity: guessing one person's PIN is limited, other people are not
        locked out (several outside persons can share an IP, e.g. a hospital NAT)."""
        return f"{client}|{' '.join(rule_tokens(given))}|{' '.join(rule_tokens(family))}"

    def _throttle(self, client: str) -> None:
        with self._lock:
            now = time.monotonic()
            fails = [t for t in self._fails.get(client, []) if now - t < 600]
            self._fails[client] = fails
            if len(fails) >= 10:
                raise IdPError("Zu viele fehlgeschlagene Anmeldungen. Bitte später erneut versuchen.")

    def _failed(self, client: str) -> None:
        with self._lock:
            self._fails.setdefault(client, []).append(time.monotonic())

    def authenticate(self, given: str, family: str, birthdate: str, pin: str, client: str) -> dict:
        """-> identity {given, family, birthdate, persona}. Raises IdPError (generic message, nothing revealed)."""
        given, family, pin = (given or "").strip(), (family or "").strip(), (pin or "").strip()
        client = self._tkey(client, given, family)
        self._throttle(client)
        if not given or not family:
            raise IdPError("Vorname und Nachname sind nötig.")
        if pin:
            ph = self._pin_hash(pin)
            for p in self.store.q("SELECT * FROM personas"):
                if hmac.compare_digest(p["pin_hash"], ph) and (_same_name(given, family, p["given"], p["family"])
                                                                or fhirmap.identity_matches(f"{p['given']} {p['family']}", given, family)):
                    return {"given": p["given"], "family": p["family"], "birthdate": p["birthdate"], "persona": p["key"]}
            self._failed(client)
            raise IdPError("Anmeldung fehlgeschlagen (Name oder PIN falsch).")
        if any(fhirmap.identity_matches(n, given, family) or _same_name(given, family, *_split(n))
               for n in self.protected_names()):
            self._failed(client)
            raise IdPError("Diese Identität ist geschützt: Bestätigung mit der PIN der ID-Austria-App nötig.")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birthdate or ""):
            raise IdPError("Geburtsdatum nötig.")
        return {"given": given, "family": family, "birthdate": birthdate, "persona": None}

    # ------------------------------------------------------------ token
    def issue(self, ident: dict, aud: str) -> str:
        now = int(time.time())
        claims = {"iss": self.iss, "sub": pairwise_sub(bpk(ident["given"], ident["family"], ident["birthdate"]), self.salt),
                  "aud": aud, "iat": now, "exp": now + self.ttl, "nonce": secrets.token_hex(12), "jti": secrets.token_hex(16),
                  "given_name": ident["given"], "family_name": ident["family"], "acr": ACR_SIMULATED,
                  "simulated": True}
        with self._lock:
            self._userinfo = {k: v for k, v in self._userinfo.items() if v[0] > time.time()}
            self._userinfo[claims["jti"]] = (claims["exp"], ident["birthdate"])
        return sign_jws(self.key, claims)

    def userinfo_birthdate(self, jti: str) -> str | None:
        """The eID birth date, server-side (like a userinfo call): never in the token, never in FHIR.
        Used once, for the patient-as-recipient rule of fhirmap.identity_matches."""
        with self._lock:
            v = self._userinfo.pop(jti, None)
        return v[1] if v and v[0] > time.time() else None

    def verify(self, token: str, aud: str) -> dict:
        try:
            h64, c64, s64 = token.split(".")
            header, claims = json.loads(unb64u(h64)), json.loads(unb64u(c64))
        except Exception as e:
            raise IdPError("malformed token") from e
        if header.get("alg") != "ES256" or header.get("kid") != self.key.kid:
            raise IdPError("wrong algorithm or key")
        if not self.key.verify(f"{h64}.{c64}".encode(), unb64u(s64)):
            raise IdPError("bad signature")
        if claims.get("iss") != self.iss or claims.get("aud") != aud:
            raise IdPError("wrong issuer or audience")
        if not (claims.get("iat", 0) - 30 <= time.time() <= claims.get("exp", 0)):
            raise IdPError("expired")
        if claims.get("acr") != ACR_SIMULATED or claims.get("simulated") is not True:
            raise IdPError("not a token of the simulated IdP")
        for k in ("sub", "given_name", "family_name", "jti", "nonce"):
            if not claims.get(k):
                raise IdPError(f"missing {k}")
        if "birthdate" in claims or str(claims.get("sub", "")).startswith("GH:"):
            raise IdPError("token carries data it must not carry")
        return claims

    def jwks(self) -> dict:
        return {"keys": [self.key.jwk()]}


_TITLES = {"dr", "dr.", "prof", "prof.", "univ", "univ.", "doz", "doz.", "mag", "mag.", "med", "med.", "di", "dipl",
           "dipl.", "ing", "ing.", "msc", "bsc", "ma", "ba", "phd", "mba", "dr.in", "dr.med.", "frau", "herr"}


def split_person_name(name: str) -> tuple[str, str]:
    """'Dr. Anna Novak' -> ('Anna', 'Novak'); 'Univ.-Prof. Dr. Eva Maria Brandl-Huber, MSc' -> ('Eva Maria',
    'Brandl-Huber'). Titles and anything after a comma are dropped; the last remaining word is the family name."""
    words = []
    for w in name.split(",")[0].split():
        parts = [x for x in w.split("-") if x]
        if all(x.casefold() in _TITLES or x.casefold().rstrip(".") in _TITLES for x in parts):
            continue
        words.append(w)
    if not words:
        return "", ""
    return " ".join(words[:-1]), words[-1]


def _split(name: str) -> tuple[str, str]:
    toks = name.split()
    return " ".join(toks[:-1]), toks[-1] if toks else ""
