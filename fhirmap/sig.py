"""Verify the identity assertion kept in Provenance.signature (JWS compact, sigFormat application/jose).

    verify_provenance_signature(provenance, jwks) -> {valid, alg, kid, claims, problems, signatures}

``jwks`` is a JWK Set ({"keys": [...]}) or a single JWK. Supported: ES256 (the IdP's asymmetric
key, the target design), RS256, and HS256 with an "oct" key (the service's current HMAC statement).
Uses ``cryptography`` for ES/RS (imported lazily; HS256 needs only the stdlib).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _keys(jwks) -> list[dict]:
    if isinstance(jwks, str):
        jwks = json.loads(jwks)
    if isinstance(jwks, dict) and "keys" in jwks:
        return list(jwks["keys"])
    return [jwks] if isinstance(jwks, dict) else list(jwks or [])


def _verify(alg: str, key: dict, signing_input: bytes, sig: bytes) -> bool:
    if alg == "HS256":
        if key.get("kty") != "oct":
            return False
        return hmac.compare_digest(hmac.new(_unb64u(key["k"]), signing_input, hashlib.sha256).digest(), sig)
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    try:
        if alg == "ES256":
            if key.get("kty") != "EC" or key.get("crv") != "P-256" or len(sig) != 64:
                return False
            pub = ec.EllipticCurvePublicNumbers(int.from_bytes(_unb64u(key["x"]), "big"),
                                                int.from_bytes(_unb64u(key["y"]), "big"),
                                                ec.SECP256R1()).public_key()
            der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
            pub.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
            return True
        if alg == "RS256":
            if key.get("kty") != "RSA":
                return False
            pub = rsa.RSAPublicNumbers(int.from_bytes(_unb64u(key["e"]), "big"),
                                       int.from_bytes(_unb64u(key["n"]), "big")).public_key()
            pub.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
            return True
    except (InvalidSignature, ValueError, KeyError):
        return False
    return False


def verify_jws(token: str, jwks) -> dict:
    """Verify one JWS compact token against a JWK Set. {valid, alg, kid, claims, problems}."""
    out = {"valid": False, "alg": None, "kid": None, "claims": None, "problems": []}
    parts = (token or "").strip().split(".")
    if len(parts) != 3:
        out["problems"].append("not a JWS compact token (header.payload.signature)")
        return out
    try:
        header = json.loads(_unb64u(parts[0]))
        out["claims"] = json.loads(_unb64u(parts[1]))
        sig = _unb64u(parts[2])
    except (ValueError, json.JSONDecodeError):
        out["problems"].append("token parts are not valid base64url JSON")
        return out
    alg, kid = header.get("alg"), header.get("kid")
    out["alg"], out["kid"] = alg, kid
    if alg not in ("ES256", "RS256", "HS256"):
        out["problems"].append(f"unsupported or unsafe alg {alg!r}")
        return out
    keys = [k for k in _keys(jwks) if not kid or k.get("kid") in (None, kid)]
    if not keys:
        out["problems"].append(f"no key with kid {kid!r} in the JWKS")
        return out
    try:
        ok = any(_verify(alg, k, f"{parts[0]}.{parts[1]}".encode("ascii"), sig) for k in keys)
    except ImportError:
        out["problems"].append("the 'cryptography' package is needed for ES256/RS256")
        return out
    out["valid"] = ok
    if not ok:
        out["problems"].append("signature does not verify with the given key(s)")
    return out


def verify_provenance_signature(provenance: dict, jwks, *, check_signer: bool = True,
                                require_binding: bool = True, id_map: dict | None = None,
                                resources: list | None = None) -> dict:
    """Verify every application/jose signature of a Provenance.

    - the JWS verifies against ``jwks``;
    - with ``check_signer``: the token's given_name/family_name is the person the statement is about:
      ``signature.onBehalfOf`` when ``signature.who`` is the verifier Device (the service signs on behalf of the
      verified sender), else ``signature.who`` (the earlier shape, so older filings still verify);
    - with ``require_binding``: the token's ``content_sha256`` equals the SHA-256 of the Provenance's
      source entity, and its ``targets`` equal the Provenance's targets, so a statement cannot be
      moved onto another filing. ``id_map`` maps the urn:uuid references in the token to server
      references after the transaction ({'urn:uuid:..': 'DiagnosticReport/12', ...}), for verifying a
      Provenance read back from the FHIR server.
    - with ``resources`` (the filed resources, as built or read back from the server): the token's
      ``resources_sha256`` must equal the clinical digest of those resources, so an Observation value edited
      after filing fails; ``changed`` lists the resources that differ (server reference where known).
    Returns {valid, signatures, results: [...], problems, alg, kid, claims, bound, changed}."""
    from .build import _source_sha256
    from .identity import identity_matches
    sigs = [s for s in (provenance or {}).get("signature") or [] if s.get("sigFormat") == "application/jose"]
    res = {"valid": False, "signatures": len(sigs), "results": [], "problems": [], "alg": None, "kid": None,
           "claims": None}
    if not sigs:
        res["problems"].append("Provenance has no application/jose signature")
        return res
    for s in sigs:
        try:
            token = base64.b64decode(s.get("data") or "").decode("ascii")
        except (ValueError, UnicodeDecodeError):
            r = {"valid": False, "problems": ["signature.data is not base64 of an ASCII JWS"], "claims": None}
        else:
            r = verify_jws(token, jwks)
        c = r.get("claims") or {}
        who, field = _signed_person(s, resources, id_map)
        if r["valid"] and check_signer and (c.get("given_name") or c.get("family_name")):
            if not identity_matches(who, c.get("given_name") or "", c.get("family_name") or ""):
                r["valid"] = False
                r["problems"].append(f"the token's person is not {field}")
        r["bound"] = False
        if require_binding and r.get("claims") is not None:
            want_sha = _source_sha256(provenance)
            targets = {t.get("reference") for t in provenance.get("target") or []}
            claimed = {(id_map or {}).get(t, t) for t in (c.get("targets") or [])}
            if not c.get("content_sha256") or c.get("content_sha256") != want_sha:
                r["valid"] = False
                r["problems"].append("statement does not cover this answer's SHA-256 (content_sha256)")
            elif not c.get("targets") or not _same_targets(claimed, targets):
                r["valid"] = False
                r["problems"].append("statement does not cover exactly this Provenance's targets")
            else:
                r["bound"] = True
        if r.get("claims") is not None and resources is not None:
            r["changed"] = _content_check(c, resources, provenance, id_map, r)
        res["results"].append(r)
    first = res["results"][0]
    res.update(alg=first.get("alg"), kid=first.get("kid"), claims=first.get("claims"))
    res["valid"] = all(r["valid"] for r in res["results"])
    res["bound"] = all(r.get("bound") for r in res["results"]) if require_binding else None
    res["changed"] = [x for r in res["results"] for x in r.get("changed") or []]
    res["problems"] = [p for r in res["results"] for p in r["problems"]]
    return res


def _is_device(ref: dict) -> bool:
    from .build import SID_SERVICE
    return (ref.get("type") == "Device" or str(ref.get("reference") or "").startswith("Device/")
            or (ref.get("identifier") or {}).get("system") == SID_SERVICE)


def _signed_person(sig: dict, resources: list | None, id_map: dict | None) -> tuple[str, str]:
    """(name, element) of the person a signature speaks for. New shape: who = the verifier Device, onBehalfOf =
    the sender's Practitioner, or the Patient when she sent it herself (its display, else the name of the referenced
    resource in ``resources``).
    Old shape: who = the sender's Practitioner (its display)."""
    who = sig.get("who") or {}
    if not _is_device(who):
        return who.get("display") or "", "signature.who"
    ob = sig.get("onBehalfOf") or {}
    if ob.get("display"):
        return ob["display"], "signature.onBehalfOf"
    ref = ob.get("reference") or ""
    refs = {ref, (id_map or {}).get(ref, ref)}
    for res in resources or []:
        rt = res.get("resourceType")
        if rt in ("Practitioner", "Patient") and refs & {f"{rt}/{res.get('id')}", f"urn:uuid:{res.get('id')}"}:
            n = (res.get("name") or [{}])[0]
            return (n.get("text") or " ".join([*(n.get("given") or []), n.get("family") or ""]).strip()), \
                "signature.onBehalfOf"
    return "", "signature.onBehalfOf"


def _same_targets(claimed: set, stored: set) -> bool:
    """The signed targets are exactly the Provenance's targets. HAPI strips versions from stored references by
    default, so a signed 'Observation/12/_history/2' matches a stored 'Observation/12'; a version the server did
    keep must be the signed one. The version itself stays bound through the content check, which reads every
    resource at the version recorded at filing."""
    from .build import unversioned
    if claimed == stored:
        return True
    if len(claimed) != len(stored) or {unversioned(x) for x in claimed} != {unversioned(t) for t in stored}:
        return False
    return all(t in claimed for t in stored if "/_history/" in t)


def _content_check(claims: dict, resources: list, provenance: dict, id_map: dict | None, r: dict) -> list[str]:
    """Compare the token's resources_sha256 / resource_digests with the given resources."""
    from .build import CLINICAL_TYPES, clinical_digest
    want = claims.get("resources_sha256")
    if not want:
        r["valid"] = False
        r["problems"].append("statement does not cover the filed clinical content (resources_sha256)")
        return []
    from .build import unversioned
    targets = {unversioned(t.get("reference")) for t in provenance.get("target") or []}
    type_of = {f"urn:uuid:{x.get('id')}": x["resourceType"] for x in resources if x.get("id")}
    mine = []
    for res in resources:
        if res.get("resourceType") not in CLINICAL_TYPES:
            continue
        ref = f"{res['resourceType']}/{res.get('id')}"
        urn = f"urn:uuid:{res.get('id')}"
        if targets and ref not in targets and urn not in targets:
            continue                           # not part of this filing
        mine.append((ref if ref in targets else urn, clinical_digest(res, type_of)))
    whole = hashlib.sha256("\n".join(sorted(d for _, d in mine)).encode("ascii")).hexdigest()
    if whole == want:
        return []
    r["valid"] = False
    claimed = claims.get("resource_digests") or {}
    got = {d for _, d in mine}
    changed = []
    for ref, d in mine:                        # read-back resources whose content is not in the statement
        if d not in set(claimed.values()):
            changed.append(ref)
    for urn, d in claimed.items():             # signed resources that are no longer present unchanged
        if d not in got:
            server = (id_map or {}).get(urn, urn)
            if server not in changed:
                changed.append(server)
    if not changed:
        changed = ["(resource set differs)"]
    r["problems"].append("filed clinical content differs from what was signed: " + ", ".join(sorted(set(changed))))
    return sorted(set(changed))
