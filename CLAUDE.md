# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## Repository-specific context

This is **Matthew Bendall's lab website** (bendall-lab.github.io), a fork of the [al-folio](https://github.com/alshedivat/al-folio) Jekyll theme. `AGENTS.md` (imported above) covers the generic al-folio build/format/deploy workflow — the notes below cover what's specific to this fork: the custom publication pipeline.

### Publication/bibliography pipeline

Unlike stock al-folio (which edits `_bibliography/papers.bib` by hand), this site generates `papers.bib` from a Paperpile export. Never hand-edit `_bibliography/papers.bib` — edit the inputs and regenerate.

- `_bibliography/paperpile.bib` — raw Paperpile export of the `_My Publications` folder (source of truth for reference data; planned: Paperpile's GitHub integration pushes it automatically).
- `bin/update_references.py` — **the current generator.** Reads `paperpile.bib` + `_data/pubinfo.yml` (`selected_papers`, `preprint_published` DOI pairs), collapses preprints into their published versions, adds `pdf`/`html` (from `_data/oa_links.yml`), `preview`, `altmetric`, `dimensions`, `selected`, and `google_scholar_id` (matched from `_data/citations.yml`; citation _counts_ stay in `citations.yml` and are read by `_layouts/bib.liquid` at build time), and writes `papers.bib`. Output is deterministic and makes no network calls (the manifest is read, never fetched). Flags: `--check` (exit 1 if `papers.bib` is stale), `--dry-run` (print diff only), `--no-previews`, `--no-citations`, `--update-citations-yml` (back-fill DOIs, keeps a `.bak`), plus `--paperpile/--pubinfo/--out/--pdf-dir/--preview-dir/--citations/--links` path overrides and `--no-links`.
- `bin/resolve_links.py` — **the only network step for PDF links.** For each entry with a DOI it records a public link in `_data/oa_links.yml` (keyed by bibkey; fields `pdf`, `kind`, `source`, `license`, `pmcid`, `is_oa`, `doi`, `resolved`). Order: `pdf_overrides` in `pubinfo.yml` → Unpaywall direct PDF → Europe PMC open-access PDF (also records the PMCID) → `{pdf_bucket_base}/{bibkey}.pdf` (only for bibkeys listed under `pdf_bucket` in `pubinfo.yml`, and only if `pdf_bucket_base` is set) → `https://doi.org/<doi>` (`kind: landing`). `update_references.py` emits `kind` pdf/override/bucket as the `pdf` field (PDF button) and `landing` as `html` (HTML button), so a landing page is never labelled "PDF". By default existing pdf/bucket records are kept and landing records are retried (new papers often aren't indexed yet); `--refresh` re-resolves everything, `--bibkey KEY` restricts, `--dry-run` prints. Failures only warn. Entries with no DOI (thesis) need an override or bucket entry. Tests: `uv run --frozen --with pytest pytest tests`.
- `ref.py` (repo root) — legacy earlier iteration of the same pipeline; don't edit it.
- `bin/update_scholar_citations.py` — refreshes citation counts in `_data/citations.yml` from Google Scholar (run on a schedule by `update-citations.yml`).
- Preview images are looked up in `assets/img/publication_preview/` by the naming convention `<bibkey>_<label>.<ext>` (e.g. `Fei2026-zj_Fig3.webp`); if none exists, page 1 of the PDF is rendered.
- PDFs are **not** kept in the repo (`assets/pdf/` is git-ignored except `example_pdf.pdf`). Local copies (point `--pdf-dir` / `$REFS_PDF_DIR` at the Paperpile-synced folder) are used only to render preview images; the public `pdf` links come from `_data/oa_links.yml`.

Environment: uv (`pyproject.toml` + `uv.lock`, Python 3.13, `bibtexparser<2` — the scripts use the v1 API). Run `uv sync`, then `uv run python bin/update_references.py [--check]`. `reftools/environment.yml` is a conda/mamba equivalent. The root `requirements.txt` is a separate, smaller pip environment (`nbconvert`, `pyyaml`, `rendercv[full]`, `scholarly`) used by CI (`render-cv.yml`, notebook rendering) — not the bib pipeline's environment.

Planned (not yet implemented): a `--candidates` step that extracts thumbnail candidates from PDFs plus a contact sheet for picking; a GitHub Action that regenerates `papers.bib` when `paperpile.bib` or `citations.yml` changes and dispatches the deploy (the citation workflow will also dispatch `deploy.yml`, since pushes by `GITHUB_TOKEN` don't trigger it); and a local git hook.

### Data files

`_data/` holds several citation/publication YAML files beyond the theme defaults: `citations.yml` (Google Scholar counts), `oa_links.yml` (public PDF links, written by `resolve_links.py`) and `pubinfo.yml` (`selected_papers`, `preprint_published`, optional `pdf_overrides` / `pdf_bucket_base` / `pdf_bucket`). `thumbnails.yml` and the `thumbnails` section of `pubinfo.yml` are dead (previews now use the filename convention above). Files such as `citations1.yml`, `original_citations.yml`, `test_citations.yml` are working/backup copies from bibliography rework (git-ignored), not files the theme reads. Check `_layouts/`/`_includes/` for actual `site.data.*` references before assuming a data file is live.

### CV

The CV (`_pages/cv.md` / `_data/cv.yml`) is rendered via `rendercv` (see `render-cv.yml` workflow and recent commit "chore: render the latest CV") — regenerate through that workflow/tool rather than editing rendered output by hand.
