"""Load a folder of mixed files into text documents and tables.

Built-in parsers cover .md/.txt, .csv, .xlsx (openpyxl), .pdf (pypdf), .docx
(python-docx) and .html. Pass ``parser="docling"`` to route PDFs/DOCX/PPTX
through Docling instead when it is installed -- it handles layout and scanned
pages far better than the light built-ins.

Tables *inside* documents are kept in the text as Markdown pipe tables, so the
model can read rules written as tables and quote them. Those that look like
lists of records (they have an identifier column) are also offered to schema
induction, like a spreadsheet would be.
"""

from __future__ import annotations

import csv
import io
import re
from html.parser import HTMLParser
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
    notes: list[str] = field(default_factory=list)

    def document(self, doc_id: str) -> Document | None:
        return next((d for d in self.documents if d.doc_id == doc_id), None)


def load_corpus(
    inputs: list[str | Path],
    parser: str = "builtin",
    window: int = 6000,
    doc_tables: str = "auto",
) -> Corpus:
    """Load files and folders.

    ``window`` is the chunk size in characters. ``doc_tables`` decides which
    tables found inside documents also become data tables: ``"auto"`` (those
    with an identifier column), ``"all"`` or ``"off"``.
    """
    if doc_tables not in ("auto", "all", "off"):
        raise ValueError(f"doc_tables must be auto, all or off, got {doc_tables!r}")
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
            elif suffix in TEXT_SUFFIXES | RICH_SUFFIXES:
                text = (path.read_text(encoding="utf-8-sig") if suffix in TEXT_SUFFIXES
                        else _rich_text(path, parser))
                doc = _document(rel, path, text, window)
                corpus.documents.append(doc)
                if doc_tables != "off":
                    _adopt_doc_tables(corpus, doc, doc_tables)
            else:
                corpus.skipped.append(f"{rel}: unsupported file type")
        except ImportError as exc:
            corpus.skipped.append(f"{rel}: {exc}")
    return corpus


def _adopt_doc_tables(corpus: Corpus, doc: Document, mode: str) -> None:
    for table in extract_doc_tables(doc.doc_id, doc.path, doc.text):
        if not table.rows:
            continue
        if mode == "all" or looks_like_records(table):
            corpus.tables.append(table)
            corpus.notes.append(f"{table.table_id}: table in a document used as data ({len(table.rows)} rows)")
        else:
            corpus.notes.append(f"{table.table_id}: table kept as text only (no identifier column)")


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
        return _docx_text(docx.Document(str(path)))
    if suffix in (".html", ".htm"):
        return html_to_text(path.read_text(encoding="utf-8-sig"))
    raise ImportError(f"{suffix} needs --parser docling")


# ------------------------------------------------- tables inside documents

_SEPARATOR_ROW = re.compile(r"^\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?$")
_ID_COLUMN = re.compile(r"(^id$|_id$|^id_|编号|代码|工号|code$|_no$|^no\.?$|number$)", re.I)
_ID_VALUE = re.compile(r"^[A-Za-z]{1,6}[-_]?\d{1,}$")
_CAPTION = re.compile(r"^\s*(?:表|附表|Table)\s*[0-9一二三四五六七八九十]+\s*[:：.、]?\s*(.+)$", re.I)


def pipe_table(rows: list[list[str]]) -> str:
    """Render rows (first one is the header) as a Markdown pipe table."""
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    clean = [[re.sub(r"\s+", " ", c).strip().replace("|", "\\|") for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(clean[0]) + " |", "|" + "|".join(" --- " for _ in range(width)) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in clean[1:]]
    return "\n".join(lines)


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", line)]


def extract_doc_tables(doc_id: str, path: Path, text: str) -> list[Table]:
    """Find Markdown pipe tables in ``text``; name each after its caption or nearest heading."""
    lines = text.splitlines()
    tables: list[Table] = []
    i = 0
    while i < len(lines) - 1:
        if "|" not in lines[i] or not _SEPARATOR_ROW.match(lines[i + 1].strip()) or "-" not in lines[i + 1]:
            i += 1
            continue
        header = _split_row(lines[i])
        if len(header) != len(_split_row(lines[i + 1])) or not any(header):
            i += 1
            continue
        j = i + 2
        rows = []
        while j < len(lines) and "|" in lines[j] and lines[j].strip():
            cells = _split_row(lines[j])
            cells = (cells + [""] * len(header))[:len(header)]
            if any(cells):
                rows.append(cells)
            j += 1
        columns, seen = [], set()
        for n, name in enumerate(header):
            name = name or f"column_{n + 1}"
            while name in seen:
                name += "_2"
            seen.add(name)
            columns.append(name)
        number = len(tables) + 1
        name = _table_name(lines, i) or f"{Path(doc_id).stem}_table{number}"
        tables.append(Table(f"{doc_id}#table{number}", path, name, columns,
                            [dict(zip(columns, r)) for r in rows]))
        i = j
    return tables


def _table_name(lines: list[str], start: int) -> str | None:
    """A caption right above the table, else the closest heading above it."""
    for back in range(start - 1, max(start - 4, -1), -1):
        line = lines[back].strip()
        if not line:
            continue
        m = _CAPTION.match(line.strip("*_ "))
        if m:
            return m.group(1).strip("*_ ：:。.")
        break
    for back in range(start - 1, -1, -1):
        if lines[back].startswith("#"):
            title = lines[back].lstrip("#").strip()
            title = re.sub(r"^(?:附件|附录|Appendix|Annex)\s*[0-9A-Za-z一二三四五六七八九十]*\s*[:：.、]?\s*", "", title, flags=re.I)
            title = re.sub(r"^[0-9一二三四五六七八九十]+\s*[.、)）]\s*", "", title)
            return title or None
    return None


def looks_like_records(table: Table) -> bool:
    """True for a list of things with identifiers; False for matrices, tiers and layouts."""
    if len(table.rows) < 2 or len(table.columns) < 2:
        return False
    for column in table.columns:
        values = [r.get(column, "") for r in table.rows]
        if not all(values) or len(set(values)) != len(values):
            continue
        if _ID_COLUMN.search(column.strip()) or all(_ID_VALUE.match(v) for v in values):
            return True
    return False


def _docx_text(document: Any) -> str:
    """Paragraphs and tables in reading order; tables as pipe tables, headings as ``#``."""
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph

    parts: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name or "") if para.style is not None else ""
            m = re.match(r"(?:Heading|标题)\s*(\d)", style)
            parts.append("#" * int(m.group(1)) + " " + text if m else text)
        elif tag == "tbl":
            rows = []
            for row in DocxTable(child, document).rows:
                cells, previous = [], None
                for cell in row.cells:  # merged cells repeat the same object
                    if cell._tc is previous:
                        continue
                    previous = cell._tc
                    cells.append(cell.text)
                rows.append(cells)
            rendered = pipe_table(rows)
            if rendered:
                parts.append(rendered)
    return "\n\n".join(parts)


class _HTMLText(HTMLParser):
    _BLOCK = {"p", "div", "li", "ul", "ol", "section", "article", "blockquote", "pre", "table", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.rows: list[list[str]] | None = None
        self.cell: list[str] | None = None
        self.depth = 0  # nested tables are flattened into the outer cell

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "noscript"):
            self.skip += 1
        elif tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.rows = []
        elif self.depth == 1 and tag == "tr":
            self.rows.append([])
        elif self.depth == 1 and tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            self.cell = []
        elif self.rows is None:
            if re.fullmatch(r"h[1-6]", tag):
                self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
            elif tag == "br":
                self.parts.append("\n")
            elif tag in self._BLOCK:
                self.parts.append("\n\n")
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "noscript"):
            self.skip = max(0, self.skip - 1)
        elif tag == "table" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.parts.append("\n\n" + pipe_table(self.rows or []) + "\n\n")
                self.rows, self.cell = None, None
        elif self.depth == 1 and tag in ("td", "th") and self.cell is not None:
            self.rows[-1].append("".join(self.cell))
            self.cell = None
        elif self.rows is None and (tag in self._BLOCK or re.fullmatch(r"h[1-6]", tag)):
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.rows is None:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_NORMATIVE = re.compile(
    r"须|应当|应该|必须|不得|禁止|严禁|不允许|方可|可以|有权|负责|审批|批准|列入|暂停|取消|终止|施行|生效|自.{0,14}起"
    r"|\b(?:must|shall|should|may not|may only|cannot|can not|required?|requires|prohibited|forbidden|not allowed|"
    r"not permitted|only if|unless|approv\w+|authori[sz]\w+|responsible|effective|blacklist\w*|suspend\w*|"
    r"terminat\w+|cancel\w*|exceed\w*|at least|at most|no more than|no later than)\b",
    re.IGNORECASE,
)


def looks_normative(text: str) -> bool:
    """A cheap test for wording that states a rule, a permission or a change to a record."""
    return bool(_NORMATIVE.search(text))
