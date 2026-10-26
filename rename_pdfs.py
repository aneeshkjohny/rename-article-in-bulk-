#!/usr/bin/env python3
"""
rename_pdfs.py — rename academic PDFs to  Author_First-Five-Title-Words_Year.pdf

Metadata is gathered in this order of trust:

  1. arXiv ID found in the PDF text or filename  -> arXiv API (most reliable)
  2. DOI found in the PDF text                   -> Crossref API
  3. XMP metadata embedded in the PDF
  4. The classic /Info dictionary (Title/Author/CreationDate)
  5. Heuristic: largest-font line on page 1 as the title

Dry-run is the default. Nothing is renamed until you pass --apply.

Install:
    pip install pypdf requests
    pip install pdfplumber        # optional, enables the font-size title guess

Examples:
    python rename_pdfs.py ~/papers                    # preview
    python rename_pdfs.py ~/papers --apply            # do it
    python rename_pdfs.py ~/papers -r --apply --csv log.csv
    python rename_pdfs.py paper.pdf --offline --apply # no network calls
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("pypdf is required:  pip install pypdf")

USER_AGENT = "rename_pdfs.py/1.0 (mailto:you@example.com)"
ARXIV_API = "http://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works/"

# Words we don't want to see as the "first five words" of a title.
LEADING_JUNK = {"a", "an", "the"}

# Values PDF writers drop into /Author when nobody set one. Treat as unknown.
PLACEHOLDER_AUTHORS = {
    "anonymous", "unknown", "author", "authors", "user", "owner", "guest",
    "admin", "administrator", "microsoft office user", "windows user",
    "hp", "dell", "acer", "asus", "lenovo", "toshiba", "sony", "pc",
    "adobe acrobat", "acrobat", "word", "latex", "pdflatex", "none", "n/a",
}

STOP_TITLE_LINES = re.compile(
    r"^(arxiv|preprint|submitted|accepted|published|under review|draft|"
    r"proceedings|journal of|abstract|keywords|introduction|www\.|http)",
    re.I,
)

ARXIV_NEW = re.compile(r"arxiv[:\s]*(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
ARXIV_OLD = re.compile(r"arxiv[:\s]*([a-z-]+(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?", re.I)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
# The stamp arXiv prints on page 1 of its own PDFs, e.g.
#   "arXiv:2301.12345v2  [cs.LG]  5 Jan 2023"  or  "arXiv:hep-th/9901001v1 [hep-th]"
# Requiring the "[category]" part avoids matching papers that merely cite arXiv.
ARXIV_STAMP = re.compile(
    r"arxiv\s*:\s*(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?\s*\[[A-Za-z.\-]+\]",
    re.I,
)
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


# --------------------------------------------------------------------------
# data container
# --------------------------------------------------------------------------
@dataclass
class Meta:
    author: str = ""
    title: str = ""
    year: str = ""
    source: str = ""
    arxiv: bool = False

    def complete(self) -> bool:
        return bool(self.author and self.title and self.year)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def http_get(url: str, timeout: int = 20) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:  # network down, 404, rate limit...
        print(f"    ! request failed ({exc})")
        return None


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def surname(author_field: str) -> str:
    """Pull the family name of the first author out of a messy author string."""
    if not author_field:
        return ""
    if author_field.strip().lower().strip(".") in PLACEHOLDER_AUTHORS:
        return ""
    # take the first author only
    first = re.split(r";|\band\b|,\s*(?=[A-Z][a-z]+\s+[A-Z])|&", author_field)[0]
    first = first.strip().strip(",")
    if not first:
        return ""
    if "," in first:  # "Ramanujan, Srinivasa"
        return first.split(",")[0].strip()
    parts = [p for p in first.split() if p]
    if not parts:
        return ""
    # handle "van der Waals", "de Souza", "Del Rio"
    particles = {"van", "von", "de", "del", "della", "der", "den", "di", "da",
                 "dos", "la", "le", "bin", "ibn", "al"}
    for i, part in enumerate(parts[:-1]):
        if part.lower() in particles:
            return " ".join(parts[i:])
    return parts[-1]


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title.replace("\n", " ")).strip()
    title = re.sub(r"\$[^$]*\$", "", title)          # inline LaTeX math
    title = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", title)  # \emph{x} -> x
    title = title.replace("{", "").replace("}", "")
    return title.strip(" .:-–—")


def title_words(title: str, count: int) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", clean_title(title))
    while words and words[0].lower() in LEADING_JUNK:
        words.pop(0)
    return words[:count]


def sanitize(part: str, keep_case: bool) -> str:
    part = strip_accents(part)
    part = re.sub(r"[^A-Za-z0-9\- ]+", "", part)
    part = re.sub(r"\s+", "-", part.strip())
    return part if keep_case else part.lower()


def year_from(text: str) -> str:
    m = YEAR_RE.search(text or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# extraction from the file itself
# --------------------------------------------------------------------------
def read_pdf(path: Path, pages: int = 2) -> tuple[PdfReader | None, str]:
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        text = ""
        for page in reader.pages[:pages]:
            try:
                text += page.extract_text() or ""
            except Exception:
                continue
        return reader, text
    except Exception as exc:
        print(f"    ! cannot open PDF ({exc})")
        return None, ""


def meta_from_info(reader: PdfReader) -> Meta:
    m = Meta(source="pdf /Info")
    try:
        info = reader.metadata or {}
    except Exception:
        return Meta()
    title = str(info.get("/Title", "") or "")
    author = str(info.get("/Author", "") or "")
    created = str(info.get("/CreationDate", "") or "")

    # LaTeX/Word often leave junk like "untitled" or a file path in /Title
    if title.lower().strip() in {"untitled", "microsoft word - document", ""} or \
       title.lower().endswith((".dvi", ".doc", ".tex", ".pdf")):
        title = ""
    m.title = clean_title(title)
    m.author = surname(author)
    m.year = year_from(created)          # "D:20230115120000" -> 2023
    return m


def meta_from_xmp(reader: PdfReader) -> Meta:
    m = Meta(source="XMP")
    try:
        xmp = reader.xmp_metadata
    except Exception:
        return Meta()
    if not xmp:
        return Meta()
    try:
        titles = xmp.dc_title or {}
        if titles:
            m.title = clean_title(next(iter(titles.values())))
        creators = xmp.dc_creator or []
        if creators:
            m.author = surname(creators[0])
        date = xmp.xmp_create_date or (xmp.dc_date[0] if xmp.dc_date else None)
        if date:
            m.year = str(date.year)
    except Exception:
        pass
    return m


def title_by_font_size(path: Path) -> str:
    """Largest-font text block on page 1 — usually the title. Needs pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        return ""
    try:
        with pdfplumber.open(str(path)) as pdf:
            if not pdf.pages:
                return ""
            words = pdf.pages[0].extract_words(extra_attrs=["size", "top"])
    except Exception:
        return ""
    if not words:
        return ""
    biggest = max(round(w.get("size", 0), 1) for w in words)
    # group the big words into lines by their vertical position
    lines: dict[int, list] = {}
    for w in words:
        if round(w.get("size", 0), 1) >= biggest - 0.6:
            lines.setdefault(round(w["top"] / 4), []).append(w)
    chunks = []
    for key in sorted(lines):
        line = " ".join(w["text"] for w in sorted(lines[key], key=lambda x: x["x0"]))
        if STOP_TITLE_LINES.match(line.strip()):
            continue
        chunks.append(line.strip())
    return clean_title(" ".join(chunks))[:250]


# --------------------------------------------------------------------------
# online lookups
# --------------------------------------------------------------------------
def find_arxiv_id(text: str, filename: str) -> str:
    for blob in (text, filename):
        for pattern in (ARXIV_NEW, ARXIV_OLD):
            m = pattern.search(blob or "")
            if m:
                return m.group(1)
    # bare "2301.12345" style filename, e.g. downloaded straight from arXiv
    m = re.fullmatch(r"(\d{4}\.\d{4,5})(v\d+)?", Path(filename).stem)
    return m.group(1) if m else ""


def is_arxiv_file(text: str, filename: str) -> bool:
    """True if the PDF is an arXiv copy (page-1 stamp or arXiv-style filename)."""
    if ARXIV_STAMP.search(text or ""):
        return True
    stem = Path(filename).stem
    return bool(re.fullmatch(r"(\d{4}\.\d{4,5}|[a-z-]+_\d{7})(v\d+)?", stem, re.I))


def fetch_arxiv(arxiv_id: str) -> Meta:
    url = f"{ARXIV_API}?id_list={urllib.parse.quote(arxiv_id)}&max_results=1"
    raw = http_get(url)
    if not raw:
        return Meta()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return Meta()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", ns)
    if entry is None:
        return Meta()
    m = Meta(source=f"arXiv:{arxiv_id}")
    node = entry.find("a:title", ns)
    if node is not None and node.text:
        m.title = clean_title(node.text)
    author = entry.find("a:author/a:name", ns)
    if author is not None and author.text:
        m.author = surname(author.text)
    published = entry.find("a:published", ns)
    if published is not None and published.text:
        m.year = published.text[:4]
    return m


def fetch_crossref(doi: str) -> Meta:
    raw = http_get(CROSSREF_API + urllib.parse.quote(doi))
    if not raw:
        return Meta()
    try:
        msg = json.loads(raw)["message"]
    except Exception:
        return Meta()
    m = Meta(source=f"doi:{doi}")
    if msg.get("title"):
        m.title = clean_title(msg["title"][0])
    if msg.get("author"):
        a = msg["author"][0]
        m.author = a.get("family") or surname(a.get("name", ""))
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            m.year = str(parts[0][0])
            break
    return m


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def merge(base: Meta, extra: Meta) -> Meta:
    """Fill blank fields of `base` from `extra`, tracking where things came from."""
    used = False
    for field in ("author", "title", "year"):
        if not getattr(base, field) and getattr(extra, field):
            setattr(base, field, getattr(extra, field))
            used = True
    if used and extra.source:
        base.source = f"{base.source}+{extra.source}" if base.source else extra.source
    return base


def gather(path: Path, offline: bool, delay: float) -> Meta:
    reader, text = read_pdf(path)
    meta = Meta()
    meta.arxiv = is_arxiv_file(text, path.name)

    if not offline:
        arxiv_id = find_arxiv_id(text, path.name)
        if arxiv_id:
            meta = merge(meta, fetch_arxiv(arxiv_id))
            time.sleep(delay)
        if not meta.complete():
            doi = DOI_RE.search(text or "")
            if doi:
                meta = merge(meta, fetch_crossref(doi.group(0).rstrip(".,;")))
                time.sleep(delay)

    if reader is not None and not meta.complete():
        meta = merge(meta, meta_from_xmp(reader))
    if reader is not None and not meta.complete():
        meta = merge(meta, meta_from_info(reader))
    if not meta.title:
        guessed = title_by_font_size(path)
        # a one-word or very short fragment is page furniture, not a title
        if guessed and len(guessed) >= 12 and len(guessed.split()) >= 3:
            meta = merge(meta, Meta(title=guessed, source="page-1 layout"))
    if not meta.year:
        meta = merge(meta, Meta(year=year_from(text[:3000]), source="page-1 text"))
    return meta


def build_name(meta: Meta, args) -> str | None:
    """Use whatever is available: author+title, title only, or author only.

    Returns None only when neither an author nor a title could be recovered —
    a year on its own is not enough to name a file by.
    """
    words = title_words(meta.title, args.words)
    author_part = sanitize(meta.author, args.keep_case) if meta.author else ""
    title_part = args.word_sep.join(
        sanitize(w, args.keep_case) for w in words) if words else ""

    if not author_part and not title_part:
        return None

    fields = [p for p in (author_part, title_part) if p]
    if meta.year:
        fields.append(meta.year)
    elif args.require_year:
        return None
    else:
        fields.append("undated")
    stem = "_".join(f for f in fields if f)[:180]
    if meta.arxiv:
        stem += "_arxiv"
    return stem + ".pdf"


def unique_path(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 100):
        candidate = folder / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many collisions for {name}")


def collect_pdfs(inputs: list[str], recursive: bool) -> list[Path]:
    found: list[Path] = []
    for item in inputs:
        p = Path(item).expanduser()
        if p.is_dir():
            found += sorted(p.rglob("*.pdf") if recursive else p.glob("*.pdf"))
        elif p.suffix.lower() == ".pdf" and p.is_file():
            found.append(p)
        else:
            print(f"skipping {p} (not a PDF or directory)")
    return found


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rename academic PDFs to Author_Title-Words_Year.pdf")
    ap.add_argument("paths", nargs="+", help="PDF files and/or folders")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="descend into sub-folders")
    ap.add_argument("--apply", action="store_true",
                    help="actually rename (default is a dry run)")
    ap.add_argument("--offline", action="store_true",
                    help="skip arXiv/Crossref lookups, use embedded metadata only")
    ap.add_argument("-w", "--words", type=int, default=5,
                    help="how many title words to keep (default 5)")
    ap.add_argument("--word-sep", default="-",
                    help="separator between title words (default '-')")
    ap.add_argument("--keep-case", action="store_true",
                    help="preserve capitalisation instead of lower-casing")
    ap.add_argument("--require-year", action="store_true",
                    help="skip files whose year cannot be determined")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds to pause between API calls (default 0.5)")
    ap.add_argument("--csv", metavar="FILE",
                    help="write an old-name/new-name/source log")
    args = ap.parse_args()

    pdfs = collect_pdfs(args.paths, args.recursive)
    if not pdfs:
        print("No PDFs found.")
        return 1

    print(f"{len(pdfs)} PDF(s) found. "
          f"{'RENAMING' if args.apply else 'DRY RUN — nothing will change'}.\n")

    rows, renamed, skipped = [], 0, 0
    for path in pdfs:
        print(f"* {path.name}")
        meta = gather(path, args.offline, args.delay)
        new_name = build_name(meta, args)

        if not new_name:
            reason = ("no year found" if args.require_year and not meta.year
                      else "no author or title found")
            print(f"    -> SKIPPED ({reason})\n")
            skipped += 1
            rows.append([str(path), "", f"skipped: {reason}"])
            continue
        if new_name == path.name:
            print("    -> already named correctly\n")
            rows.append([str(path), path.name, "unchanged"])
            continue

        target = unique_path(path.parent, new_name)
        print(f"    -> {target.name}   [{meta.source}]")
        if args.apply:
            try:
                path.rename(target)
                renamed += 1
            except OSError as exc:
                print(f"    ! rename failed ({exc})")
                rows.append([str(path), target.name, f"error: {exc}"])
                continue
        rows.append([str(path), target.name, meta.source])
        print()

    print(f"\nDone. renamed={renamed if args.apply else 0}  "
          f"skipped={skipped}  total={len(pdfs)}")
    if not args.apply:
        print("Re-run with --apply to perform the renames.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["old_path", "new_name", "metadata_source"])
            writer.writerows(rows)
        print(f"Log written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
