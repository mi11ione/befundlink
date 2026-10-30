"""Key material. One master secret (PGK_SECRET, >= 32 bytes) -> independent per-purpose keys via HKDF-SHA256,
so a leak or misuse of one purpose (e.g. CSRF) never yields another (e.g. the audit chain key).
The mock ID Austria signs with its own ES256 key pair (data/idp_es256.pem, gitignored, 0600); its public
key is published as a JWKS so the signature in Provenance can be verified independently."""
from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .store import write_private

PURPOSES = ("csrf", "audit-chain", "pin", "contact")


def derive(master: bytes, purpose: str) -> bytes:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown key purpose {purpose}")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"pgk-bridge/v1", info=purpose.encode()).derive(master)


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class ES256Key:
    """P-256 signing key for the mock IdP, persisted once."""

    def __init__(self, path: Path | None):
        if path is not None and path.exists():
            self.key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
            if path is not None:  # 0600 from creation on, in a 0700 directory
                write_private(path, self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
        nums = self.key.public_key().public_numbers()
        self.x, self.y = nums.x.to_bytes(32, "big"), nums.y.to_bytes(32, "big")
        self.kid = b64u(hashes_sha256(self.x + self.y))[:16]

    def jwk(self) -> dict:
        return {"kty": "EC", "crv": "P-256", "x": b64u(self.x), "y": b64u(self.y), "use": "sig", "alg": "ES256", "kid": self.kid}

    def sign(self, data: bytes) -> bytes:
        r, s = decode_dss_signature(self.key.sign(data, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def verify(self, data: bytes, sig: bytes) -> bool:
        if len(sig) != 64:
            return False
        try:
            self.key.public_key().verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                                         data, ec.ECDSA(hashes.SHA256()))
            return True
        except Exception:
            return False


def sign_jws(key: "ES256Key", claims: dict) -> str:
    """Compact JWS (RFC 7515), ES256."""
    import json
    header = {"alg": "ES256", "typ": "JWT", "kid": key.kid}
    si = f"{b64u(json.dumps(header, separators=(',', ':')).encode())}." \
         f"{b64u(json.dumps(claims, ensure_ascii=False, separators=(',', ':')).encode())}"
    return f"{si}.{b64u(key.sign(si.encode()))}"


def hashes_sha256(b: bytes) -> bytes:
    h = hashes.Hash(hashes.SHA256())
    h.update(b)
    return h.finalize()
