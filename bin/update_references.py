#! /usr/bin/env python
"""Generate _bibliography/papers.bib from a Paperpile BibTeX export.

Inputs (all paths default to locations relative to the repository root):
    - _bibliography/paperpile.bib        Paperpile export (source of truth)
    - _data/pubinfo.yml                  selected papers, preprint -> published pairs
    - _data/citations.yml                Google Scholar data (read only; refreshed by
                                         bin/update_scholar_citations.py)
    - _data/oa_links.yml                 public PDF links by bibkey (written by
                                         bin/resolve_links.py; read only here)
    - assets/pdf/ or $REFS_PDF_DIR       optional local PDFs named as in Paperpile's `file`
                                         field; only used to render preview images
    - assets/img/publication_preview/    images named `<bibkey>_<label>.<ext>`

The output is deterministic (no timestamps, no network access) so it is safe to use
from a git hook and to verify in CI with `--check`.
"""

import argparse
import difflib
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List
from unicodedata import normalize as unorm

import bibtexparser
import pandas as pd
import yaml
from bibtexparser.bibdatabase import BibDatabase
from bibtexparser.bwriter import BibTexWriter

REPO_ROOT = Path(__file__).resolve().parent.parent
PDF_KINDS = ("pdf", "override", "bucket")  # manifest kinds whose link is a PDF; "landing" is not


class PipelineError(Exception):
    """Unrecoverable problem; the script exits non-zero."""


def warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(msg, file=sys.stderr)


""" General Utilities """


def normtitle(s: str, drop_brackets=True) -> str:
    """Normalize a title string for matching."""
    s = re.sub("[\r\n]+", " ", s)
    s = unorm("NFC", s)
    s = s.upper()
    # Convert various dash-like characters to a standard dash
    s = re.sub(r"[-‐‑‒–—−﹣－]", "-", s)
    if drop_brackets:
        s = re.sub(r"[\[\]\(\)\{\}]", "", s)
    s = re.sub(r"\s+", " ", s)
    # Remove all non-alphanumeric characters except dashes and spaces
    s = re.sub(r"[^\w\- ]", "", s)
    return s.strip()


def norm_doi(s) -> str | None:
    if s is None or (isinstance(s, float) and pd.isnull(s)):
        return None
    s = str(s).strip().lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    return s or None


def ensure_columns(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df


def to_entry(row) -> Dict[str, Any]:
    return {k: v for k, v in row.items() if pd.notnull(v)}


""" File loading """


def bib_fromfile(source_bib: Path) -> BibDatabase:
    if not source_bib.is_file():
        raise PipelineError(f"Paperpile bibliography not found: {source_bib}")
    try:
        with open(source_bib) as f:
            return bibtexparser.load(f)
    except Exception as e:
        raise PipelineError(f"Could not parse {source_bib}: {e}") from e


def yaml_fromfile(path: Path, required: bool = False) -> Dict[str, Any]:
    if not path.is_file():
        if required:
            raise PipelineError(f"File not found: {path}")
        warn(f"{path} not found; continuing without it.")
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise PipelineError(f"Could not parse {path}: {e}") from e


""" Steps """


def add_local_pdf(ref_df: pd.DataFrame, pdfdir: Path) -> pd.DataFrame:
    """Set `local_pdf` when the file named in Paperpile's `file` field exists in pdfdir.
    Only used to render previews; it is dropped before the bibliography is written."""
    ref_df = ensure_columns(ref_df, "file")

    def _convert(x):
        if isinstance(x, str) and x and (pdfdir / Path(x).name).exists():
            return Path(x).name
        return None

    ref_df["local_pdf"] = ref_df["file"].map(_convert)
    return ref_df


def add_links(ref_df: pd.DataFrame, manifest: Dict[str, Any]) -> pd.DataFrame:
    """Set `pdf` (PDF kinds) or `html` (landing page) from the oa_links.yml manifest."""
    ref_df = ensure_columns(ref_df, "pdf", "html")
    missing = []
    for idx, row in ref_df.iterrows():
        rec = manifest.get(row["ID"]) or {}
        url = rec.get("pdf")
        if url and rec.get("kind") in PDF_KINDS:
            ref_df.at[idx, "pdf"] = url
        else:
            missing.append(row["ID"])
            if url and rec.get("kind") == "landing" and pd.isnull(row["html"]):
                ref_df.at[idx, "html"] = url
    if missing:
        warn(f"{len(missing)} entries have no PDF link ({', '.join(missing)}); "
             "run bin/resolve_links.py or add `pdf_overrides` / `pdf_bucket` in pubinfo.yml.")
    return ref_df


def make_preview_from_pdf(
    pdf_path: Path,
    outprefix: Path,
    npages: int | None = None,
    dpi: int = 72,
    write: bool = True,
) -> List[Path]:
    """Render the first `npages` pages to PNG. With write=False, only return the
    paths that would be created."""
    if not write:
        return [Path(f"{outprefix}_page{i+1}.png") for i in range(npages or 1)]

    import pymupdf  # imported lazily: only needed when previews are generated

    paths = []
    with pymupdf.open(pdf_path) as doc:
        n = doc.page_count if npages is None else min(npages, doc.page_count)
        for i in range(n):
            out = Path(f"{outprefix}_page{i+1}.png")
            doc[i].get_pixmap(dpi=dpi).save(out)
            info(f"Creating preview: {out}")
            paths.append(out)
    return paths


def _preview_rank(path: Path):
    """Sort key: abstract, then figures, then extracted pages."""
    stem = path.stem
    label = stem.rsplit("_", 1)[1] if "_" in stem else ""
    if re.search(r"abstract", label, re.I):
        return 0, label
    if m := re.search(r"fig(?:ure)?(\d+)", label, re.I):
        return int(m.group(1)), label
    if m := re.search(r"page(\d+)", label, re.I):
        return 128 + int(m.group(1)), label
    return 255, label


def add_preview(
    ref_df: pd.DataFrame,
    imgdir: Path,
    pdfdir: Path,
    make_previews: bool = True,
    write: bool = True,
) -> pd.DataFrame:
    imgext = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
    lookup_all: Dict[str, List[Path]] = defaultdict(list)
    if imgdir.is_dir():
        for p in sorted(imgdir.iterdir()):
            if p.suffix.lower() not in imgext:
                continue
            if "_" not in p.stem:
                warn(f"Ignoring preview '{p.name}': expected '<bibkey>_<label>.<ext>'.")
                continue
            lookup_all[p.stem.rsplit("_", 1)[0]].append(p)

    if make_previews:
        for _, row in ref_df[ref_df["local_pdf"].notnull()].iterrows():
            if row["ID"] not in lookup_all:
                lookup_all[row["ID"]].extend(
                    make_preview_from_pdf(
                        pdf_path=pdfdir / row["local_pdf"],
                        outprefix=imgdir / row["ID"],
                        npages=1,
                        dpi=72,
                        write=write,
                    )
                )

    previews = {k: sorted(v, key=_preview_rank)[0].name for k, v in lookup_all.items()}
    ref_df["preview"] = ref_df["ID"].map(previews)
    return ref_df


def add_google_scholar(
    ref_df: pd.DataFrame, gs_data: Dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Attach `google_scholar_id` (the pubid part of the `userid:pubid` key in
    citations.yml) by matching on DOI, title+year, title, then truncated title.
    Counts are NOT copied; the site reads them from citations.yml at build time.

    Returns (ref_df, gs_df) where gs_df has the per-paper match results."""
    papers = (gs_data or {}).get("papers") or {}
    ref_df = ensure_columns(ref_df, "doi", "title", "year")
    ref_df["google_scholar_id"] = None
    if not papers:
        warn("No Google Scholar data; skipping google_scholar_id.")
        return ref_df, pd.DataFrame()

    gs_df = pd.DataFrame(
        {
            "key": k,
            "gs_id": k.split(":", 1)[1] if ":" in k else k,
            "title": v.get("title") or "",
            "year": str(v.get("year", "")),
            "doi": norm_doi(v.get("doi")),
        }
        for k, v in papers.items()
    )
    gs_df["nt"] = gs_df["title"].map(normtitle)
    gs_df["nt_trunc"] = gs_df["nt"].str.slice(stop=64)
    gs_df["matched_to"] = None
    gs_df["match_on"] = None

    claimed: set = set()  # gs rows already assigned

    def find(row):
        doi = norm_doi(row["doi"])
        nt = normtitle(row["title"]) if isinstance(row["title"], str) else ""
        year = str(row["year"]) if pd.notnull(row["year"]) else ""
        avail = gs_df[~gs_df.index.isin(claimed)]
        rules = []
        if doi:
            rules.append(("doi", avail["doi"] == doi))
        if nt:
            rules.append(("title+year", (avail["nt"] == nt) & (avail["year"] == year)))
            rules.append(("title", avail["nt"] == nt))
            rules.append(("title_trunc", avail["nt_trunc"] == nt[:64]))
        for name, mask in rules:
            m = avail[mask]
            if len(m) == 1:
                return m.index[0], name
            if len(m) > 1:
                warn(f"{row['ID']}: {len(m)} Google Scholar candidates for '{name}'; trying next rule.")
        return None, None

    # Two passes so stronger rules win before weaker ones can claim an entry.
    results = {}
    for idx, row in ref_df.iterrows():
        gi, rule = find(row)
        if gi is not None and rule in ("doi", "title+year"):
            claimed.add(gi)
            results[idx] = (gi, rule)
    for idx, row in ref_df.iterrows():
        if idx in results:
            continue
        gi, rule = find(row)
        if gi is not None:
            claimed.add(gi)
            results[idx] = (gi, rule)

    for idx, (gi, rule) in results.items():
        ref_df.at[idx, "google_scholar_id"] = gs_df.at[gi, "gs_id"]
        gs_df.at[gi, "matched_to"] = ref_df.at[idx, "ID"]
        gs_df.at[gi, "match_on"] = rule
        if rule != "doi" and (d := norm_doi(ref_df.at[idx, "doi"])):
            gs_df.at[gi, "new_doi"] = d

    info("Google Scholar match results:")
    for rule, n in gs_df["match_on"].value_counts(dropna=False).items():
        info(f"  {n:4d}  {'UNMATCHED' if pd.isnull(rule) else rule}")
    unmatched_pp = ref_df[ref_df["google_scholar_id"].isnull()]
    if len(unmatched_pp):
        info("Paperpile entries without a Google Scholar match:")
        for _, r in unmatched_pp.iterrows():
            info(f"  {r['ID']}: {r['title']}")
    unmatched_gs = gs_df[gs_df["matched_to"].isnull()]
    if len(unmatched_gs):
        info("Google Scholar entries without a Paperpile match:")
        for _, r in unmatched_gs.iterrows():
            info(f"  {r['key']} ({r['year']}): {r['title']}")
    return ref_df, gs_df


def backfill_citation_dois(citations_yml: Path, gs_data: Dict[str, Any], gs_df: pd.DataFrame) -> None:
    """Write DOIs learned from the Paperpile match back to citations.yml (backup kept)."""
    if gs_df.empty or "new_doi" not in gs_df.columns:
        info("No DOIs to back-fill into citations.yml.")
        return
    changed = 0
    for _, r in gs_df[gs_df["new_doi"].notnull()].iterrows():
        entry = gs_data["papers"][r["key"]]
        if norm_doi(entry.get("doi")) != r["new_doi"]:
            entry["doi"] = r["new_doi"]
            changed += 1
    if not changed:
        info("citations.yml already has all matched DOIs.")
        return
    backup = citations_yml.with_name(citations_yml.name + ".bak")
    shutil.copy2(citations_yml, backup)
    with open(citations_yml, "w") as f:
        yaml.dump(gs_data, f, width=1000, sort_keys=True, allow_unicode=True)
    info(f"Back-filled {changed} DOIs into {citations_yml} (backup: {backup}).")


def remove_published_preprints(ref_df: pd.DataFrame, pubinfo: Dict[str, Any]) -> pd.DataFrame:
    """Drop preprints that have a published version; mark the published entry with
    `preprint_doi` and let it inherit `preview` / `google_scholar_id` if it has none."""
    ref_df = ensure_columns(ref_df, "doi", "preprint_doi", "preview", "google_scholar_id")
    dois = ref_df["doi"].map(norm_doi)

    for pair in pubinfo.get("preprint_published") or []:
        pre, pub = norm_doi(pair.get("preprint")), norm_doi(pair.get("published"))
        if not pre or not pub:
            warn(f"Malformed preprint_published entry in pubinfo: {pair}")
            continue
        pre_idx = ref_df.index[dois == pre]
        pub_idx = ref_df.index[dois == pub]
        if not len(pre_idx):
            warn(f"Preprint DOI '{pre}' not found in Paperpile bibliography.")
        if not len(pub_idx):
            warn(f"Published DOI '{pub}' not found in Paperpile bibliography.")
        if len(pub_idx):
            p = pub_idx[0]
            ref_df.at[p, "preprint_doi"] = pair["preprint"]
            if len(pre_idx):
                for col in ("preview", "google_scholar_id"):
                    if pd.isnull(ref_df.at[p, col]) and pd.notnull(ref_df.at[pre_idx[0], col]):
                        ref_df.at[p, col] = ref_df.at[pre_idx[0], col]
                        info(f"{ref_df.at[p, 'ID']}: inherited {col} from preprint {ref_df.at[pre_idx[0], 'ID']}.")
        # Only drop the preprint when its published version is present
        if len(pre_idx) and len(pub_idx):
            ref_df = ref_df.drop(index=pre_idx)
            dois = ref_df["doi"].map(norm_doi)
    return ref_df


def add_selected(ref_df: pd.DataFrame, selected: Dict[str, Any]) -> pd.DataFrame:
    ref_df = ensure_columns(ref_df, "doi")
    sel = {norm_doi(d.get("doi")) for d in (selected or {}).values() if d.get("doi")}
    found = set(ref_df["doi"].map(norm_doi)) & sel
    for missing in sorted(sel - found):
        warn(f"Selected paper with DOI '{missing}' not found in Paperpile bibliography.")
    ref_df["selected"] = ref_df["doi"].map(lambda d: "true" if norm_doi(d) in sel else None)
    return ref_df


def build_bibtex(
    paperpile_bib: Path,
    pubinfo_yml: Path,
    pdf_dir: Path,
    preview_dir: Path,
    citations_yml: Path | None,
    links_yml: Path | None = None,
    make_previews: bool = True,
    write_previews: bool = True,
    update_citations_yml: bool = False,
    add_altmetric: bool = True,
    add_dimensions: bool = True,
    display_source: str | None = None,
) -> str:
    """Run the whole pipeline and return the text of papers.bib."""
    pp_bibDB = bib_fromfile(paperpile_bib)
    if not pp_bibDB.entries:
        raise PipelineError(f"No entries found in {paperpile_bib}")
    pp_df = pd.DataFrame(pp_bibDB.entries)
    pubinfo = yaml_fromfile(pubinfo_yml)

    db = BibDatabase()
    db.comments = [
        f'This file is generated from "{display_source or paperpile_bib}" by `update_references.py`. Do not edit by hand.'
    ]

    pp_df = add_local_pdf(pp_df, pdf_dir)
    pp_df = add_preview(pp_df, preview_dir, pdf_dir, make_previews, write_previews)
    pp_df = pp_df.drop(columns=["local_pdf"])
    db.comments.append('Add "preview" field with preview images.')

    if citations_yml is not None:
        gs_data = yaml_fromfile(citations_yml)
        pp_df, gs_df = add_google_scholar(pp_df, gs_data)
        db.comments.append('Add "google_scholar_id" field from Google Scholar data.')
        if update_citations_yml:
            backfill_citation_dois(citations_yml, gs_data, gs_df)

    _n = len(pp_df)
    pp_df = remove_published_preprints(pp_df, pubinfo)
    if len(pp_df) < _n:
        db.comments.append('Link preprints to final articles when specified in "_data/pubinfo.yml".')

    if links_yml is not None:
        pp_df = add_links(pp_df, yaml_fromfile(links_yml))
        db.comments.append('Add "pdf" (or "html" for landing pages) field from "_data/oa_links.yml".')

    if add_altmetric:
        pp_df["altmetric"] = "true"
        db.comments.append('Add "altmetric" field.')
    if add_dimensions:
        pp_df["dimensions"] = "true"
        db.comments.append('Add "dimensions" field.')

    if pubinfo.get("selected_papers"):
        pp_df = add_selected(pp_df, pubinfo["selected_papers"])
        db.comments.append("Add selected papers.")

    db.entries = [to_entry(row) for _, row in pp_df.iterrows()]
    writer = BibTexWriter()
    writer.contents = ["comments", "entries"]
    writer.indent = "    "
    return writer.write(db)


def parse_args(argv=None) -> argparse.Namespace:
    def rel(p):
        return str(REPO_ROOT / p)

    ap = argparse.ArgumentParser(description="Generate papers.bib from a Paperpile export.")
    ap.add_argument("--paperpile", default=rel("_bibliography/paperpile.bib"), type=Path)
    ap.add_argument("--pubinfo", default=rel("_data/pubinfo.yml"), type=Path)
    ap.add_argument("--out", default=rel("_bibliography/papers.bib"), type=Path)
    ap.add_argument("--pdf-dir", default=os.environ.get("REFS_PDF_DIR") or rel("assets/pdf"), type=Path,
                    help="local PDFs used only to render previews (env: REFS_PDF_DIR); may not exist")
    ap.add_argument("--links", default=rel("_data/oa_links.yml"), type=Path,
                    help="public PDF link manifest (read only; written by bin/resolve_links.py)")
    ap.add_argument("--no-links", action="store_true", help="skip the pdf/html link fields")
    ap.add_argument("--preview-dir", default=rel("assets/img/publication_preview"), type=Path)
    ap.add_argument("--citations", default=rel("_data/citations.yml"), type=Path,
                    help="Google Scholar data (read only). Use --no-citations to skip.")
    ap.add_argument("--no-citations", action="store_true", help="skip the Google Scholar merge")
    ap.add_argument("--update-citations-yml", action="store_true",
                    help="back-fill matched DOIs into citations.yml (keeps a .bak copy)")
    ap.add_argument("--no-previews", action="store_true", help="do not generate previews from PDFs")
    ap.add_argument("--check", action="store_true",
                    help="write nothing; exit 1 if regenerating would change --out")
    ap.add_argument("--dry-run", action="store_true",
                    help="write nothing (no output, previews or citations.yml); print a diff summary")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    read_only = args.check or args.dry_run
    if read_only and args.update_citations_yml:
        raise PipelineError("--update-citations-yml cannot be combined with --check/--dry-run")

    try:
        display = Path(args.paperpile)
        try:
            display = display.resolve().relative_to(REPO_ROOT)
        except ValueError:
            pass
        text = build_bibtex(
            paperpile_bib=args.paperpile,
            pubinfo_yml=args.pubinfo,
            pdf_dir=args.pdf_dir,
            preview_dir=args.preview_dir,
            citations_yml=None if args.no_citations else args.citations,
            links_yml=None if args.no_links else args.links,
            make_previews=not args.no_previews,
            write_previews=not read_only,
            update_citations_yml=args.update_citations_yml,
            display_source=str(display),
        )
    except PipelineError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    current = args.out.read_text() if args.out.is_file() else ""
    if read_only:
        if text == current:
            info(f"{args.out} is up to date.")
            return 0
        diff = list(difflib.unified_diff(
            current.splitlines(), text.splitlines(), str(args.out), "regenerated", lineterm="", n=1))
        if args.dry_run:
            info(f"{args.out} would change ({len(diff)} diff lines).")
            print("\n".join(diff[:200]))
            return 0
        print("\n".join(diff[:200]))
        info(f"{args.out} is out of date; run bin/update_references.py.")
        return 1

    if text != current:
        args.out.write_text(text)
        info(f"Wrote {args.out}.")
    else:
        info(f"{args.out} unchanged.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PipelineError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
