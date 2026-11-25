"""Load a folder of mixed files into text documents and tables.

Built-in parsers cover .md/.txt, .csv, .xlsx (openpyxl), .pdf (pypdf) and
.docx (python-docx). Pass ``parser="docling"`` to route PDFs/DOCX/PPTX through
Docling instead when it is installed -- it handles layout and scanned pages far
better than the light built-ins.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
TABLE_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xlsm"}
RICH_SUFFIXES = {".pdf", ".docx", ".pptx", ".html", ".htm"}

_DATE_RE = re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?")
_EFFECTIVE_RE = re.compile(r"(?:自|于)\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*(?:起|开始)|"
                           r"effective\s+(?:from\s+|on\s+)?(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


@dataclass
class Chunk:
    doc_id: str
    index: int
    text: str
    start: int
    end: int

    @property
    def key(self) -> str:
        return f"{self.doc_id}#{self.index}"


@dataclass
class Document:
    doc_id: str
    path: Path
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
    chunks: list[Chunk] = field(default_factory=list)


@dataclass
class Table:
    table_id: str
    path: Path
    name: str
    columns: list[str]
    rows: list[dict[str, str]]


@dataclass
class Corpus:
    root: Path
    documents: list[Document] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def document(self, doc_id: str) -> Document | None:
        return next((d for d in self.documents if d.doc_id == doc_id), None)


def load_corpus(inputs: list[str | Path], parser: str = "builtin", window: int = 6000) -> Corpus:
    paths: list[Path] = []
    roots = []
    for item in inputs:
        p = Path(item)
        if not p.exists():
            raise FileNotFoundError(item)
        p = p.resolve()  # ids are relative to the common root, however the inputs were spelled
        if p.is_dir():
            roots.append(p)
            paths.extend(sorted(x for x in p.rglob("*") if x.is_file() and not x.name.startswith(".")))
        elif p.is_file():
            roots.append(p.parent)
            paths.append(p)
        else:
            raise FileNotFoundError(item)
    root = Path(_common_root(roots)) if roots else Path(".")
    corpus = Corpus(root=root)
    for path in paths:
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
        suffix = path.suffix.lower()
        try:
            if suffix in TABLE_SUFFIXES:
                corpus.tables.extend(_load_tables(path, rel))
            elif suffix in TEXT_SUFFIXES:
                corpus.documents.append(_document(rel, path, path.read_text(encoding="utf-8-sig"), window))
            elif suffix in RICH_SUFFIXES:
                corpus.documents.append(_document(rel, path, _rich_text(path, parser), window))
            else:
                corpus.skipped.append(f"{rel}: unsupported file type")
        except ImportError as exc:
            corpus.skipped.append(f"{rel}: {exc}")
    return corpus


def _common_root(paths: list[Path]) -> str:
    import os

    return os.path.commonpath([str(p.resolve()) for p in paths])


def _document(doc_id: str, path: Path, text: str, window: int) -> Document:
    doc = Document(doc_id=doc_id, path=path, text=text, meta=_doc_meta(text))
    doc.chunks = chunk_text(doc_id, text, window)
    return doc


def _doc_meta(text: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
    if title:
        meta["title"] = title[:120]
    m = _EFFECTIVE_RE.search(text)
    if m:
        meta["effective_date"] = normalize_date(m.group(1) or m.group(2))
    dates = [normalize_date(x.group(0)) for x in _DATE_RE.finditer(text)]
    dates = [d for d in dates if d]
    if dates:
        meta["latest_date_mentioned"] = max(dates)
    return meta


def normalize_date(text: str | None) -> str | None:
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def chunk_text(doc_id: str, text: str, window: int = 6000) -> list[Chunk]:
    """Greedy paragraph packing into windows of at most ``window`` characters."""
    chunks: list[Chunk] = []
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.S)]
    if not spans:
        return chunks
    start, end = spans[0]
    for s, e in spans[1:]:
        if e - start > window:
            chunks.append(Chunk(doc_id, len(chunks), text[start:end], start, end))
            start = s
        end = e
    chunks.append(Chunk(doc_id, len(chunks), text[start:end], start, end))
    # split any single paragraph that is still longer than the window
    out: list[Chunk] = []
    for c in chunks:
        if len(c.text) <= window:
            out.append(Chunk(doc_id, len(out), c.text, c.start, c.end))
            continue
        for offset in range(0, len(c.text), window):
            piece = c.text[offset:offset + window]
            out.append(Chunk(doc_id, len(out), piece, c.start + offset, c.start + offset + len(piece)))
    return out


def _load_tables(path: Path, rel: str) -> list[Table]:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        raw = path.read_text(encoding="utf-8-sig")
        delimiter = "\t" if suffix == ".tsv" else (csv.Sniffer().sniff(raw[:2048], ",;\t").delimiter if raw else ",")
        reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
        rows = [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]
        return [Table(rel, path, path.stem, [c.strip() for c in reader.fieldnames or []], rows)]
    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        raise ImportError("install openpyxl to read Excel files (pip install openpyxl)")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    tables = []
    for ws in wb.worksheets:
        values = list(ws.iter_rows(values_only=True))
        if not values:
            continue
        header = [str(c).strip() if c is not None else "" for c in values[0]]
        rows = []
        for raw_row in values[1:]:
            if all(c is None for c in raw_row):
                continue
            rows.append({h: ("" if c is None else str(c).strip()) for h, c in zip(header, raw_row) if h})
        name = path.stem if len(wb.worksheets) == 1 else ws.title
        tables.append(Table(f"{rel}:{ws.title}", path, name, [h for h in header if h], rows))
    return tables


def _rich_text(path: Path, parser: str) -> str:
    if parser == "docling":
        try:
            from docling.document_converter import DocumentConverter
        except ImportError:  # pragma: no cover
            raise ImportError("parser=docling needs `pip install docling`")
        return DocumentConverter().convert(str(path)).document.export_to_markdown()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:  # pragma: no cover
            raise ImportError("install pypdf to read PDFs, or use --parser docling")
        return "\n\n".join((page.extract_text() or "").strip() for page in PdfReader(str(path)).pages)
    if suffix == ".docx":
        try:
            import docx
        except ImportError:  # pragma: no cover
            raise ImportError("install python-docx to read .docx, or use --parser docling")
        return "\n\n".join(p.text for p in docx.Document(str(path)).paragraphs if p.text.strip())
    if suffix in (".html", ".htm"):
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", path.read_text(encoding="utf-8"), flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n\n", text, flags=re.I)
        return re.sub(r"<[^>]+>", "", text)
    raise ImportError(f"{suffix} needs --parser docling")
