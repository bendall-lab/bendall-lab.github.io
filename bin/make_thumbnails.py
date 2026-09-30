#! /usr/bin/env python
"""Make thumbnail *candidates* for publications, and promote the ones you pick.

Workflow:
    1. uv run bin/make_thumbnails.py --pdf-dir <folder with the PDFs> [--open]
         writes `<bibkey>_cand-<kind>.webp` + `index.html` (a contact sheet) into
         _candidates/thumbnails/ (git-ignored) for entries with no curated image yet.
    2. Look at the sheet and pick one image per paper:
         uv run bin/make_thumbnails.py --promote <bibkey> <kind> [label]
       which writes assets/img/publication_preview/<bibkey>_<label>.webp (commit this).
    3. bin/update_references.py reads only the committed images; it never touches PDFs.

Candidate kinds:
    fig1     crop of Figure 1 found in the PDF (caption heuristic, images + vector drawings)
    abstract graphical abstract from the Europe PMC full text (open-access, CC-licensed only)
    epmc     Figure 1 from the Europe PMC full text (same conditions)
    largest  largest embedded raster on pages 1-3 (logos/repeated images ignored)
    page1    first page of the PDF

Europe PMC figures are opt-in (--epmc). The full-text XML is available to scripts, but at the
time of writing the image hosts (europepmc.org, pmc.ncbi.nlm.nih.gov) answer Python clients
with HTTP 403, so the images usually can't be fetched. A graphical abstract you download
from the article page can be promoted from a file instead:
    uv run bin/make_thumbnails.py --promote <bibkey> path/to/image.png abstract
Network calls are bounded by short timeouts.
"""
import argparse
import html
import io
import os
import re
import subprocess
import sys
import time
import webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_references import (  # noqa: E402
    REPO_ROOT,
    PipelineError,
    bib_fromfile,
    info,
    norm_doi,
    warn,
    yaml_fromfile,
)

MAX_WIDTH = 600
WEBP_QUALITY = 80
CAND_RE = re.compile(r"^(?P<key>.+)_cand-(?P<kind>[a-z0-9]+)\.webp$")
KINDS = ("fig1", "abstract", "epmc", "largest", "page1")
# Default curated label for each candidate kind (labels are ranked by update_references.py:
# abstract, then figN, then pageN).
DEFAULT_LABEL = {"fig1": "fig1", "epmc": "fig1", "largest": "fig1", "abstract": "abstract", "page1": "page1"}
FIG_SEARCH_PAGES = 12
FIG_TIME_BUDGET = 30.0  # seconds per paper for the Figure 1 search

# "Fig. 1", "Figure 1", "FIG 1 |" ... at the start of a block; not "Figure 1 shows", "Figure 1.2"
FIG1_CAPTION = re.compile(
    r"^\s*(?i:fig(?:ure|\.)?)\s*1(?!\d|\.\d)(?:\s*[.:|–—\-]|\s+[A-Z(\[“‘\"']|\s*$)"
)


""" Images """


def save_webp(img: Image.Image, out: Path, max_width: int = MAX_WIDTH, quality: int = WEBP_QUALITY) -> Path:
    img = img.convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, max(1, round(img.height * max_width / img.width))), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "WEBP", quality=quality, method=6)
    return out


def render_clip(page, clip=None, dpi: int = 150) -> Image.Image:
    import pymupdf

    pix = page.get_pixmap(dpi=dpi, clip=clip, colorspace=pymupdf.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


""" PDF heuristics """


def _union(a, b):
    """Union of two rects by min/max (pymupdf's `|` mishandles zero-width lines)."""
    import pymupdf

    return pymupdf.Rect(min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1))


def _pad(r, d):
    import pymupdf

    return pymupdf.Rect(r.x0 - d, r.y0 - d, r.x1 + d, r.y1 + d)


def _clamp(r, bounds):
    import pymupdf

    return pymupdf.Rect(max(r.x0, bounds.x0), max(r.y0, bounds.y0), min(r.x1, bounds.x1), min(r.y1, bounds.y1))


def _overlaps(a, b) -> bool:
    return a.x0 <= b.x1 and b.x0 <= a.x1 and a.y0 <= b.y1 and b.y0 <= a.y1


def _in_furniture_band(r, page_rect, frac: float = 0.09) -> bool:
    """Entirely inside the header or footer band of the page."""
    return r.y1 < page_rect.y0 + frac * page_rect.height or r.y0 > page_rect.y1 - frac * page_rect.height


def _graphic_rects(page) -> List[Any]:
    """Bounding boxes of images and vector drawings, without page furniture."""
    import pymupdf

    pr = page.rect
    rects = []
    for im in page.get_image_info():
        r = pymupdf.Rect(im["bbox"])
        if r.width > 4 and r.height > 4 and not _in_furniture_band(r, pr):
            rects.append(r)
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"])
        if r.is_empty and r.width < 0.01 and r.height < 0.01:
            continue
        if _in_furniture_band(r, pr):  # running headers/footers, journal logos, rules
            continue
        if r.width * r.height > 0.8 * pr.width * pr.height:  # page background
            continue
        rects.append(r)
    return rects


def _short_text_blocks(page, caption_rect) -> List[Any]:
    """Short text blocks (panel letters, axis labels) that may belong to a figure."""
    import pymupdf

    out = []
    for b in page.get_text("blocks"):
        if b[6] != 0:
            continue
        r = pymupdf.Rect(b[:4])
        if len(b[4].strip()) <= 60 and not _overlaps(r, caption_rect) and not _in_furniture_band(r, page.rect):
            out.append(r)
    return out


def _grow_above(rects, cap, gap: float = 20.0, max_seed_dist: float = 60.0, page_width: float = 0.0):
    """Union of graphics stacked directly above `cap`. Returns a Rect or None.

    With a narrow (single-column) caption, thin lines outside the caption's column are
    ignored, so table rules in the neighbouring column can't stretch the crop."""
    import pymupdf

    above = [r for r in rects if r.y1 <= cap.y0 + 3]
    seeds = [r for r in above if r.x1 >= cap.x0 - 5 and r.x0 <= cap.x1 + 5 and min(r.width, r.height) >= 2]
    if not seeds:
        return None
    seed_bottom = max(r.y1 for r in seeds)
    if cap.y0 - seed_bottom > max_seed_dist:
        return None
    above.sort(key=lambda r: -r.y1)
    narrow = bool(page_width) and cap.width < 0.6 * page_width
    xgap = 4.0 if narrow else gap  # don't reach across the gutter into the next column
    union = None
    ux0, ux1, top = cap.x0, cap.x1, seed_bottom
    taken = set()
    for _ in range(3):
        grew = False
        for i, r in enumerate(above):
            if i in taken:
                continue
            if not (r.y1 >= top - gap and r.x1 >= ux0 - xgap and r.x0 <= ux1 + xgap):
                continue
            if narrow and min(r.width, r.height) < 2 and (r.x0 < cap.x0 - gap or r.x1 > cap.x1 + gap):
                continue
            taken.add(i)
            union = pymupdf.Rect(r) if union is None else _union(union, r)
            ux0, ux1, top = min(ux0, union.x0), max(ux1, union.x1), min(top, union.y0)
            grew = True
        if not grew:
            break
    return union


def _grow_below(rects, cap, gap: float = 20.0, max_seed_dist: float = 60.0, page_width: float = 0.0):
    """Same as _grow_above for captions that sit above their figure (mirror image)."""
    import pymupdf

    mirrored = [pymupdf.Rect(r.x0, -r.y1, r.x1, -r.y0) for r in rects]
    mcap = pymupdf.Rect(cap.x0, -cap.y1, cap.x1, -cap.y0)
    u = _grow_above(mirrored, mcap, gap, max_seed_dist, page_width)
    return None if u is None else pymupdf.Rect(u.x0, -u.y1, u.x1, -u.y0)


def _grow_beside(rects, cap, gap: float = 15.0, max_items: int = 1500):
    """Figure next to a side caption: the largest solid graphic level with the caption,
    plus everything touching it."""
    solid = [r for r in rects if min(r.width, r.height) >= 2]
    level = [r for r in solid
             if (r.x0 >= cap.x1 - 5 or r.x1 <= cap.x0 + 5) and min(r.y1, cap.y1) - max(r.y0, cap.y0) > 0.3 * cap.height]
    if not level or len(rects) > max_items:
        return None
    union = max(level, key=lambda r: r.width * r.height)
    union = _union(union, union)
    grew = True
    while grew:
        grew = False
        for r in rects:
            if _overlaps(r, _pad(union, gap)) and not (union.x0 <= r.x0 and r.x1 <= union.x1 and union.y0 <= r.y0 and r.y1 <= union.y1):
                union = _union(union, r)
                grew = True
    return union


def _grow_by_whitespace(page, cap, min_frac: float = 0.15):
    """Last resort (figure drawn in a way pymupdf doesn't report): the band between the
    nearest body-text paragraph above the caption and the caption, if it isn't blank."""
    import pymupdf

    pr = page.rect
    top = pr.y0 + 0.09 * pr.height
    for b in page.get_text("blocks"):
        r = pymupdf.Rect(b[:4])
        if b[6] == 0 and len(b[4].strip()) > 150 and r.y1 <= cap.y0 and r.x1 >= cap.x0 and r.x0 <= cap.x1:
            top = max(top, r.y1)
    region = pymupdf.Rect(min(cap.x0, pr.x0 + 30), top + 2, max(cap.x1, pr.x1 - 30), cap.y0 - 2)
    if region.height < min_frac * pr.height:
        return None
    small = render_clip(page, region, dpi=30).convert("L")
    dark = sum(1 for v in small.getdata() if v < 235)
    return region if dark > 0.04 * small.width * small.height else None


def _sane_region(r, page_rect) -> bool:
    if r is None:
        return False
    if r.width < 0.2 * page_rect.width or r.height < 0.06 * page_rect.height:
        return False
    return r.width * r.height <= 0.9 * page_rect.width * page_rect.height


def find_figure1(doc, max_pages: int = FIG_SEARCH_PAGES, budget: float = FIG_TIME_BUDGET):
    """Locate Figure 1. Returns (page_index, clip Rect) or None."""
    import pymupdf

    start = time.monotonic()
    for pno in range(min(max_pages, doc.page_count)):
        if time.monotonic() - start > budget:
            warn(f"Figure 1 search timed out after page {pno}")
            return None
        page = doc[pno]
        captions = [
            pymupdf.Rect(b[:4]) for b in page.get_text("blocks") if b[6] == 0 and FIG1_CAPTION.match(b[4])
        ]
        if not captions:
            continue
        rects = _graphic_rects(page)
        for cap in captions:
            for grow in (_grow_above, _grow_below, _grow_beside, _grow_by_whitespace):
                if grow is _grow_beside:
                    region = grow(rects, cap)
                elif grow is _grow_by_whitespace:
                    region = grow(page, cap)
                else:
                    region = grow(rects, cap, page_width=page.rect.width)
                if not _sane_region(region, page.rect):
                    continue
                for t in _short_text_blocks(page, cap):  # panel letters, axis labels
                    if _overlaps(t, _pad(region, 14)):
                        region = _union(region, t)
                clip = _clamp(_pad(region, 6), page.rect)
                return pno, clip
    return None


def find_largest_image(doc, max_pages: int = 3):
    """Largest non-tiny, non-repeated embedded raster on the first pages.
    Returns (page_index, bbox Rect) or None."""
    import pymupdf

    seen: Dict[int, int] = {}
    found: List[Tuple[float, int, Any, int]] = []
    for pno in range(min(max_pages, doc.page_count)):
        page = doc[pno]
        pr = page.rect
        for im in page.get_image_info(xrefs=True):
            xref = im.get("xref") or 0
            r = pymupdf.Rect(im["bbox"])
            seen[xref] = seen.get(xref, 0) + 1
            if r.width < 120 or r.height < 90 or im.get("width", 0) < 200 or im.get("height", 0) < 150:
                continue
            if r.width * r.height > 0.85 * pr.width * pr.height:  # scanned page
                continue
            found.append((r.width * r.height, pno, r, xref))
    found = [f for f in found if not (f[3] and seen.get(f[3], 0) > 1)]  # repeated logos/headers
    if not found:
        return None
    _, pno, r, _ = max(found, key=lambda f: f[0])
    return pno, r


def iou(a, b) -> float:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    if w <= 0 or h <= 0:
        return 0.0
    ia = w * h
    return ia / (a.width * a.height + b.width * b.height - ia)


""" Europe PMC (open-access full text) """


class Http:
    def __init__(self, timeout: float = 20.0, pause: float = 0.4, max_bytes: int = 15_000_000):
        self.timeout, self.pause, self.max_bytes = timeout, pause, max_bytes
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "bendall-lab-thumbnails/1.0 (mailto:bendall@gwu.edu)"

    def get(self, url: str) -> Optional[Tuple[bytes, str]]:
        """(content, content-type) or None. One retry, bounded size and time."""
        for attempt in range(2):
            try:
                time.sleep(self.pause)
                r = self.session.get(url, timeout=self.timeout, stream=True)
                if r.status_code != 200:
                    warn(f"HTTP {r.status_code} for {url}")
                    return None
                buf = io.BytesIO()
                for chunk in r.iter_content(65536):
                    buf.write(chunk)
                    if buf.tell() > self.max_bytes:
                        warn(f"{url}: response larger than {self.max_bytes} bytes, skipped")
                        return None
                return buf.getvalue(), r.headers.get("content-type", "")
            except requests.RequestException as e:
                warn(f"{url}: {e}" + (" (retrying)" if attempt == 0 else ""))
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def parse_jats_figures(xml_text: str) -> Dict[str, Any]:
    """Return {'license': str|None, 'figures': [{label, caption, href, graphical}]}."""
    root = ET.fromstring(xml_text)
    parent = {c: p for p in root.iter() for c in p}
    lic = None
    for el in root.iter():
        if _local(el.tag) == "license":
            href = next((v for k, v in el.attrib.items() if _local(k) == "href"), "") or ""
            ltype = el.attrib.get("license-type", "")
            m = re.search(r"creativecommons\.org/(?:licenses|publicdomain)/([a-z\-]+)/([\d.]+)", href)
            if m:
                lic = f"cc-{m.group(1)}-{m.group(2)}"
            elif ltype.lower().startswith("cc") or "creative commons" in _text(el).lower():
                lic = ltype.lower() or "cc"
            if lic:
                break
    figs = []
    for el in root.iter():
        if _local(el.tag) != "fig":
            continue
        label = _text(next((c for c in el if _local(c.tag) == "label"), None))
        caption = _text(next((c for c in el if _local(c.tag) == "caption"), None))
        href = None
        for g in el.iter():
            if _local(g.tag) == "graphic":
                href = next((v for k, v in g.attrib.items() if _local(k) == "href"), None)
                if href:
                    break
        graphical, p = False, parent.get(el)
        while p is not None:
            if _local(p.tag) == "abstract" and "graphical" in (p.attrib.get("abstract-type", "")).lower():
                graphical = True
            p = parent.get(p)
        if re.search(r"graphical abstract", f"{label} {caption[:80]}", re.I):
            graphical = True
        if href:
            figs.append({"label": label, "caption": caption, "href": href, "graphical": graphical})
    return {"license": lic, "figures": figs}


def pick_epmc_figures(parsed: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """{'abstract': fig, 'epmc': fig1} for whichever exist."""
    out = {}
    for f in parsed["figures"]:
        if f["graphical"] and "abstract" not in out:
            out["abstract"] = f
        elif re.match(r"^(?i:fig(?:ure|\.)?)\s*1$", f["label"].strip().rstrip(".")) and "epmc" not in out:
            out["epmc"] = f
    return out


def epmc_image_url(pmcid: str, href: str) -> str:
    if not re.search(r"\.(jpe?g|png|gif|tiff?)$", href, re.I):
        href += ".jpg"
    return f"https://europepmc.org/articles/{pmcid}/bin/{href}"


def pmc_blob_url(page_html: str, href: str) -> Optional[str]:
    """CDN URL of a figure image as linked from the PMC article page (europepmc.org's own
    image path blocks scripted access)."""
    stem = re.sub(r"\.(jpe?g|png|gif|tiff?)$", "", href, flags=re.I)
    m = re.search(r"https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[^\"'\s]*/" + re.escape(stem) + r"\.(?:jpe?g|png|gif)", page_html)
    return m.group(0) if m else None


def fetch_epmc_candidates(http, pmcid: str) -> List[Dict[str, Any]]:
    """[{kind, image, license, caption, url}] from the open-access full text; CC licences only."""
    got = http.get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML")
    if not got:
        return []
    try:
        parsed = parse_jats_figures(got[0].decode("utf-8", "replace"))
    except ET.ParseError as e:
        warn(f"{pmcid}: could not parse full text XML ({e})")
        return []
    if not parsed["license"]:
        warn(f"{pmcid}: no Creative Commons license found in full text; skipping Europe PMC figures")
        return []
    out = []
    figs = pick_epmc_figures(parsed)
    page_html = ""
    if figs:
        page = http.get(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/")
        page_html = page[0].decode("utf-8", "replace") if page else ""
    for kind, fig in figs.items():
        url = pmc_blob_url(page_html, fig["href"]) or epmc_image_url(pmcid, fig["href"])
        img = http.get(url)
        if not img or not img[1].lower().startswith("image/"):
            continue
        try:
            out.append({"kind": kind, "image": Image.open(io.BytesIO(img[0])), "license": parsed["license"],
                        "caption": fig["caption"], "url": url})
        except Exception as e:  # noqa: BLE001 - corrupt image, keep going
            warn(f"{url}: not a readable image ({e})")
    return out


""" Per-paper candidates """


def find_pdf(entry: Dict[str, Any], pdf_dir: Path) -> Optional[Path]:
    """PDF named in Paperpile's `file` field (basename) inside pdf_dir."""
    for part in str(entry.get("file", "")).split(";"):
        name = Path(part.strip()).name
        if name and (pdf_dir / name).is_file():
            return pdf_dir / name
    return None


def cand_path(outdir: Path, bibkey: str, kind: str) -> Path:
    return outdir / f"{bibkey}_cand-{kind}.webp"


def make_candidates(
    bibkey: str,
    pdf: Path,
    outdir: Path,
    pmcid: Optional[str] = None,
    http=None,
    force: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Write candidate images for one paper; returns {kind: metadata}. Never raises."""
    import pymupdf

    cands: Dict[str, Dict[str, Any]] = {}
    try:
        doc = pymupdf.open(pdf)
    except Exception as e:  # noqa: BLE001
        warn(f"{bibkey}: cannot open {pdf.name}: {e}")
        return cands

    def fresh(kind):
        return force or not cand_path(outdir, bibkey, kind).exists()

    with doc:
        fig_bbox = fig_page = None
        try:
            found = find_figure1(doc)
            if found:
                fig_page, clip = found
                fig_bbox = clip
                if fresh("fig1"):
                    save_webp(render_clip(doc[fig_page], clip), cand_path(outdir, bibkey, "fig1"))
                cands["fig1"] = {"source": "pdf", "page": fig_page + 1}
            else:
                info(f"{bibkey}: no Figure 1 found")
        except Exception as e:  # noqa: BLE001
            warn(f"{bibkey}: Figure 1 search failed: {e}")

        try:
            found = find_largest_image(doc)
            if found:
                pno, bbox = found
                if fig_bbox is not None and pno == fig_page and iou(bbox, fig_bbox) > 0.7:
                    info(f"{bibkey}: largest image duplicates the Figure 1 crop; skipped")
                else:
                    if fresh("largest"):
                        save_webp(render_clip(doc[pno], _clamp(_pad(bbox, 2), doc[pno].rect)),
                                  cand_path(outdir, bibkey, "largest"))
                    cands["largest"] = {"source": "pdf", "page": pno + 1}
        except Exception as e:  # noqa: BLE001
            warn(f"{bibkey}: largest-image search failed: {e}")

        try:
            if fresh("page1"):
                save_webp(render_clip(doc[0], dpi=100), cand_path(outdir, bibkey, "page1"))
            cands["page1"] = {"source": "pdf", "page": 1}
        except Exception as e:  # noqa: BLE001
            warn(f"{bibkey}: page 1 render failed: {e}")

    if http is not None and pmcid:
        if not (fresh("abstract") and fresh("epmc")):
            for k in ("abstract", "epmc"):
                if cand_path(outdir, bibkey, k).exists():
                    cands[k] = {"source": "europepmc", "pmcid": pmcid}
        else:
            try:
                for c in fetch_epmc_candidates(http, pmcid):
                    save_webp(c["image"], cand_path(outdir, bibkey, c["kind"]))
                    cands[c["kind"]] = {"source": "europepmc", "pmcid": pmcid, "license": c["license"],
                                        "url": c["url"], "caption": c["caption"][:200]}
            except Exception as e:  # noqa: BLE001
                warn(f"{bibkey}: Europe PMC figures failed: {e}")
    return cands


""" Curated images, promote """


def curated_keys(preview_dir: Path) -> set:
    keys = set()
    if preview_dir.is_dir():
        for p in preview_dir.iterdir():
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"} and "_" in p.stem:
                keys.add(p.stem.rsplit("_", 1)[0])
    return keys


def promote(bibkey: str, kind: str, label: Optional[str], outdir: Path, preview_dir: Path, force: bool = False) -> Path:
    """Write the candidate `kind` (or, if `kind` is an image file, that file) as
    <preview_dir>/<bibkey>_<label>.webp, downscaled."""
    if Path(kind).is_file():
        src, label = Path(kind), label or "fig1"
    else:
        src = cand_path(outdir, bibkey, kind)
        if not src.is_file():
            raise PipelineError(f"No candidate {src.name} in {outdir}; run make_thumbnails.py first.")
    label = label or DEFAULT_LABEL.get(kind, kind)
    if "_" in label or not re.fullmatch(r"[A-Za-z0-9-]+", label):
        raise PipelineError(f"Invalid label '{label}': use letters, digits and dashes only.")
    dest = preview_dir / f"{bibkey}_{label}.webp"
    if dest.exists() and not force:
        raise PipelineError(f"{dest} already exists (use --force to overwrite).")
    with Image.open(src) as im:
        save_webp(im, dest)
    info(f"Promoted {src.name} -> {dest}")
    return dest


""" Contact sheet """

SHEET_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--card:#f5f5f7;--line:#d2d2d7;--acc:#0a66c2}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f4;--mut:#9a9aa1;--card:#232326;--line:#3a3a3f;--acc:#6cb4ff}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
h1{font-size:1.25rem;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 20px}
.paper{border-top:1px solid var(--line);padding:16px 0}
.paper h2{font-size:1rem;margin:0}.meta{color:var(--mut);font-size:.85rem;margin:2px 0 10px}
.badge{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:0 8px;font-size:.75rem;margin-left:6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px}
.card img{width:100%;height:auto;display:block;border-radius:4px;background:#fff}
.kind{font-weight:600;margin:6px 0 2px}.src{color:var(--mut);font-size:.75rem}
code{display:block;font-size:.72rem;word-break:break-all;margin:6px 0;color:var(--fg)}
button{font:inherit;font-size:.78rem;color:var(--acc);background:none;border:1px solid var(--line);border-radius:6px;padding:2px 8px;cursor:pointer}
"""


def contact_sheet(rows: List[Dict[str, Any]], outdir: Path) -> Path:
    parts = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Thumbnail candidates</title><style>{SHEET_CSS}</style>",
             f"<h1>Thumbnail candidates</h1><p class=sub>{len(rows)} papers. Pick one per paper and run the command "
             f"shown (or press Copy). Promoted images go to assets/img/publication_preview/.</p>"]
    for row in rows:
        key = row["key"]
        badge = "<span class=badge>curated</span>" if row.get("curated") else ""
        parts.append(f"<section class=paper><h2>{html.escape(row['title'])}{badge}</h2>"
                     f"<div class=meta>{html.escape(key)} &middot; {html.escape(str(row['year']))}</div><div class=grid>")
        for kind in KINDS:
            c = row["cands"].get(kind)
            if not c:
                continue
            cmd = f"uv run bin/make_thumbnails.py --promote {key} {kind}"
            src = c["source"] + (f", {c['license']}" if c.get("license") else "") + (f", p.{c['page']}" if c.get("page") else "")
            img = html.escape(cand_path(Path(), key, kind).name)
            parts.append(f"<div class=card><a href='{img}'><img loading=lazy src='{img}' alt='{html.escape(kind)}'></a>"
                         f"<div class=kind>{kind}</div><div class=src>{html.escape(src)}</div>"
                         f"<code>{html.escape(cmd)}</code>"
                         f"<button onclick=\"navigator.clipboard.writeText(this.previousElementSibling.textContent)\">Copy</button></div>")
        parts.append("</div></section>")
    path = outdir / "index.html"
    outdir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


""" CLI """


def parse_args(argv=None) -> argparse.Namespace:
    def rel(p):
        return str(REPO_ROOT / p)

    ap = argparse.ArgumentParser(description="Make thumbnail candidates from local PDFs; promote the ones you pick.")
    ap.add_argument("--paperpile", default=rel("_bibliography/paperpile.bib"), type=Path)
    ap.add_argument("--pubinfo", default=rel("_data/pubinfo.yml"), type=Path)
    ap.add_argument("--pdf-dir", default=os.environ.get("REFS_PDF_DIR") or rel("assets/pdf"), type=Path,
                    help="folder with the PDFs named as in Paperpile's `file` field (env: REFS_PDF_DIR)")
    ap.add_argument("--manifest", default=rel("_data/oa_links.yml"), type=Path,
                    help="oa_links.yml; its pmcid values enable Europe PMC figures")
    ap.add_argument("--out-candidates", default=rel("_candidates/thumbnails"), type=Path)
    ap.add_argument("--preview-dir", default=rel("assets/img/publication_preview"), type=Path)
    ap.add_argument("--bibkey", action="append", help="only this bibkey (repeatable)")
    ap.add_argument("--only-missing", action=argparse.BooleanOptionalAction, default=True,
                    help="only entries with no curated image yet (default; --no-only-missing for all)")
    ap.add_argument("--max", type=int, default=None, help="process at most N papers")
    ap.add_argument("--epmc", action="store_true",
                    help="also try Europe PMC figures (CC-licensed OA papers with a pmcid in the manifest); "
                         "see module docstring: image hosts often return 403")
    ap.add_argument("--no-network", action="store_true", help="never use the network (overrides --epmc)")
    ap.add_argument("--force", action="store_true", help="regenerate existing candidates / overwrite curated files")
    ap.add_argument("--open", action="store_true", help="open the contact sheet in the browser")
    ap.add_argument("--promote", nargs="+", metavar=("BIBKEY", "KIND|FILE"),
                    help="BIBKEY KIND [LABEL] (or BIBKEY FILE [LABEL]): write the candidate (or your own "
                         "image file) to the curated location and exit")
    ap.add_argument("--accept-auto", nargs="?", const="fig1", metavar="KIND",
                    help="after generating, promote KIND (default fig1, falling back to page1) for every paper "
                         "without a curated image")
    return ap.parse_args(argv)


def open_sheet(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False, timeout=10)
        else:
            webbrowser.open(path.as_uri())
    except Exception as e:  # noqa: BLE001
        warn(f"could not open {path}: {e}")


def main(argv=None, http=None) -> int:
    args = parse_args(argv)
    try:
        if args.promote:
            if not 2 <= len(args.promote) <= 3:
                raise PipelineError("--promote takes BIBKEY KIND [LABEL]")
            promote(args.promote[0], args.promote[1], args.promote[2] if len(args.promote) == 3 else None,
                    args.out_candidates, args.preview_dir, args.force)
            return 0

        entries = bib_fromfile(args.paperpile).entries
        if not entries:
            raise PipelineError(f"No entries found in {args.paperpile}")
        pubinfo = yaml_fromfile(args.pubinfo)
        manifest = yaml_fromfile(args.manifest)
    except PipelineError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    preprints = {norm_doi(p.get("preprint")) for p in pubinfo.get("preprint_published", []) or [] if p.get("preprint")}
    curated = curated_keys(args.preview_dir)
    if args.no_network or not (args.epmc or http is not None):
        http = None
    elif http is None:
        http = Http()

    todo, no_pdf = [], []
    for e in entries:
        key = e["ID"]
        if args.bibkey and key not in args.bibkey:
            continue
        if norm_doi(e.get("doi")) in preprints:
            continue  # replaced by its published version
        if args.only_missing and key in curated and not args.bibkey:
            continue
        pdf = find_pdf(e, args.pdf_dir)
        if pdf is None:
            no_pdf.append(key)
            continue
        todo.append((e, pdf))
    if no_pdf:
        warn(f"{len(no_pdf)} entries skipped, no local PDF in {args.pdf_dir}: {', '.join(no_pdf)}")
    if args.max is not None:
        todo = todo[: args.max]
    if not todo:
        info("Nothing to do.")
        return 0

    rows, failed = [], []
    for i, (e, pdf) in enumerate(todo, 1):
        key = e["ID"]
        info(f"[{i}/{len(todo)}] {key}")
        pmcid = (manifest.get(key) or {}).get("pmcid")
        cands = make_candidates(key, pdf, args.out_candidates, pmcid=pmcid, http=http, force=args.force)
        if not cands:
            failed.append(key)
        rows.append({"key": key, "title": re.sub(r"[{}]", "", e.get("title", key)),
                     "year": e.get("year", ""), "cands": cands, "curated": key in curated})

    sheet = contact_sheet(rows, args.out_candidates)
    meta = {r["key"]: {"title": r["title"], "year": r["year"], "candidates": r["cands"]} for r in rows}
    (args.out_candidates / "candidates.yml").write_text(yaml.safe_dump(meta, sort_keys=True, allow_unicode=True))
    info(f"Contact sheet: {sheet}")
    if failed:
        warn(f"no candidates for: {', '.join(failed)}")

    if args.accept_auto:
        for r in rows:
            if r["curated"]:
                continue
            kind = args.accept_auto if args.accept_auto in r["cands"] else "page1"
            if kind in r["cands"]:
                try:
                    promote(r["key"], kind, None, args.out_candidates, args.preview_dir, args.force)
                except PipelineError as err:
                    warn(str(err))
    if args.open:
        open_sheet(sheet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
