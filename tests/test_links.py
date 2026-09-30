"""Offline tests for bin/resolve_links.py and the manifest -> pdf/html mapping.

Run with: uv run --frozen --with pytest pytest tests
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import resolve_links as rl  # noqa: E402
import update_references as ur  # noqa: E402

DOI = "10.1234/abc"
UNPAYWALL_URL = "https://api.unpaywall.org/v2/"
EPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class FakeHttp:
    def __init__(self, unpaywall=None, epmc=None):
        self.unpaywall, self.epmc, self.calls = unpaywall, epmc, []

    def get_json(self, url, params=None):
        self.calls.append(url)
        return self.unpaywall if url.startswith(UNPAYWALL_URL) else self.epmc


def unpaywall(pdf=None, lic="cc-by", oa=True):
    loc = {"url_for_pdf": pdf, "url": "https://doi.org/" + DOI, "license": lic}
    return {"is_oa": oa, "best_oa_location": loc, "oa_locations": [loc]}


def epmc(pmcid="PMC1", oa="Y", pdf=True):
    r = {"doi": DOI.upper(), "pmcid": pmcid, "isOpenAccess": oa, "license": "cc by"}
    if pdf:
        r["fullTextUrlList"] = {"fullTextUrl": [
            {"documentStyle": "pdf", "availability": "Open access", "site": "Europe_PMC",
             "url": f"https://europepmc.org/articles/{pmcid}?pdf=render"}]}
    return {"resultList": {"result": [r]}}


def resolve(http, **kw):
    return rl.resolve_entry("Key1", DOI, http, "a@b.c", **kw)


def test_override_wins_and_skips_network():
    http = FakeHttp(unpaywall(pdf="https://x/u.pdf"), epmc())
    rec = resolve(http, overrides={"Key1": "https://x/o.pdf"})
    assert rec["kind"] == "override" and rec["pdf"] == "https://x/o.pdf"
    assert http.calls == []


def test_unpaywall_pdf_beats_europepmc_but_records_pmcid():
    rec = resolve(FakeHttp(unpaywall(pdf="https://x/u.pdf"), epmc()))
    assert (rec["kind"], rec["source"], rec["pdf"]) == ("pdf", "unpaywall", "https://x/u.pdf")
    assert rec["pmcid"] == "PMC1" and rec["license"] == "cc-by"


def test_europepmc_used_when_unpaywall_has_no_pdf():
    rec = resolve(FakeHttp(unpaywall(pdf=None), epmc()))
    assert (rec["kind"], rec["source"]) == ("pdf", "europepmc")
    assert rec["pdf"] == "https://europepmc.org/articles/PMC1?pdf=render"
    assert rec["is_oa"] is True


def test_landing_fallback_when_nothing_found():
    rec = resolve(FakeHttp(None, None))
    assert rec == {"doi": DOI, "pdf": "https://doi.org/" + DOI, "kind": "landing", "source": "doi"}


def test_landing_when_not_open_access():
    rec = resolve(FakeHttp(unpaywall(pdf=None, oa=False), epmc(oa="N", pdf=False)))
    assert rec["kind"] == "landing"


def test_bucket_only_when_configured_and_declared():
    http = FakeHttp(None, None)
    assert resolve(http, bucket_base="https://b.org", bucket_keys=set())["kind"] == "landing"
    assert resolve(http, bucket_keys={"Key1"})["kind"] == "landing"  # no base configured
    rec = resolve(http, bucket_base="https://b.org/", bucket_keys={"Key1"})
    assert (rec["kind"], rec["pdf"]) == ("bucket", "https://b.org/Key1.pdf")


def test_no_doi_needs_override_or_bucket():
    http = FakeHttp()
    assert rl.resolve_entry("T", None, http, "a@b.c") is None
    rec = rl.resolve_entry("T", None, http, "a@b.c", bucket_base="https://b.org", bucket_keys={"T"})
    assert rec["pdf"] == "https://b.org/T.pdf"
    assert http.calls == []


def test_only_missing_keeps_pdf_records_but_retries_landing():
    existing = {"pdf": "https://x/old.pdf", "kind": "pdf", "source": "unpaywall", "doi": DOI, "resolved": "2026-01-01"}
    http = FakeHttp(None, None)
    assert resolve(http, existing=existing)["pdf"] == "https://x/old.pdf" and http.calls == []
    landing = {"pdf": "https://doi.org/" + DOI, "kind": "landing", "source": "doi", "doi": DOI}
    rec = resolve(FakeHttp(unpaywall(pdf="https://x/new.pdf")), existing=landing)
    assert rec["kind"] == "pdf"
    assert resolve(FakeHttp(unpaywall(pdf="https://x/new.pdf")), existing=existing, only_missing=False)["pdf"] == "https://x/new.pdf"


def test_resolved_date_only_changes_with_content():
    entries = [{"ID": "Key1", "doi": DOI}]
    http = FakeHttp(unpaywall(pdf="https://x/u.pdf"))
    m1 = rl.resolve_all(entries, {}, http, "a@b.c", {}, None, set(), today="2026-02-01")
    m2 = rl.resolve_all(entries, m1, http, "a@b.c", {}, None, set(), only_missing=False, today="2026-03-01")
    assert m2["Key1"]["resolved"] == "2026-02-01"
    m3 = rl.resolve_all(entries, m1, FakeHttp(unpaywall(pdf="https://x/v.pdf")), "a@b.c", {}, None, set(),
                        only_missing=False, today="2026-03-01")
    assert m3["Key1"]["resolved"] == "2026-03-01"
    assert rl.dump_manifest(m1) == rl.dump_manifest(dict(reversed(list(m1.items()))))


def test_manifest_to_pdf_and_html_fields():
    df = pd.DataFrame([{"ID": "A"}, {"ID": "B"}, {"ID": "C"}, {"ID": "D"}])
    manifest = {
        "A": {"pdf": "https://x/a.pdf", "kind": "pdf"},
        "B": {"pdf": "https://doi.org/10.1/b", "kind": "landing"},
        "C": {"pdf": "https://b.org/C.pdf", "kind": "bucket"},
    }
    out = ur.add_links(df, manifest).set_index("ID")
    assert out.loc["A", "pdf"] == "https://x/a.pdf" and pd.isnull(out.loc["A", "html"])
    assert pd.isnull(out.loc["B", "pdf"]) and out.loc["B", "html"] == "https://doi.org/10.1/b"
    assert out.loc["C", "pdf"] == "https://b.org/C.pdf"
    assert pd.isnull(out.loc["D", "pdf"]) and pd.isnull(out.loc["D", "html"])
