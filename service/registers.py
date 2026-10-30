"""Register pre-flight (MOCK adapter over service/data/registers.json).

Policy:
  lab_result        -> person in the ÖÄK Ärzteliste with a lab-medicine specialty
  discharge_report  -> organisation in the GDA-Index + the person's role at that organisation
Unknown -> warning; the clinician decides (role then recorded as 'asserted by requester').
Production: eHealth-Verzeichnisdienst / GDA-Index (as a GDA) and the public ÖÄK Ärzteliste.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import fhirmap
from fhirmap.identity import compare_names


def names_equal(a: str, b: str) -> bool:
    """Register lookup: the strict RULES.md name comparison (titles/case/ICAO umlauts normalised, order kept)."""
    return compare_names(a, b) == "same"

LAB_SPECIALTY = re.compile(r"labor|labordiagnostik|laboratory|klinische chemie|mikrobiologie|hämatologie|haematologie", re.I)
POLICY_URI = fhirmap.POLICY  # one policy URI scheme (fhirmap): https://pgk-bridge.example/fhir/policy/<name>


def org_key(org: str | None) -> str:
    return re.sub(r"\W+", "", (org or "").casefold().replace("ß", "ss"))


class Registers:
    def __init__(self, path: str | Path):
        self.data = json.loads(Path(path).read_text(encoding="utf-8"))

    @property
    def aerzteliste(self) -> dict:
        return self.data["aerzteliste"]

    @property
    def gda(self) -> dict:
        return self.data["gda_index"]

    def find_physician(self, name: str) -> dict | None:
        return next((e for e in self.aerzteliste["entries"] if names_equal(e["name"], name)), None)

    def find_org(self, org: str | None) -> dict | None:
        k = org_key(org)
        return next((e for e in self.gda["entries"] if k and org_key(e["name"]) == k), None) if k else None

    def member(self, mxid: str) -> dict:
        return next((m for m in (self.data.get("network") or {}).get("members", []) if m["mxid"] == mxid), {})

    def clinic(self) -> dict:
        return self.data.get("clinic") or {}

    def member_name(self, mxid: str) -> str | None:
        """Display name of a network member (the requester) from the PGK directory."""
        m = next((m for m in (self.data.get("network") or {}).get("members", []) if m["mxid"] == mxid), None)
        return m["name"] if m else None

    def org_domains(self) -> dict[str, list[str]]:
        return {e["name"]: list(e.get("domains") or []) for e in self.gda["entries"]}

    def preflight(self, name: str, role: str | None, org: str | None, doc_kind: str) -> dict:
        """Returns {ok, policy, register, source, source_url, name, detail, text, message, entry}.
        ok=False -> warning, needs confirmation."""
        res = self._preflight(name, role, org, doc_kind)
        e = res.get("entry") or {}
        res["detail"] = e.get("specialty") or ((e.get("staff") or {}).get("role") if isinstance(e.get("staff"), dict) else None)
        res["text"] = ("✓ " if res["ok"] else "⚠ ") + res["message"]
        return res

    def _preflight(self, name: str, role: str | None, org: str | None, doc_kind: str) -> dict:
        if doc_kind == "lab_result":
            e = self.find_physician(name)
            base = {"policy": POLICY_URI[doc_kind], "register": self.aerzteliste["label"], "source": self.aerzteliste["label"],
                    "source_url": self.aerzteliste["source"], "name": name}
            if not e:
                return {**base, "ok": False, "entry": None,
                        "message": f"{name}: NICHT in der {self.aerzteliste['label']} gefunden. Trotzdem senden? "
                                   f"Die Rolle wird dann als 'vom Anfordernden angegeben' vermerkt."}
            if not LAB_SPECIALTY.search(e["specialty"]):
                return {**base, "ok": False, "entry": e,
                        "message": f"{name}: in der {self.aerzteliste['label']}, aber Fach '{e['specialty']}' ist keine Labormedizin."}
            return {**base, "ok": True, "entry": e,
                    "message": f"{name}: in der {self.aerzteliste['label']} gefunden, Fach {e['specialty']}."}
        e = self.find_org(org)
        base = {"policy": POLICY_URI.get(doc_kind, fhirmap.POLICY_OTHER), "register": self.gda["label"],
                "source": self.gda["label"], "source_url": self.gda["source"], "name": name}
        if not e:
            return {**base, "ok": False, "entry": None,
                    "message": f"Organisation '{org or '?'}' NICHT im {self.gda['label']} gefunden. Trotzdem senden?"}
        staff = next((s for s in e.get("staff") or [] if names_equal(s["name"], name)), None)
        if not staff:
            return {**base, "ok": False, "entry": {"org": e, "staff": None},
                    "message": f"{e['name']} ist im {self.gda['label']} ({e['type']}), aber {name} ist dort nicht gelistet. "
                               f"Trotzdem senden? Die Rolle wird als 'vom Anfordernden angegeben' vermerkt."}
        return {**base, "ok": True, "entry": {"org": e, "staff": staff},
                "message": f"{e['name']} im {self.gda['label']} ({e['type']}); {name}: {staff['role']}."}
