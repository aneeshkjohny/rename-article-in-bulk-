# rename_pdfs.py

Bulk-rename academic PDFs to a clean, consistent, searchable format:

```
author_first-five-title-words_year.pdf
```

Example: `vaswani_attention-is-all-you-need_2017_arxiv.pdf`

The script finds the paper's metadata (author, title, year) using several sources, ordered by reliability, and builds the new filename from it. **It is a dry run by default** — nothing is renamed until you pass `--apply`.

---

## Features

- **Dry-run by default** – preview every rename before committing.
- **Multi-source metadata lookup**, trusted in this order:
  1. **arXiv ID** found in the PDF text or filename → arXiv API (most reliable)
  2. **DOI** found in the PDF text → Crossref API
  3. **XMP metadata** embedded in the PDF
  4. The classic PDF **/Info dictionary** (Title / Author / CreationDate)
  5. **Layout heuristic** – largest-font line on page 1 is used as the title (needs `pdfplumber`)
  6. Year fallback: first plausible year (1950–2049) in the first 3000 characters of the text
- **Fills gaps across sources** – if one source gives only a title, the missing author/year are filled from the next source. The log records the combined source (e.g. `arXiv:2301.12345+XMP`).
- **Partial names allowed** – uses author + title, title only, or author only, depending on what could be recovered.
- **Smart author handling**
  - Takes the first author's surname only.
  - Understands `"Last, First"` and `"First Last"` forms.
  - Handles name particles (`van der Waals`, `de Souza`, `Del Rio`, `bin`, `al`, …).
  - Ignores placeholder authors such as `Anonymous`, `Microsoft Office User`, `Adobe Acrobat`, `LaTeX`, laptop brands, etc.
- **Title cleaning** – strips inline LaTeX (`$...$`, `\emph{x}`), braces, accents, punctuation, and leading articles (*a / an / the*). Ignores junk titles such as `untitled`, `Microsoft Word - Document`, or titles ending in `.dvi/.doc/.tex/.pdf`.
- **arXiv tagging** – files recognised as arXiv copies get an `_arxiv` suffix.
- **Collision safe** – never overwrites; if the target exists, appends `_2`, `_3`, … (up to 99).
- **Already-named detection** – files already matching the target name are left alone.
- **Offline mode** – no network calls, embedded metadata only.
- **Polite to APIs** – configurable delay between requests.
- **CSV log** of every decision (old path, new name, metadata source / skip reason / error).
- Accepts any mix of **files and folders**, optionally recursive.
- Handles encrypted PDFs that open with an empty password.

---

## Installation

Requires Python 3.9+ (uses `from __future__ import annotations`).

```bash
pip install pypdf requests
pip install pdfplumber     # optional – enables the font-size title guess
```

> `pypdf` is required (the script exits without it). `pdfplumber` is optional; without it, the layout-based title fallback is skipped. Network lookups use only the standard library (`urllib`).

---

## Usage

```bash
python rename_pdfs.py PATH [PATH ...] [options]
```

`PATH` can be one or more PDF files and/or folders. Anything else is skipped with a message.

### Quick examples

```bash
# Preview renames for a folder (no changes)
python rename_pdfs.py ~/papers

# Actually rename
python rename_pdfs.py ~/papers --apply

# Recurse into sub-folders and save a CSV log
python rename_pdfs.py ~/papers -r --apply --csv log.csv

# Single file, no internet
python rename_pdfs.py paper.pdf --offline --apply

# Keep 3 title words, underscore separator, preserve capitalisation
python rename_pdfs.py ~/papers -w 3 --word-sep _ --keep-case --apply

# Only rename files where a year could be determined
python rename_pdfs.py ~/papers --require-year --apply
```

---

## Options

| Option | Default | Description |
|---|---|---|
| `paths` | *(required)* | One or more PDF files and/or folders. |
| `-r`, `--recursive` | off | Descend into sub-folders. |
| `--apply` | off | Actually rename files. Without it, the run is a dry run. |
| `--offline` | off | Skip arXiv/Crossref lookups; use only embedded metadata and page text. |
| `-w`, `--words N` | `5` | Number of title words to keep. |
| `--word-sep SEP` | `-` | Separator between title words. |
| `--keep-case` | off | Preserve capitalisation (default is lower-case). |
| `--require-year` | off | Skip files whose year cannot be determined (otherwise `undated` is used). |
| `--delay SECONDS` | `0.5` | Pause between API calls. |
| `--csv FILE` | – | Write a log with columns `old_path, new_name, metadata_source`. |

---

## Naming rules

Filename = `author` `_` `title-words` `_` `year` [`_arxiv`] `.pdf`

- **Author** – first author's surname, accents removed, only `A–Z a–z 0-9 -` kept.
- **Title** – first *N* words (default 5), after dropping a leading *a/an/the*; accents and special characters removed.
- **Year** – publication year; if unknown, the literal `undated` (unless `--require-year`).
- Missing author or title is simply omitted; if **both** are missing, the file is **skipped** (a year alone is never enough).
- The name stem is capped at 180 characters.
- Lower-case unless `--keep-case`.

| Recovered info | Result |
|---|---|
| author + title + year | `smith_deep-learning-for-image-segmentation_2021.pdf` |
| title + year | `deep-learning-for-image-segmentation_2021.pdf` |
| author + title, no year | `smith_deep-learning-for-image-segmentation_undated.pdf` |
| arXiv copy | `smith_deep-learning-for-image-segmentation_2021_arxiv.pdf` |
| nothing usable | skipped |

---

## Output

For each file the script prints the original name, the proposed name, and the metadata source:

```
* 2301.12345.pdf
    -> vaswani_attention-is-all-you-need_2017_arxiv.pdf   [arXiv:2301.12345]

* scan0042.pdf
    -> SKIPPED (no author or title found)

Done. renamed=0  skipped=1  total=2
Re-run with --apply to perform the renames.
```

Possible statuses: renamed, `already named correctly`, `SKIPPED (reason)`, or `rename failed`.

### CSV log

With `--csv log.csv`, one row per file:

| old_path | new_name | metadata_source |
|---|---|---|
| `/papers/a.pdf` | `smith_title-words_2020.pdf` | `doi:10.1000/xyz+XMP` |
| `/papers/b.pdf` | | `skipped: no author or title found` |
| `/papers/c.pdf` | `c-name.pdf` | `unchanged` |
| `/papers/d.pdf` | `x.pdf` | `error: <OS error>` |

---

## Notes & limitations

- **Set a real contact in the User-Agent.** `USER_AGENT` near the top of the script contains a placeholder (`mailto:you@example.com`). Replace it with your email — Crossref and arXiv prefer identifiable clients.
- Only the first **2 pages** are read for text (arXiv/DOI/year detection).
- A DOI found in the text may be a *cited* paper rather than the paper itself; check the dry-run output for sanity before `--apply`.
- Scanned (image-only) PDFs have no text layer, so only filename/metadata can help; many will be skipped.
- Renames happen in place (same folder); files are never moved.
- Only `.pdf` files are processed.
- Network failures are non-fatal: the script prints a warning and falls back to the next source.
- Exit code is `1` if no PDFs are found, otherwise `0`.

---

## Recommended workflow

1. Run a dry run: `python rename_pdfs.py ~/papers -r --csv preview.csv`
2. Review the output / CSV.
3. Apply: `python rename_pdfs.py ~/papers -r --apply --csv log.csv`
4. Keep `log.csv` as a record of old → new names.
