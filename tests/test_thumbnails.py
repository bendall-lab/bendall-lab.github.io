"""Offline tests for bin/make_thumbnails.py and the preview lookup in update_references.py.

Run with: uv run --frozen --with pytest pytest tests
"""
import io
import sys
from pathlib import Path

import pandas as pd
import pymupdf
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import make_thumbnails as mt  # noqa: E402
import update_references as ur  # noqa: E402


def png_bytes(w=300, h=200, color=(200, 60, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


BODY = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 6


def make_pdf(path: Path, vector: bool = False, caption="Figure 1. A test figure caption.") -> Path:
    """Page 1: body text, a figure (raster or vector) and a caption beneath it."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(pymupdf.Rect(60, 80, 552, 160), BODY, fontsize=10)
    fig = pymupdf.Rect(100, 200, 500, 450)
    if vector:
        shape = page.new_shape()
        shape.draw_rect(pymupdf.Rect(100, 200, 300, 450))
        shape.draw_circle(pymupdf.Point(400, 325), 100)
        shape.finish(color=(0, 0, 1), fill=(0.8, 0.8, 1))
        shape.commit()
    else:
        page.insert_image(fig, stream=png_bytes(400, 250))
    page.insert_textbox(pymupdf.Rect(60, 460, 552, 500), caption, fontsize=9)
    page.insert_textbox(pymupdf.Rect(60, 520, 552, 700), BODY * 2, fontsize=10)
    doc.save(path)
    doc.close()
    return path


@pytest.mark.parametrize("text,ok", [
    ("Figure 1. A caption", True),
    ("Fig. 1 | Title", True),
    ("FIGURE 1\n(A) Workflow", True),
    ("Figure 1\n‘Sequence-discrete’ populations", True),
    ("Fig 1: Overview", True),
    ("Figure 1 shows the results of", False),
    ("Figure 1.2 Something in a thesis", False),
    ("Figure 10. Not the first", False),
    ("Figure 2. Second", False),
])
def test_caption_regex(text, ok):
    assert bool(mt.FIG1_CAPTION.match(text)) is ok


@pytest.mark.parametrize("vector", [False, True])
def test_find_figure1_bbox(tmp_path, vector):
    pdf = make_pdf(tmp_path / "p.pdf", vector=vector)
    with pymupdf.open(pdf) as doc:
        found = mt.find_figure1(doc)
    assert found is not None
    pno, clip = found
    assert pno == 0
    # clip covers the figure, stays above the caption (y=460) and below the body text (y=160)
    assert clip.x0 <= 100 and clip.x1 >= 300
    assert 160 < clip.y0 <= 205 and 445 <= clip.y1 < 460


def test_find_figure1_absent(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", caption="Table 1. Not a figure")
    with pymupdf.open(pdf) as doc:
        assert mt.find_figure1(doc) is None


def test_make_candidates_names_and_size(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf")
    out = tmp_path / "cands"
    cands = mt.make_candidates("Doe2024-ab", pdf, out)
    assert {"fig1", "page1"} <= set(cands)
    for kind in cands:
        p = out / f"Doe2024-ab_cand-{kind}.webp"
        assert p.is_file() and mt.CAND_RE.match(p.name)
        with Image.open(p) as im:
            assert im.format == "WEBP" and im.width <= mt.MAX_WIDTH
    assert cands["fig1"]["page"] == 1


def test_make_candidates_never_raises_on_bad_pdf(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert mt.make_candidates("Bad2024-xx", bad, tmp_path / "c") == {}


def test_promote(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf")
    out, prev = tmp_path / "cands", tmp_path / "prev"
    mt.make_candidates("Doe2024-ab", pdf, out)
    dest = mt.promote("Doe2024-ab", "fig1", None, out, prev)
    assert dest == prev / "Doe2024-ab_fig1.webp" and dest.is_file()
    with pytest.raises(ur.PipelineError):  # no silent overwrite
        mt.promote("Doe2024-ab", "fig1", None, out, prev)
    mt.promote("Doe2024-ab", "fig1", None, out, prev, force=True)
    assert mt.promote("Doe2024-ab", "page1", None, out, prev).name == "Doe2024-ab_page1.webp"
    with pytest.raises(ur.PipelineError):
        mt.promote("Doe2024-ab", "largest", None, out, prev)  # no such candidate
    with pytest.raises(ur.PipelineError):
        mt.promote("Doe2024-ab", "fig1", "bad_label", out, prev, force=True)  # '_' would break the naming


def test_promote_from_file(tmp_path):
    src = tmp_path / "abstract.png"
    src.write_bytes(png_bytes(1200, 500))
    dest = mt.promote("Doe2024-ab", str(src), "abstract", tmp_path / "c", tmp_path / "prev")
    assert dest.name == "Doe2024-ab_abstract.webp"
    with Image.open(dest) as im:
        assert im.width == mt.MAX_WIDTH


def test_contact_sheet_has_promote_commands(tmp_path):
    rows = [{"key": "Doe2024-ab", "title": "A <b>title</b>", "year": 2024, "curated": False,
             "cands": {"fig1": {"source": "pdf", "page": 1}, "page1": {"source": "pdf", "page": 1}}}]
    html = mt.contact_sheet(rows, tmp_path).read_text()
    assert "--promote Doe2024-ab fig1" in html and "Doe2024-ab_cand-page1.webp" in html
    assert "A &lt;b&gt;title&lt;/b&gt;" in html  # escaped


def write_bib(path: Path):
    path.write_text(
        "@article{Doe2024-ab,\n title={One},\n year={2024},\n doi={10.1/one},\n file={All Papers/x/one.pdf}\n}\n"
        "@article{Roe2023-cd,\n title={Two},\n year={2023},\n doi={10.1/two},\n file={All Papers/x/two.pdf}\n}\n"
        "@article{Poe2022-ef,\n title={Three},\n year={2022},\n doi={10.1/three},\n file={All Papers/x/missing.pdf}\n}\n"
    )


def test_main_only_missing_and_accept_auto(tmp_path):
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    make_pdf(pdfs / "one.pdf")
    make_pdf(pdfs / "two.pdf")
    bib, prev, out = tmp_path / "p.bib", tmp_path / "prev", tmp_path / "cands"
    write_bib(bib)
    prev.mkdir()
    (prev / "Roe2023-cd_Fig1.jpg").write_bytes(png_bytes())  # already curated
    base = ["--paperpile", str(bib), "--pubinfo", str(tmp_path / "none.yml"), "--manifest", str(tmp_path / "none.yml"),
            "--pdf-dir", str(pdfs), "--out-candidates", str(out), "--preview-dir", str(prev), "--no-network"]
    assert mt.main(base) == 0
    made = {p.name.split("_cand-")[0] for p in out.glob("*_cand-*.webp")}
    assert made == {"Doe2024-ab"}  # Roe curated, Poe has no PDF
    assert (out / "index.html").is_file() and (out / "candidates.yml").is_file()

    assert mt.main(base + ["--accept-auto"]) == 0
    assert (prev / "Doe2024-ab_fig1.webp").is_file()  # fig1 preferred
    assert not list(prev.glob("Roe2023-cd_*.webp"))  # curated entries untouched

    assert mt.main(base + ["--promote", "Doe2024-ab", "page1"]) == 0
    assert (prev / "Doe2024-ab_page1.webp").is_file()


JATS = """<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
<abstract abstract-type="graphical"><p><fig id="ga"><label>Graphical abstract</label>
<graphic xlink:href="ga.jpg"/></fig></p></abstract>
<permissions><license xlink:href="https://creativecommons.org/licenses/by/4.0/"><p>CC BY</p></license></permissions>
</article-meta></front><body>
<fig id="F1"><label>Figure 1</label><caption><title>Workflow</title></caption><graphic xlink:href="f1"/></fig>
<fig id="F2"><label>Figure 2</label><graphic xlink:href="f2.jpg"/></fig></body></article>"""


def test_jats_parse_and_pick():
    parsed = mt.parse_jats_figures(JATS)
    assert parsed["license"] == "cc-by-4.0"
    picked = mt.pick_epmc_figures(parsed)
    assert picked["abstract"]["href"] == "ga.jpg" and picked["epmc"]["href"] == "f1"
    assert mt.epmc_image_url("PMC1", "f1") == "https://europepmc.org/articles/PMC1/bin/f1.jpg"


def test_jats_without_cc_license_is_skipped():
    class Http:
        def get(self, url):
            return JATS.replace("creativecommons.org/licenses/by/4.0/", "example.com/tos").encode(), "text/xml"

    assert mt.fetch_epmc_candidates(Http(), "PMC1") == []


def test_pmc_blob_url():
    html = '<img src="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/d908/6786656/bea8/pcbi.g001.jpg">'
    assert mt.pmc_blob_url(html, "pcbi.g001") == "https://cdn.ncbi.nlm.nih.gov/pmc/blobs/d908/6786656/bea8/pcbi.g001.jpg"
    assert mt.pmc_blob_url(html, "other") is None


""" update_references: previews come from committed images only """


def test_preview_ranking_and_no_pdf_needed(tmp_path):
    prev = tmp_path / "prev"
    prev.mkdir()
    for n in ["A_page1.png", "A_Fig2.jpg", "A_fig1.webp", "B_page1.png", "B_abstract.webp", "B_fig1.jpg", "C_Fig3.png"]:
        (prev / n).write_bytes(b"x")
    (prev / "nounderscore.png").write_bytes(b"x")  # ignored with a warning
    df = pd.DataFrame({"ID": ["A", "B", "C", "D"]})
    out = ur.add_preview(df, prev)
    assert out["preview"].tolist()[:3] == ["A_fig1.webp", "B_abstract.webp", "C_Fig3.png"]
    assert pd.isnull(out["preview"].iloc[3])  # no image -> no preview; PDFs are never rendered
    assert not list(prev.glob("*cand*"))


def test_build_bibtex_needs_no_pdfs_and_warns(tmp_path, capsys):
    bib = tmp_path / "p.bib"
    write_bib(bib)
    (tmp_path / "prev").mkdir()
    (tmp_path / "prev" / "Doe2024-ab_fig1.webp").write_bytes(b"x")
    text = ur.build_bibtex(
        paperpile_bib=bib, pubinfo_yml=tmp_path / "none.yml", preview_dir=tmp_path / "prev", citations_yml=None
    )
    assert "preview = {Doe2024-ab_fig1.webp}" in text
    err = capsys.readouterr().err
    assert "no curated preview image (Roe2023-cd, Poe2022-ef)" in err
