#!/usr/bin/env python3
"""okf ingest — URL/document ingest producer for the OKF brain."""

from __future__ import annotations

import argparse
import datetime
import json
import hashlib
import logging
import pathlib
import re
import sys
import urllib.parse
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Configuration and constants
# ---------------------------------------------------------------------------
INGEST_TS = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
USER_AGENT = "okf-ingest/1.0 (plain-text fetcher)"
FETCH_TIMEOUT = 10  # seconds

# Personal domains default to private; all others default to shareable (D-015).
PERSONAL_DOMAINS = {"life", "people", "orgs", "documents", "work"}

# Tag hierarchy: flat tag → hierarchical tag (loaded from tags/hierarchy.json)
_TAG_HIERARCHY: dict[str, str] | None = None

def _load_tag_hierarchy() -> dict[str, str]:
    """Load tag hierarchy mapping from tags/hierarchy.json (cached)."""
    global _TAG_HIERARCHY
    if _TAG_HIERARCHY is not None:
        return _TAG_HIERARCHY
    hier_path = pathlib.Path(__file__).resolve().parent.parent / "tags" / "hierarchy.json"
    if hier_path.exists():
        with open(hier_path, encoding="utf-8") as f:
            _TAG_HIERARCHY = json.load(f)
    else:
        _TAG_HIERARCHY = {}
    return _TAG_HIERARCHY

def hierarchize_tags(tags: List[str]) -> List[str]:
    """Rename flat tags to their hierarchical form using tags/hierarchy.json.
    Tags already containing '/' are left as-is.  The special tag 'dev' is preserved."""
    hierarchy = _load_tag_hierarchy()
    result = []
    for t in tags:
        if "/" in t:
            result.append(t)
        elif t in hierarchy:
            result.append(hierarchy[t])
        else:
            result.append(t)
    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for t in result:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def slugify(stem: str) -> str:
    """Normalize a string for use as a filesystem slug."""
    s = stem.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def default_visibility(domain: str) -> str:
    """Domain-based visibility default (D-015). Personal domains → private."""
    d = (domain or "").strip().lower()
    return "private" if d in PERSONAL_DOMAINS else "shareable"


def _parse_inline_list(raw: str) -> List[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [t.strip().strip("\"'" ) for t in inner.split(",") if t.strip()]
    return [t.strip().strip("\"'" ) for t in raw.split(",") if t.strip()]


def escape_body_hashtags(body: str) -> str:
    """Backslash-escape #word patterns in body text so Obsidian doesn't parse them as tags.

    Obsidian treats any ``#word`` in body text as a tag.  This function escapes
    such patterns with a leading backslash (``\\#word``) so Obsidian displays the
    ``#`` character but does not create a tag.  Protected zones are left untouched:

    - Fenced code blocks (````` ... `````)
    - Inline code (`` ` ... ` ``)
    - Markdown link URLs (``[text](url#anchor)``)
    - Markdown headings (``## Heading``)
    - Already-escaped ``\\#word``
    """
    if not body:
        return body

    # Placeholders for protected content
    _idx = [0]
    _map: dict[str, str] = {}

    def _ph(m: re.Match) -> str:
        key = f"\x00PH{_idx[0]}\x00"
        _idx[0] += 1
        _map[key] = m.group(0)
        return key

    # 1. Extract fenced code blocks
    w = re.sub(r"```[\s\S]*?```", _ph, body)
    # 2. Extract inline code
    w = re.sub(r"`[^`]+`", _ph, w)
    # 3. Extract markdown images ![alt](url)
    w = re.sub(r"!\[[^\]]*\]\([^\)]*\)", _ph, w)
    # 4. Protect markdown link URLs [display](url) — keep display text, hide url
    def _link(m: re.Match) -> str:
        display = m.group(1)
        url = m.group(2)
        key = f"\x00PH{_idx[0]}\x00"
        _idx[0] += 1
        _map[key] = url
        return f"[{display}]({key})"
    w = re.sub(r"\[([^\]]*)\]\(([^\)]*)\)", _link, w)
    # 5. Extract bare URLs
    w = re.sub(r"https?://\S+", _ph, w)
    # 6. Escape #word in remaining text (not headings, not already escaped)
    w = re.sub(
        r"(?<!\\)(?<!#)(^|[\s\(\[{,;:!?\'\"])(#[a-zA-Z][\w/]*(?:/[\w]+)*)",
        lambda m: f"{m.group(1)}\\{m.group(2)}",
        w,
    )
    # 7. Restore placeholders
    for key, value in _map.items():
        w = w.replace(key, value)

# ---------------------------------------------------------------------------
# Security screening (prompt injection / steganography / homoglyphs)
# ---------------------------------------------------------------------------
# Injection phrase bank vendored from
# https://raw.githubusercontent.com/darshanNhb/okf-guard/main/src/okfguard/rules/injection_patterns.py
# Apache-2.0 (darshanNhb/okf-guard), vendored for stdlib-only ingest.
# 31 patterns across 8 categories: instruction_override, new_instruction,
# fake_role_assertion, fake_system_marker, direct_ai_address,
# data_exfiltration, permission_override, jailbreak.
_INJECTION_PATTERNS: tuple[tuple[str, "re.Pattern[str]", float], ...] = (
    ("instruction_override", re.compile(r"ignore (all |the )?(previous|prior|above|earlier|preceding) (instructions?|prompts?|directives?|rules?|guidelines?)", re.IGNORECASE), 0.85),
    ("instruction_override", re.compile(r"disregard (all |the )?(above|previous|prior|earlier|preceding)", re.IGNORECASE), 0.80),
    ("instruction_override", re.compile(r"forget (all |the )?(previous|prior|above|earlier) (instructions?|context|prompts?|rules?)", re.IGNORECASE), 0.80),
    ("instruction_override", re.compile(r"do not follow (the )?(previous|prior|above|original) (instructions?|rules?|guidelines?)", re.IGNORECASE), 0.80),
    ("instruction_override", re.compile(r"override (all |the )?(previous|prior|existing|current) (instructions?|rules?|policies?|guidelines?)", re.IGNORECASE), 0.80),
    ("new_instruction", re.compile(r"(new|updated|revised|replacement) (instructions?|directives?|rules?|guidelines?)\s*:", re.IGNORECASE), 0.70),
    ("new_instruction", re.compile(r"your (new|actual|real|true) (task|instructions?|objective|purpose|mission|goal) (is|are)\b", re.IGNORECASE), 0.75),
    ("new_instruction", re.compile(r"from now on,?\s+(you (should|must|will|shall)|always|never)", re.IGNORECASE), 0.70),
    ("new_instruction", re.compile(r"instead,?\s+(you (should|must|will)|do the following|follow these)", re.IGNORECASE), 0.65),
    ("fake_role_assertion", re.compile(r"you are now\b", re.IGNORECASE), 0.70),
    ("fake_role_assertion", re.compile(r"(act|behave|operate|function|respond) as\s+(if you were |though you are |a |an |the )", re.IGNORECASE), 0.65),
    ("fake_role_assertion", re.compile(r"(pretend|imagine|assume|suppose) (that )?you('re| are) (a |an |the )?", re.IGNORECASE), 0.70),
    ("fake_role_assertion", re.compile(r"switch(ing)? (to|into) (a |an )?(\w+ )?mode", re.IGNORECASE), 0.60),
    ("fake_system_marker", re.compile(r"\b(SYSTEM|ADMIN|ROOT|SUPERUSER|DEVELOPER)\s*(PROMPT|MESSAGE|INSTRUCTION|OVERRIDE|COMMAND)?\s*:", re.IGNORECASE), 0.60),
    ("fake_system_marker", re.compile(r"\[SYSTEM\]|\[ADMIN\]|\[INTERNAL\]|\[OVERRIDE\]", re.IGNORECASE), 0.65),
    ("fake_system_marker", re.compile(r"<\s*system\s*>|<\s*/?\s*instructions?\s*>", re.IGNORECASE), 0.70),
    ("fake_system_marker", re.compile(r"###\s*(SYSTEM|INTERNAL|ADMIN)\s*(INSTRUCTIONS?|PROMPT|MESSAGE|DIRECTIVE)", re.IGNORECASE), 0.70),
    ("direct_ai_address", re.compile(r"\b(AI|assistant|agent|model|LLM|GPT|chatbot|bot)\s*[,:]?\s*(when|if|please|do not|don't|you (should|must|will))\s+", re.IGNORECASE), 0.75),
    ("direct_ai_address", re.compile(r"\b(AI|assistant|agent|model|LLM)\s*[,:]?\s*(when|if) you (read|process|see|parse|encounter|find|receive)", re.IGNORECASE), 0.75),
    ("direct_ai_address", re.compile(r"(dear|attention|note to|message for|hey)\s+(AI|assistant|agent|model|LLM|bot)", re.IGNORECASE), 0.70),
    ("data_exfiltration", re.compile(r"(send|forward|email|transmit|post|upload|exfiltrate|leak)\s+(this|the|all|any|every)\s+.{0,30}\s+(to|at|via)\s+", re.IGNORECASE), 0.70),
    ("data_exfiltration", re.compile(r"(include|embed|insert|append|attach)\s+(the |all |this )?(api[- ]?key|password|token|secret|credential|private[- ]?key|access[- ]?key|auth)", re.IGNORECASE), 0.80),
    ("data_exfiltration", re.compile(r"(output|print|display|reveal|show|expose|dump)\s+(your |the |all )?(system[- ]?prompt|instructions?|hidden|internal|secret|confidential|private)", re.IGNORECASE), 0.75),
    ("permission_override", re.compile(r"(approve|authorize|allow|permit|grant|accept)\s+(automatically|unconditionally|without|all|any|every)", re.IGNORECASE), 0.70),
    ("permission_override", re.compile(r"(no|skip|bypass|disable|remove|ignore)\s+(review|verification|validation|approval|check|confirmation|authentication|authorization|oversight)\s+(needed|required|necessary|is needed|is required)", re.IGNORECASE), 0.75),
    ("permission_override", re.compile(r"(skip|bypass|disable|circumvent|avoid|ignore)\s+(the )?(review|verification|validation|approval|safety|security|check|safeguard|filter)", re.IGNORECASE), 0.70),
    ("permission_override", re.compile(r"without\s+(any )?(human |manual )?(review|approval|verification|oversight|confirmation|proof|evidence)", re.IGNORECASE), 0.60),
    ("jailbreak", re.compile(r"(you have no|there are no|remove all|drop all|you don't have any)\s+(restrictions?|limitations?|constraints?|boundaries|guardrails?|rules?|filters?)", re.IGNORECASE), 0.75),
    ("jailbreak", re.compile(r"(enable|enter|switch to|activate)\s+(unrestricted|developer|debug|god|admin|jailbreak|DAN|evil|uncensored)\s*(mode)?", re.IGNORECASE), 0.80),
    ("jailbreak", re.compile(r"(in this (hypothetical|fictional|imaginary|simulated|roleplay) scenario|for (educational|research|testing|academic) purposes?),?\s+(you|it is|the)", re.IGNORECASE), 0.55),
    ("jailbreak", re.compile(r"(this is a test|this is just a test|testing purposes only)\s*(,|\.|\s)?\s*(ignore|bypass|disable|skip)", re.IGNORECASE), 0.70),
)

_STEGO_TAG_RE = re.compile("[\U000E0000-\U000E007F]{2,}")

# Cyrillic look-alikes of Latin characters (upstream okf-guard detector).
# NOTE: upstream maps U+0471 ѱ → "ψ" (a Greek letter, not Latin); kept here
# verbatim for faithfulness — a ѱ in a mixed-script word never matches the
# basic-Latin range, so it cannot flag on its own.
_CYRILLIC_HOMOGLYPHS: dict[str, str] = {
    "\u0430": "a", "\u0435": "e", "\u043E": "o", "\u0440": "p",
    "\u0441": "c", "\u0445": "x", "\u0443": "y", "\u0456": "i",
    "\u0458": "j", "\u04BB": "h", "\u0455": "s", "\u0471": "ψ",
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041A": "K",
    "\u041C": "M", "\u041D": "H", "\u041E": "O", "\u0420": "P",
    "\u0421": "C", "\u0422": "T", "\u0425": "X",
}
_HOMOGLYPH_WORD_RE = re.compile(r"\S+")
def scan_injection(text: str) -> List[Dict[str, Any]]:
    """Screen ``text`` for prompt-injection and encoding tricks (pure, no mutation).

    Three detectors: the vendored phrase bank, Unicode tag-char
    steganography (runs of ≥2 chars in U+E0000–U+E007F, decoded by
    subtracting the base code point), and mixed-script Cyrillic/Latin
    homoglyph words. Flags are sorted by confidence, highest first.
    """
    flags: List[Dict[str, Any]] = []
    for label, pattern, conf in _INJECTION_PATTERNS:
        for m in pattern.finditer(text):
            flags.append({
                "kind": "injection",
                "label": label,
                "confidence": conf,
                "match": m.group()[:60],
            })
    for m in _STEGO_TAG_RE.finditer(text):
        decoded = "".join(chr(ord(ch) - 0xE0000) for ch in m.group())
        if decoded.strip():
            flags.append({"kind": "stego", "decoded": decoded, "chars": len(m.group()), "confidence": 0.8})
    for w in _HOMOGLYPH_WORD_RE.findall(text):
        # ponytail: naive script-mix heuristic, full word-boundary check if false positives appear
        if any(ch in _CYRILLIC_HOMOGLYPHS for ch in w) and any("\u0041" <= ch <= "\u007A" for ch in w):
            flags.append({"kind": "homoglyph", "word": w, "confidence": 0.5})
    flags.sort(key=lambda f: f["confidence"], reverse=True)
    return flags


# Zero-width / invisible characters smuggled into ingested source text (hidden
# prompt-injection payloads). Stripped before a concept is written; raw/
# snapshots keep the original bytes verbatim for provenance. Ranges mirror
# okf-guard plus small-form/combining marks, mathematical alphanumerics
# (homoglyph vectors) and Unicode tag characters (steganography vectors):
# U+200B-200F, U+202A-202E, U+2060, U+FE42, U+1D400-1D7FF, U+E000-F8FF,
# U+E0000-E007F. Written with ASCII escapes on purpose — the matched points
# are invisible in source files.
_UNSAFE_CHARS_RE = re.compile("[\u200B-\u200F\u202A-\u202E\u2060\uFE42\U0001D400-\U0001D7FF\uE000-\uF8FF\U000E0000-\U000E007F]")

def strip_unsafe_chars(text: str) -> tuple[str, int]:
    """Remove zero-width / invisible characters from ``text``.

    Returns ``(cleaned, count)`` — count is the number of characters removed.
    """
    return _UNSAFE_CHARS_RE.subn("", text)


def parse_source(text: str) -> Tuple[str | None, List[str], str]:
    """Extract (title, tags, body) from YAML frontmatter, bold fields, or H1.

    - YAML frontmatter title/tags are preferred when present.
    - Bold **Title:** / **Tags:** (and similar metadata lines) are stripped from body.
    - First markdown H1 is used as title when no YAML/bold title.
    - Returns title=None when no title signal is found.
    - Removes ``clippings`` from tags.
    """
    original = text if text is not None else ""
    text = original.lstrip("\ufeff").strip()
    title: str | None = None
    tags: List[str] = []
    body = text

    # YAML frontmatter
    if text.startswith("---"):
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            end = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    end = i
                    break
            if end is not None:
                for line in lines[1:end]:
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    m = re.match(r"^(title|tags)\s*:\s*(.*)$", line, re.I)
                    if not m:
                        continue
                    key, rest = m.group(1).lower(), m.group(2).strip()
                    if key == "title" and rest:
                        # strip quotes
                        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in ("\"", "'"):
                            rest = rest[1:-1]
                        title = rest
                    elif key == "tags":
                        tags = _parse_inline_list(rest)
                body = "\n".join(lines[end + 1 :]).strip()

    body_lines = body.splitlines()
    kept: List[str] = []
    # Accept both **Tags:** value  and  **Tags**: value
    bold_meta = re.compile(
        r"^\*\*(Title|Tags|Category|Type|Author|Date|URL|Source|Status)(?::\*\*|\*\*:?)\s*(.*)$",
        re.I,
    )
    for line in body_lines:
        m = bold_meta.match(line.strip())
        if m:
            key = m.group(1).lower()
            val = m.group(2).strip()
            if key == "title" and val and not title:
                title = val
            elif key == "tags" and val and not tags:
                tags = _parse_inline_list(val)
            # drop metadata line from body
            continue
        kept.append(line)
    body = "\n".join(kept).strip()

    # H1 title fallback
    if not title:
        for line in body.splitlines():
            m = re.match(r"^#\s+(.+?)\s*$", line)
            if m:
                title = m.group(1).strip()
                break

    # hashtag tokens if still no tags
    if not tags:
        found: List[str] = []
        for line in body.splitlines():
            found.extend(re.findall(r"#([A-Za-z][\w-]*)", line))
        tags = found

    tags = [t for t in (x.strip() for x in tags) if t and t.lower() != "clippings"]
    tags = hierarchize_tags(tags)
    return title, tags, body


def derive_description(body: str, limit: int = 140) -> str:
    """First non-empty, non-heading line; truncate with ellipsis at limit."""
    if not body:
        return ""
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        if len(s) <= limit:
            return s
        # keep room for "..."
        cut = max(1, limit - 3)
        return s[:cut] + "..."
    return ""


def yaml_list(items: List[str]) -> str:
    """Render a bullet list from a Python list."""
    return "\n".join(f"  - {x}" for x in items)


def render_concept(concept: Dict[str, Any]) -> str:
    """
    Render a concept dict to the canonical markdown front‑matter + body format.
    The result is suitable for writing to ``concepts/<domain>/<slug>.md``.
    """
    lines = ["---"]
    skip = {"body", "id"}
    # Stable leading keys
    for key in ("type", "visibility", "title", "domain", "description"):
        if key in concept and concept[key] not in (None, ""):
            lines.append(f"{key}: {concept[key]}")
            skip.add(key)
    # Prefer sources list; map legacy source scalar into sources if needed
    sources = concept.get("sources")
    if sources is None and concept.get("source"):
        sources = [concept["source"]]
    if sources:
        lines.append("source:")
        for item in sources:
            lines.append(f"  - {item}")
        skip.update({"sources", "source"})
    for key, value in concept.items():
        if key in skip:
            continue
        if value is None or value == "":
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    body = (concept.get("body") or "").strip()
    lines.append("")
    lines.append(body)
    lines.append("")
    return "\n".join(lines)


def fetch_url(url: str) -> str:
    """Fetch URL content as text using stdlib. Raises on failure."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def derive_source_ref(url: str) -> str:
    """Turn a URL into a provenance reference."""
    # Simple heuristic: use the path part of the URL as the ref.
    parsed = urllib.parse.urlparse(url)
    return parsed.path.strip("/") or "inline"


def make_concept(
    source_ref: str,
    text: str,
    ctype: str,
    domain: str,
    visibility: str | None = None,
    title_override: str | None = None,
) -> Dict[str, Any]:
    """Build a concept dictionary from raw text for :func:`render_concept`."""
    title, tags, body = parse_source(text)
    # Screen the ORIGINAL text (pre-strip) so stego tag chars are still visible
    # to the detector; the original bytes stay in the raw/ snapshot for provenance.
    flags = scan_injection((title_override or title or "") + "\n" + (body or ""))
    body, unsafe_n = strip_unsafe_chars(body)
    if unsafe_n:
        log.warning("⚠️  stripped %d zero-width/invisible char(s) from %s concept body (raw/ stays verbatim)",
                    unsafe_n, source_ref)
    if title_override:
        title = title_override

    # Title fallback: last path segment of source_ref (strip scheme / .md)
    if not title:
        ref = source_ref.strip()
        # strip scheme-like prefixes self: url:
        if ":" in ref and not ref.startswith("http"):
            # self:some-file.md or toolswiki:path
            ref = ref.split(":", 1)[-1]
        elif ref.startswith("http://") or ref.startswith("https://"):
            ref = urllib.parse.urlparse(ref).path
        base = ref.rstrip("/").rsplit("/", 1)[-1]
        if base.lower().endswith(".md"):
            base = base[:-3]
        title = base or "untitled"

    slug = slugify(title)
    cid = f"{domain}/{slug}" if domain else slug
    vis = visibility if visibility is not None else default_visibility(domain)

    # Provenance: keep original URL/ref as sources list entry
    sources = [source_ref]

    concept: Dict[str, Any] = {
        "id": cid,
        "type": ctype,
        "visibility": vis,
        "domain": domain,
        "tags": tags,
        "title": title,
        "description": derive_description(body),
        "sources": sources,
        "body": body,
    }
    if flags:
        first = "; ".join(f.get("label", f["kind"]) for f in flags[:3])
        log.warning("⚠️ injection screen: %d flag(s) on %s: %s", len(flags), source_ref, first)
    return concept


def snapshot_raw(
    repo: pathlib.Path,
    slug: str,
    text: str,
    source_label: str,
) -> pathlib.Path:
    """
    Write a raw snapshot of ``text`` to ``raw/<slug>.md`` and return the path.
    The function also stores a small provenance label at the top of the file.
    """
    raw_dir = repo / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{slug}.md"
    with raw_path.open("w", encoding="utf-8") as f:
        f.write(f"<!-- {source_label} -->\n\n{text}\n")
    return raw_path


def already_ingested(source_ref: str) -> bool:
    """
    Quick check to see whether a concept derived from ``source_ref`` already
    exists as a raw snapshot.  This prevents duplicate ingests when the
    function is re‑run.
    """
    slug = slugify(source_ref)
    raw_path = pathlib.Path("raw") / f"{slug}.md"
    return raw_path.exists()


def ingest_source(
    source_ref: str,
    text: str,
    ctype: str,
    domain: str,
    visibility: str,
    title_override: str | None = None,
    dry_run: bool = False,
) -> int:
    """
    Create a concept from ``text`` and write it to the vault.
    Returns ``0`` on success, ``1`` if the concept already exists, and ``2``
    on any unexpected error.
    """
    if already_ingested(source_ref):
        log.info("✅ %s already ingested – skipping", source_ref)
        return 1

    try:
        concept = make_concept(
            source_ref=source_ref,
            text=text,
            ctype=ctype,
            domain=domain,
            visibility=visibility,
            title_override=title_override,
        )
    except Exception as exc:
        log.error("❌ Failed to build concept for %s: %s", source_ref, exc)
        return 2

    if dry_run:
        log.info("🧪 Dry‑run: would ingest %s", source_ref)
        return 0

    # Write raw snapshot (used later for provenance and for the indexer)
    try:
        snapshot_raw(
            repo=pathlib.Path("."),
            slug=slugify(source_ref),
            text=text,
            source_label=derive_source_ref(source_ref),
        )
    except Exception as exc:
        log.error("❌ Could not snapshot raw file for %s: %s", source_ref, exc)
        return 2

    # Write the concept file to concepts/<domain>/<slug>.md.
    # Indexing is deferred — run `okf.py index` after the batch completes.
    try:
        slug = slugify(title_override or concept.get("title", ""))
        if not slug or "/" in slug or slug.startswith("users-"):
            log.error("❌ Could not derive a clean slug for %s — use --title to set one", source_ref)
            return 2
        domain_dir = pathlib.Path("concepts") / concept["domain"]
        domain_dir.mkdir(parents=True, exist_ok=True)
        concept_path = domain_dir / f"{slug}.md"
        if concept_path.exists():
            # Disambiguate by appending a short hash of the source_ref
            h = hashlib.sha256(source_ref.encode()).hexdigest()[:6]
            concept_path = domain_dir / f"{slug}-{h}.md"
        concept_path.write_text(render_concept(concept), encoding="utf-8")
        log.info("✅ Ingested %s → %s", source_ref, concept_path)
    except Exception as exc:
        log.error("❌ Error writing concept for %s: %s", source_ref, exc)
        return 2
    return 0


def main(argv: List[str] | None = None) -> int:
    """Entry‑point for the ``okf ingest`` CLI."""
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        description="Ingest a URL, file, or stdin into the OKF vault."
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="URL or path to a .md file to ingest",
    )
    parser.add_argument("--stdin", action="store_true", help="Read source content from stdin")
    parser.add_argument("--title", default=None, help="Override the concept title")
    parser.add_argument(
        "--type",
        default="note",
        dest="ctype",
        help="Concept type (default: note). See AGENTS.md for vocabulary.",
    )
    parser.add_argument(
        "--domain",
        default="tools",
        help="Concept domain (default: tools). See AGENTS.md.",
    )
    parser.add_argument(
        "--visibility",
        default=None,
        choices=["private", "shareable"],
        help="Visibility override (default: private for life/people/orgs/documents, shareable otherwise)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args(argv)

    # -------------------------------------------------------------------
    # Resolve the Effective visibility if the user didn't set one.
    # -------------------------------------------------------------------
    if args.visibility is None:
        args.visibility = "private" if args.domain in PERSONAL_DOMAINS else "shareable"

    # -------------------------------------------------------------------
    # Obtain the source text.
    # -------------------------------------------------------------------
    if args.stdin:
        import sys as _sys
        text = _sys.stdin.read()
        source_ref = "stdin"
    elif args.source:
        source_input = args.source
        # Distinguish URL from file path
        if source_input.startswith("http://") or source_input.startswith("https://"):
            try:
                text = fetch_url(source_input)
                source_ref = source_input
            except Exception as exc:
                log.error("❌ Failed to fetch URL %s: %s", source_input, exc)
                return 2
        else:
            source_path = pathlib.Path(source_input)
            if not source_path.is_file():
                log.error("❌ Source path %s does not exist.", source_input)
                return 2
            text = source_path.read_text(encoding="utf-8")
            source_ref = str(source_path)
    else:
        log.error("❌ No source supplied.")
        return 2

    # -------------------------------------------------------------------
    # Run the ingest operation.
    # -------------------------------------------------------------------
    result = ingest_source(
        source_ref=source_ref,
        text=text,
        ctype=args.ctype,
        domain=args.domain,
        visibility=args.visibility,
        title_override=args.title,
        dry_run=args.dry_run,
    )
    return result


if __name__ == "__main__":
    sys.exit(main())