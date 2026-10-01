"""Offline tests for `scholar_overrides` in bin/update_references.py.

Run with: uv run --frozen --with pytest pytest tests
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import update_references as ur  # noqa: E402

USER = "kel6QbEAAAAJ"


def gs_data(*items):
    """items: (pubid, title, year[, doi])"""
    papers = {}
    for it in items:
        pubid, title, year = it[:3]
        d = {"title": title, "year": year, "citations": 1}
        if len(it) > 3:
            d["doi"] = it[3]
        papers[f"{USER}:{pubid}"] = d
    return {"papers": papers}


def refs(*items):
    """items: (ID, title, year, doi)"""
    return pd.DataFrame([{"ID": i, "title": t, "year": y, "doi": d} for i, t, y, d in items])


FEI = ("Fei2026-zj", "Human endogenous retrovirus profiling reveals heterogenous expression in cutaneous melanoma",
       2026, "10.3389/FONC.2026.1708501")
FEI_GS = ("AAA", "Human Endogenous Retrovirus (HERV) Profiling Reveals Heterogenous Expression in Cutaneous Melanoma",
          "Unknown Year")


def gsid(df, bibkey):
    return df.loc[df["ID"] == bibkey, "google_scholar_id"].iloc[0]


def test_title_mismatch_is_unmatched_without_override():
    df, _ = ur.add_google_scholar(refs(FEI), gs_data(FEI_GS))
    assert pd.isnull(gsid(df, "Fei2026-zj"))


def test_override_by_doi_case_insensitive():
    df, gs = ur.add_google_scholar(refs(FEI), gs_data(FEI_GS), {"10.3389/fonc.2026.1708501": "AAA"})
    assert gsid(df, "Fei2026-zj") == "AAA"
    assert gs.loc[0, "match_on"] == "override"


def test_override_by_doi_url_form():
    df, _ = ur.add_google_scholar(refs(FEI), gs_data(FEI_GS), {"https://doi.org/10.3389/fonc.2026.1708501": "AAA"})
    assert gsid(df, "Fei2026-zj") == "AAA"


def test_override_by_bibkey():
    df, gs = ur.add_google_scholar(refs(FEI), gs_data(FEI_GS), {"Fei2026-zj": "AAA"})
    assert gsid(df, "Fei2026-zj") == "AAA"
    assert gs.loc[0, "match_on"] == "override"


def test_bare_and_full_id_are_equivalent_and_output_is_bare():
    for value in ("AAA", f"{USER}:AAA"):
        df, _ = ur.add_google_scholar(refs(FEI), gs_data(FEI_GS), {"Fei2026-zj": value})
        assert gsid(df, "Fei2026-zj") == "AAA"


def test_missing_scholar_id_warns_and_falls_back(capsys):
    df, _ = ur.add_google_scholar(refs(FEI), gs_data(FEI_GS), {"Fei2026-zj": "NOPE"})
    assert "not in citations.yml" in capsys.readouterr().err
    assert pd.isnull(gsid(df, "Fei2026-zj"))


def test_unknown_override_key_warns(capsys):
    df, _ = ur.add_google_scholar(refs(FEI), gs_data(FEI_GS), {"10.9999/nothing": "AAA"})
    assert "matches no entry" in capsys.readouterr().err
    assert pd.isnull(gsid(df, "Fei2026-zj"))


def test_duplicate_claim_warns_and_first_key_wins(capsys):
    r = refs(FEI, ("Other2020-xx", "Something unrelated entirely", 2020, "10.1/other"))
    df, _ = ur.add_google_scholar(r, gs_data(FEI_GS), {"Fei2026-zj": "AAA", "Other2020-xx": "AAA"})
    err = capsys.readouterr().err
    assert "claimed by both" in err
    assert gsid(df, "Fei2026-zj") == "AAA"  # sorted key order: Fei... < Other...
    assert pd.isnull(gsid(df, "Other2020-xx"))


def test_override_takes_precedence_over_automatic_match():
    # An exact title+year match exists for X, but the override points X elsewhere.
    r = refs(("X2020-aa", "Exact title match", 2020, None))
    g = gs_data(("EXACT", "Exact title match", "2020"), ("OTHER", "Some other listing", "2020"))
    df, gs = ur.add_google_scholar(r, g, {"X2020-aa": "OTHER"})
    assert gsid(df, "X2020-aa") == "OTHER"
    # ... and the exact-match Scholar entry stays unclaimed
    assert pd.isnull(gs.loc[gs["gs_id"] == "EXACT", "matched_to"].iloc[0])


def test_overridden_scholar_entry_cannot_be_claimed_automatically():
    # Y would match "Shared title" automatically, but the override gave it to X.
    r = refs(("X2020-aa", "Totally different title", 2020, None), ("Y2020-bb", "Shared title", 2020, None))
    g = gs_data(("SHARED", "Shared title", "2020"))
    df, _ = ur.add_google_scholar(r, g, {"X2020-aa": "SHARED"})
    assert gsid(df, "X2020-aa") == "SHARED"
    assert pd.isnull(gsid(df, "Y2020-bb"))


def test_published_inherits_preprint_id_but_own_override_wins():
    pubinfo = {"preprint_published": [{"preprint": "10.1101/pre", "published": "10.1000/pub"}]}
    r = refs(("Pre2023-aa", "Preprint title", 2023, "10.1101/pre"), ("Pub2025-bb", "Published title", 2025, "10.1000/pub"))
    g = gs_data(("PRE", "Preprint title", "2023"), ("PUB", "Different published listing", "2025"))

    # inherit: only the preprint matches automatically
    df, _ = ur.add_google_scholar(r.copy(), g, None, pubinfo)
    out = ur.remove_published_preprints(df, pubinfo)
    assert list(out["ID"]) == ["Pub2025-bb"]
    assert out.iloc[0]["google_scholar_id"] == "PRE"

    # override on the published entry wins over inheritance
    df, _ = ur.add_google_scholar(r.copy(), g, {"10.1000/pub": "PUB"}, pubinfo)
    out = ur.remove_published_preprints(df, pubinfo)
    assert out.iloc[0]["google_scholar_id"] == "PUB"


def test_collapsed_preprint_not_reported_as_unmatched(capsys):
    pubinfo = {"preprint_published": [{"preprint": "10.1101/pre", "published": "10.1000/pub"}]}
    r = refs(("Pre2023-aa", "Preprint with no scholar listing", 2023, "10.1101/pre"),
             ("Pub2025-bb", "Published title", 2025, "10.1000/pub"))
    ur.add_google_scholar(r, gs_data(("PUB", "Published title", "2025")), None, pubinfo)
    assert "Pre2023-aa" not in capsys.readouterr().err


def test_unmatched_report_suggests_ready_to_paste_override(capsys):
    ur.add_google_scholar(refs(FEI), gs_data(FEI_GS))
    err = capsys.readouterr().err
    assert "Fei2026-zj" in err
    assert 'paste under scholar_overrides:  10.3389/fonc.2026.1708501: "AAA"' in err


def test_suggestion_uses_bibkey_when_no_doi_and_skips_dissimilar(capsys):
    r = refs(("Thesis2019-aa", "Characterization of the HERV transcriptome", 2019, None))
    ur.add_google_scholar(r, gs_data(("ZZZ", "Fatal hepatic necrosis associated with trazodone", "1994")))
    err = capsys.readouterr().err
    assert "no similar Google Scholar titles" in err
    assert "paste under" not in err


def test_build_bibtex_reads_overrides_from_pubinfo(tmp_path):
    bib = tmp_path / "paperpile.bib"
    bib.write_text(
        "@ARTICLE{Fei2026-zj,\n  title = \"Human endogenous retrovirus profiling reveals heterogenous "
        "expression in cutaneous melanoma\",\n  author = \"Fei, T\",\n  year = 2026,\n"
        "  doi = \"10.3389/fonc.2026.1708501\"\n}\n"
    )
    pubinfo = tmp_path / "pubinfo.yml"
    pubinfo.write_text('scholar_overrides:\n  "10.3389/fonc.2026.1708501": "AAA"\n')
    cites = tmp_path / "citations.yml"
    cites.write_text(
        "papers:\n  'kel6QbEAAAAJ:AAA':\n    citations: 0\n    year: Unknown Year\n"
        "    title: Human Endogenous Retrovirus (HERV) Profiling Reveals Heterogenous Expression in Cutaneous Melanoma\n"
    )
    out = ur.build_bibtex(bib, pubinfo, tmp_path / "previews", cites, None)
    assert "google_scholar_id = {AAA}" in out
