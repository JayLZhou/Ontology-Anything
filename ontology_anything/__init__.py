"""Ontology-Anything: documents and tables in, executable ontology out.

Produces drafts in nano-onto's format, so an approved result can be served to
agents straight away (``nano-onto serve-mcp``).
"""

__version__ = "0.1.0"

from .documents import Corpus, load_corpus  # noqa: E402
from .pipeline import Draft, build, find_conflicts, quote_found  # noqa: E402
from .review import ReviewError, approve, render_review  # noqa: E402
from .tables import infer_tables  # noqa: E402

__all__ = [
    "__version__",
    "Corpus",
    "Draft",
    "ReviewError",
    "approve",
    "build",
    "find_conflicts",
    "infer_tables",
    "load_corpus",
    "quote_found",
    "render_review",
]
