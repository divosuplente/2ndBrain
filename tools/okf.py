#!/usr/bin/env python3
"""okf — tooling for the OKF brain corpus.

Subcommands:
  index        Walk concepts/, build tools/index.json, and regenerate provenance/map.{json,md}.
  search       Rank concepts for a query (BM25) with optional --visibility/--type/--domain filters.
  lint         Report missing required frontmatter, broken links, orphans, duplicates, privacy and tag-convention issues.
  backlinks    List concepts with inbound links to one concept (id, path, or unique slug).
  affected     Transitive inbound links (what would need review if X changes); --git BASE seeds from diff.
  relink       Rewrite intra-corpus markdown links to canonical /concepts/<id>.md paths.
  sql          Run ad-hoc SQL queries over the corpus (requires: pip install "okf-tools[sql]").
  doctor       Agent-surface integrity (ICM files, AGENTS dup, AAAK parity, routing).
  icm-sync     Diff skills/ vs CONTEXT routing; optional --write.
  log          Verify the hash chain over log.md; --seal folds in new appends, --bootstrap first-run seal.
  status       Single gate: lint + doctor + log chain → READY / NEEDS ATTENTION / BLOCKED.
  export       Export shareable concepts + their referenced raw/attachments (private never exported).
  view         Build the index, serve locally, and open the graph viewer in a browser.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import shutil
import sys
import subprocess
import tempfile
from datetime import datetime, timezone
from fnmatch import fnmatch
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote
from xml.sax.saxutils import escape as _xml_escape

import okf_normalize_dates

# --- repo layout -----------------------------------------------------------

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
CONCEPTS_DIR = REPO_ROOT / "concepts"
INDEX_PATH = TOOLS_DIR / "index.json"
PROV_JSON = REPO_ROOT / "provenance" / "map.json"
PROV_MD = REPO_ROOT / "provenance" / "map.md"
SQL_CACHE = TOOLS_DIR / "sql_cache.duckdb"

REQUIRED_FIELDS = ("type", "visibility")
VALID_VISIBILITY = ("private", "shareable")
PERSONAL_DOMAINS = {"life", "people", "orgs", "documents", "work"}  # default private (D-015)
LOOPBACK = "127.0.0.1"  # local-only bind host for `okf view`
LOG_PATH = REPO_ROOT / "log.md"
LOG_STATE = REPO_ROOT / "log.chain.jsonl"  # committed — external anchor for the log seal

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# attachment refs in concept bodies: markdown / HTML / bare links to raw/attachments/<name>
_ATTACH_REF_RE = re.compile(r"raw/attachments/([^)\s>'\"`]*)")
_FM_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
_FM_ITEM_RE = re.compile(r"^\s*-\s+(.*)$")


# --- frontmatter parsing (minimal YAML subset) -----------------------------

def split_frontmatter(text: str):
    """Return (frontmatter_dict, body_str). Empty dict if no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm = parse_frontmatter("\n".join(lines[1:end]))
    body = "\n".join(lines[end + 1:])
    return fm, body


def _strip_scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def parse_frontmatter(block: str) -> dict:
    """Parse the small YAML subset used by OKF concept frontmatter.

    Supports scalars, inline lists ([a, b]), and block lists (- item).
    Full-line comments (starting with #) and blank lines are ignored.
    """
    data: dict = {}
    last_key = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = _FM_ITEM_RE.match(raw)
        if item and last_key is not None:
            data.setdefault(last_key, [])
            if not isinstance(data[last_key], list):
                data[last_key] = []
            data[last_key].append(_strip_scalar(item.group(1)))
            continue
        m = _FM_KEY_RE.match(raw)
        if not m:
            continue
        key, rest = m.group(1), m.group(2).strip()
        last_key = key
        if rest == "":
            data[key] = None  # may become a block list on following lines
        elif rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            data[key] = [_strip_scalar(x) for x in inner.split(",")] if inner else []
        else:
            data[key] = _strip_scalar(rest)
    return data


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v not in (None, "")]
    return [value]


def tokenize(text: str):
    return _TOKEN_RE.findall((text or "").lower())


# --- concept loading -------------------------------------------------------

class Concept:
    __slots__ = ("id", "path", "fm", "body", "links", "tf", "length")

    def __init__(self, cid, path, fm, body):
        self.id = cid
        self.path = path
        self.fm = fm
        self.body = body
        self.links = extract_links(body, cid)
        terms = tokenize(" ".join([
            str(fm.get("title") or ""),
            str(fm.get("description") or ""),
            " ".join(as_list(fm.get("tags"))),
            body,
        ]))
        tf: dict = {}
        for t in terms:
            if len(t) < 2:
                continue
            tf[t] = tf.get(t, 0) + 1
        self.tf = tf
        self.length = sum(tf.values())


def concept_id_from_path(path: Path) -> str:
    return path.relative_to(CONCEPTS_DIR).with_suffix("").as_posix()


def extract_links(body: str, source_id: str):
    """Return a sorted list of concept ids this body links to (best-effort)."""
    out = set()
    for target in _LINK_RE.findall(body):
        target = target.strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path_part = target.split("#", 1)[0].split("?", 1)[0]
        if not path_part.endswith(".md"):
            continue
        # Collapse accidental /concepts/id.md.md links
        while path_part.endswith(".md.md"):
            path_part = path_part[:-3]
        if path_part.startswith("/concepts/"):
            cid = path_part[len("/concepts/"):-len(".md")]
        elif path_part.startswith("concepts/"):
            cid = path_part[len("concepts/"):-len(".md")]
        elif path_part.startswith("/"):
            continue  # bundle-relative but outside concepts/
        else:
            # relative to the source concept's directory
            base = (CONCEPTS_DIR / source_id).parent
            resolved = (base / path_part).resolve()
            try:
                cid = resolved.relative_to(CONCEPTS_DIR).with_suffix("").as_posix()
            except ValueError:
                continue
        # Only collapse known bad mid-path doubles (e.g. tools/agents/agents/foo).
        # Do NOT collapse domain hubs (tools/tools) or leaf hubs (learning/keto/keto).
        if "/agents/agents/" in cid:
            alt = cid.replace("/agents/agents/", "/agents/")
            cid = alt
        out.add(cid)
    return sorted(out)


def load_concepts():
    concepts = []
    if not CONCEPTS_DIR.exists():
        return concepts
    for path in sorted(CONCEPTS_DIR.rglob("*.md")):
        if path.name in ("index.md", "log.md", "_template.md"):
            continue  # reserved filenames — not concept documents
        text = path.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        concepts.append(Concept(concept_id_from_path(path), path, fm, body))
    return concepts


# --- index -----------------------------------------------------------------

def build_index(concepts):
    df: dict = {}
    docs = []
    for c in concepts:
        for term in c.tf:
            df[term] = df.get(term, 0) + 1
        docs.append({
            "id": c.id,
            "path": c.path.relative_to(REPO_ROOT).as_posix(),
            "type": c.fm.get("type"),
            "visibility": c.fm.get("visibility"),
            "domain": c.fm.get("domain"),
            "title": c.fm.get("title") or c.id.rsplit("/", 1)[-1],
            "description": c.fm.get("description") or "",
            "tags": as_list(c.fm.get("tags")),
            "source": as_list(c.fm.get("source")),
            "links": c.links,
            "tf": c.tf,
            "length": c.length,
        })
    total_len = sum(d["length"] for d in docs)
    avgdl = (total_len / len(docs)) if docs else 0.0
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(docs),
        "avgdl": avgdl,
        "df": df,
        "concepts": docs,
    }


def write_provenance(concepts):
    entries = {}
    for c in concepts:
        entries[c.id] = {
            "title": c.fm.get("title") or c.id.rsplit("/", 1)[-1],
            "sources": as_list(c.fm.get("source")),
        }
    PROV_JSON.write_text(json.dumps({
        "version": "1",
        "description": ("Concept -> source provenance. GENERATED by "
                        "`python3 tools/okf.py index` from each concept's `source` "
                        "frontmatter. Do not edit by hand; edit the concept's frontmatter instead."),
        "concepts": entries,
    }, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Provenance Map",
        "",
        ("**GENERATED** by `python3 tools/okf.py index` from each concept's `source` "
         "frontmatter. Do not edit by hand — edit the concept's frontmatter instead, then "
         "re-run the indexer."),
        "",
        ("Each row maps a concept to its source(s). Source refs may be historical "
         "origin paths (`pka:`, `toolswiki:`), URLs (`https://...`), or `self:` for "
         "vault-synthesized content. Historical snapshots live under `raw/`."),
        "",
        "## Concepts",
    ]
    if not entries:
        lines.append("(none yet — populated on first ingest)")
    else:
        for cid in sorted(entries):
            srcs = entries[cid]["sources"]
            srcs_str = ", ".join(f"`{s}`" for s in srcs) if srcs else "_(no provenance recorded)_"
            lines.append(f"- [{entries[cid]['title']}](/concepts/{cid}.md) — {srcs_str}")
    PROV_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_domain_indexes(concepts):
    """Generate per-domain index.md files under concepts/<domain>/index.md."""
    by_domain: dict = {}
    for c in concepts:
        d = c.fm.get("domain") or "uncategorized"
        by_domain.setdefault(d, []).append(c)
    for domain, dconcepts in sorted(by_domain.items()):
        dpath = CONCEPTS_DIR / domain / "index.md"
        dpath.parent.mkdir(parents=True, exist_ok=True)
        dconcepts.sort(key=lambda c: c.fm.get("title") or c.id.rsplit("/", 1)[-1])
        lines = [f"# {domain.capitalize()}", ""]
        for c in dconcepts:
            title = c.fm.get("title") or c.id.rsplit("/", 1)[-1]
            desc = c.fm.get("description") or ""
            lines.append(f"- [{title}](/{c.path.relative_to(REPO_ROOT).as_posix()}) — {desc}")
        dpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Domain indexes -> {len(by_domain)} files under concepts/*/index.md")

def cmd_index(args):
    concepts = load_concepts()
    index = build_index(concepts)
    INDEX_PATH.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    write_provenance(concepts)
    write_domain_indexes(concepts)
    print(f"Indexed {index['count']} concept(s) -> {INDEX_PATH.relative_to(REPO_ROOT)}")
    print(f"Provenance -> {PROV_JSON.relative_to(REPO_ROOT)}, {PROV_MD.relative_to(REPO_ROOT)}")
    return 0


# --- search ----------------------------------------------------------------

def load_index():
    if not INDEX_PATH.exists():
        return build_index(load_concepts())
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def bm25_search(index, query, k1=1.5, b=0.75):
    q_terms = [t for t in tokenize(query) if len(t) >= 2]
    n = index["count"] or 1
    avgdl = index["avgdl"] or 1.0
    df = index["df"]
    results = []
    for doc in index["concepts"]:
        score = 0.0
        dl = doc["length"] or 1
        for term in q_terms:
            f = doc["tf"].get(term, 0)
            if f == 0:
                continue
            idf = math.log(1 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            results.append((score, doc))
    results.sort(key=lambda x: x[0], reverse=True)
    return results


def apply_filters(results, visibility=None, type=None, domain=None, limit=10):
    out = []
    for score, doc in results:
        if visibility and doc.get("visibility") != visibility:
            continue
        if type and doc.get("type") != type:
            continue
        if domain and doc.get("domain") != domain:
            continue
        out.append((score, doc))
        if len(out) >= limit:
            break
    return out


def cmd_search(args):
    index = load_index()
    results = bm25_search(index, args.query)
    out = apply_filters(results, args.visibility, args.type, args.domain, args.limit)
    if args.json:
        print(json.dumps([
            {"score": round(s, 4), "id": d["id"], "path": d["path"],
             "title": d["title"], "type": d["type"], "visibility": d["visibility"]}
            for s, d in out
        ], indent=2))
        return 0
    if not out:
        print("No matches.")
        return 0
    for score, doc in out:
        print(f"{score:6.3f}  {doc['title']}  [{doc.get('type')}/{doc.get('visibility')}]")
        print(f"        {doc['path']}")
    return 0


# --- lint ------------------------------------------------------------------

VALID_STATUS = ("active", "dormant", "archived")
_V02_STATUS = {"stable", "draft", "deprecated"}  # spec v0.2 values the vault retired
_ACTOR_RE = re.compile(r"^(?:human|agent|process):[A-Za-z0-9._-]+$")
_GEN_AT_RE = re.compile(r"at:\s*([^,}\s]+)")
_GEN_BY_RE = re.compile(r"by:\s*([^,}\s]+)")
_ISO_FULL = okf_normalize_dates.ISO_FULL  # keep in sync with tools/okf_normalize_dates.py

def _fm_actors(generated, verified):
    """Yield parseable actor slugs from generated/verified frontmatter values."""
    if isinstance(generated, dict):
        by = generated.get("by")
        if by:
            yield str(by)
    elif isinstance(generated, str):
        m = _GEN_BY_RE.search(generated)
        if m:
            yield m.group(1)
    for item in as_list(verified):
        if isinstance(item, dict):
            by = item.get("by")
            if by:
                yield str(by)
        elif isinstance(item, str):
            m = _GEN_BY_RE.search(item)
            if m:
                yield m.group(1)

def lint_concepts(concepts):
    findings = []
    tag_counts: dict = {}
    ids = {c.id for c in concepts}
    inbound: dict = {c.id: 0 for c in concepts}
    titles: dict = {}

    for c in concepts:
        for field in REQUIRED_FIELDS:
            if not c.fm.get(field):
                findings.append({"level": "error", "concept": c.id,
                                 "kind": "missing-field", "detail": f"missing required `{field}`"})
        vis = c.fm.get("visibility")
        if vis and vis not in VALID_VISIBILITY:
            findings.append({"level": "error", "concept": c.id, "kind": "bad-visibility",
                             "detail": f"visibility `{vis}` not in {VALID_VISIBILITY}"})
        # privacy: personal-domain concept marked shareable — warn so override is confirmed
        domain = c.fm.get("domain")
        if domain in PERSONAL_DOMAINS and vis == "shareable":
            findings.append({"level": "warn", "concept": c.id, "kind": "privacy",
                             "detail": f"personal domain `{domain}` is shareable — confirm override is intentional"})
        # tag conventions (contract: ≥1 tag; lowercase-hyphenated; no domain- or clippings-redundant)
        tags = c.fm.get("tags") or []
        if not tags:
            findings.append({"level": "info", "concept": c.id, "kind": "missing-tag",
                             "detail": "no tags (contract: at least one per concept)"})
        else:
            dom_forms = set()
            if domain:
                dom_forms.add(domain)
                dom_forms.add(domain[:-1] if domain.endswith("s") and len(domain) > 3 else domain + "s")
            for t in tags:
                ts = t.strip() if isinstance(t, str) else None
                if ts is None or ts.startswith("- "):
                    findings.append({"level": "warn", "concept": c.id, "kind": "malformed-tag",
                                     "detail": f"tag {t!r} is not a flat scalar (likely a malformed `- - ` block entry)"})
                    continue
                if not ts:
                    continue
                tag_counts[ts] = tag_counts.get(ts, 0) + 1
                if ts != ts.lower() or "_" in ts or "--" in ts or ts == "clippings":
                    findings.append({"level": "warn", "concept": c.id, "kind": "bad-tag",
                                     "detail": f"tag `{ts}` breaks convention (lowercase, hyphenated, no underscore/clippings)"})
                if ts in dom_forms:
                    findings.append({"level": "info", "concept": c.id, "kind": "domain-tag",
                                     "detail": f"tag `{ts}` is redundant with domain `{domain}`"})
        title = (c.fm.get("title") or "").strip().lower()
        if title:
            titles.setdefault(title, []).append(c.id)
        for target in c.links:
            if target in ids:
                inbound[target] += 1
            else:
                findings.append({"level": "warn", "concept": c.id, "kind": "broken-link",
                                 "detail": f"links to missing concept `{target}`"})

        # status vocabulary (vault extension: active|dormant|archived)
        status = c.fm.get("status")
        if status and not isinstance(status, str):
            findings.append({"level": "error", "concept": c.id, "kind": "bad-status",
                             "detail": f"status {status!r} is not a flat scalar"})
        elif status and status not in VALID_STATUS:
            detail = f"status `{status}` not in {VALID_STATUS}"
            if status in _V02_STATUS:
                detail += " (okf v0.2 value — vault uses active|dormant|archived)"
            findings.append({"level": "error", "concept": c.id, "kind": "bad-status",
                             "detail": detail})
        # trust: verified must be {by,at} events; trust_tier is a retired local extension
        verified = c.fm.get("verified")
        if verified and not isinstance(verified, list):
            findings.append({"level": "warn", "concept": c.id, "kind": "malformed-trust",
                             "detail": f"`verified` must be a list of events ({{by, at}}), got {verified!r}"})
        if "trust_tier" in c.fm:
            findings.append({"level": "warn", "concept": c.id, "kind": "malformed-trust",
                             "detail": "`trust_tier` is a non-conformant local extension (spec §5.3 uses verified events)"})
        # generated.at must be ISO-8601 with explicit offset (spec §5)
        generated = c.fm.get("generated")
        if generated:
            if isinstance(generated, dict):
                gen_at = generated.get("at")
            else:
                m = _GEN_AT_RE.search(str(generated))
                gen_at = m.group(1) if m else None
            if not gen_at:
                findings.append({"level": "warn", "concept": c.id, "kind": "bad-generated-at",
                                 "detail": "generated without at"})
            elif not _ISO_FULL.match(str(gen_at)):
                findings.append({"level": "warn", "concept": c.id, "kind": "bad-generated-at",
                                 "detail": f"generated.at `{gen_at}` is not ISO-8601 with offset"})
        # legacy v0.1 key
        if "timestamp" in c.fm:
            findings.append({"level": "info", "concept": c.id, "kind": "legacy-timestamp",
                             "detail": "v0.1 legacy key, superseded by generated (spec §13.1)"})
        # actor format: generated.by and verified[].by must be (human|agent|process):slug
        for actor in _fm_actors(generated, verified):
            if not _ACTOR_RE.match(actor):
                findings.append({"level": "info", "concept": c.id, "kind": "actor-format",
                                 "detail": f"actor `{actor}` does not match (human|agent|process):slug"})

    for cid, count in inbound.items():
        if count == 0:
            findings.append({"level": "info", "concept": cid, "kind": "orphan",
                             "detail": "no inbound links"})
    for title, owners in titles.items():
        if len(owners) > 1:
            findings.append({"level": "warn", "concept": ", ".join(sorted(owners)),
                             "kind": "duplicate", "detail": f"shared title '{title}'"})
    pair_seen = set()
    for t in sorted(tag_counts):
        other = t[:-1] if t.endswith("s") else t + "s"
        if other in tag_counts:
            a, b = sorted((t, other))
            if (a, b) in pair_seen:
                continue
            pair_seen.add((a, b))
            findings.append({"level": "info", "concept": f"{a}/{b}", "kind": "plural-tag",
                             "detail": f"singular and plural tag forms coexist: '{a}'({tag_counts[a]}) vs '{b}'({tag_counts[b]})"})
    return findings


def cmd_lint(args):
    concepts = load_concepts()
    findings = lint_concepts(concepts)
    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        if not findings:
            print(f"lint: {len(concepts)} concept(s), no findings.")
        else:
            order = {"error": 0, "warn": 1, "info": 2}
            for f in sorted(findings, key=lambda x: order.get(x["level"], 9)):
                print(f"[{f['level']:5}] {f['kind']}: {f['concept']} — {f['detail']}")
            counts = {}
            for f in findings:
                counts[f["level"]] = counts.get(f["level"], 0) + 1
            print(f"\n{len(concepts)} concept(s); "
                  + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    # Never hard-fail unless --strict and there are errors.
    if args.strict and any(f["level"] == "error" for f in findings):
        return 1
    return 0


# --- backlinks -------------------------------------------------------------

def resolve_concept_arg(arg: str, ids):
    """Resolve a cli concept arg (id, path, or unique slug) to a concept id."""
    a = arg.strip()
    for prefix in ("/concepts/", "concepts/", "/"):
        if a.startswith(prefix):
            a = a[len(prefix):]
    if a.endswith(".md"):
        a = a[:-len(".md")]
    if a in ids:
        return a, []
    slug = a.rsplit("/", 1)[-1]
    matches = sorted(i for i in ids if i.rsplit("/", 1)[-1] == slug)
    if len(matches) == 1:
        return matches[0], [f"resolved slug '{slug}'"]
    if len(matches) > 1:
        return None, matches
    return None, []


def cmd_backlinks(args):
    """List concepts that link to the given concept (reverse link lookup)."""
    concepts = load_concepts()
    ids = {c.id for c in concepts}
    cid, note = resolve_concept_arg(args.concept, ids)
    if cid is None:
        if not args.json:
            if note:
                print(f"ambiguous: {', '.join(note)}", file=sys.stderr)
            else:
                print(f"error: concept not found: {args.concept}", file=sys.stderr)
        return 1
    inbound = sorted((c for c in concepts if cid in c.links and c.id != cid), key=lambda c: c.id)
    if args.json:
        print(json.dumps([
            {"id": c.id, "path": c.path.relative_to(REPO_ROOT).as_posix(),
             "title": c.fm.get("title") or c.id.rsplit("/", 1)[-1]}
            for c in inbound
        ], indent=2))
        return 0
    for n in note:
        print(f"(note) {n}")
    print(f"{len(inbound)} inbound link(s) to /concepts/{cid}.md:")
    for c in inbound:
        title = c.fm.get("title") or c.id.rsplit("/", 1)[-1]
        print(f"  {c.id} — {title}")
    return 0


# --- affected (transitive backlinks) ----------------------------------------

def _transitive_inbound(link_map, roots):
    """All concept ids that transitively link into any root (roots excluded)."""
    reverse: dict = {}
    for cid, links in link_map.items():
        for t in links:
            reverse.setdefault(t, []).append(cid)
    seen = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for src in reverse.get(cur, ()):
            if src not in seen:
                seen.add(src)
                stack.append(src)
    seen -= roots
    return seen


def _git_changed_concepts(base):
    """Ids under concepts/ changed (diff) or untracked vs BASE; None on git failure."""
    try:
        changed = subprocess.run(["git", "diff", "--name-only", base, "--", "concepts/"],
                                 cwd=REPO_ROOT, check=True,
                                 capture_output=True, text=True).stdout
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", "concepts/"],
                                   cwd=REPO_ROOT, check=True,
                                   capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    ids = set()
    for line in (changed + untracked).splitlines():
        line = line.strip()
        if not line.endswith(".md") or "concepts/" not in line:
            continue
        name = line.rsplit("/", 1)[-1]
        if name in ("index.md", "log.md", "_template.md"):
            continue
        ids.add(line[len("concepts/"):-len(".md")])
    return ids


def cmd_affected(args):
    """List concepts that transitively link to the given concept (+ optional --git seeds)."""
    concepts = load_concepts()
    ids = {c.id for c in concepts}
    cid, note = resolve_concept_arg(args.concept, ids)
    if cid is None:
        if not args.json:
            if note:
                print(f"ambiguous: {', '.join(note)}", file=sys.stderr)
            else:
                print(f"error: concept not found: {args.concept}", file=sys.stderr)
        return 1
    roots = {cid}
    git_ids = None
    if args.git:
        git_ids = _git_changed_concepts(args.git)
        if git_ids is None:
            print(f"error: git failed to list changed concepts ({args.git})", file=sys.stderr)
            return 1
        roots |= git_ids & ids
    result = _transitive_inbound({c.id: c.links for c in concepts}, roots)
    by_id = {c.id: c for c in concepts}
    hit = sorted((by_id[i] for i in result), key=lambda c: c.id)
    if args.json:
        print(json.dumps([
            {"id": c.id, "path": c.path.relative_to(REPO_ROOT).as_posix(),
             "title": c.fm.get("title") or c.id.rsplit("/", 1)[-1]}
            for c in hit
        ], indent=2))
        return 0
    for n in note:
        print(f"(note) {n}")
    if git_ids is not None:
        for seed in sorted(git_ids):
            n_direct = sum(1 for c in concepts if seed in c.links and c.id != seed)
            print(f"  {seed}: {n_direct} inbound(s), {len(hit)} transitive total")
    print(f"{len(hit)} transitive inbound link(s) to /concepts/{cid}.md:")
    for c in hit:
        title = c.fm.get("title") or c.id.rsplit("/", 1)[-1]
        print(f"  {c.id} — {title}")
    return 0


# --- view (local server + browser) -----------------------------------------

def viewer_url(port):
    return f"http://{LOOPBACK}:{port}/tools/viewer.html"


def cmd_view(args):
    """Build the index, serve the repo locally, and open the graph viewer."""
    import functools
    import http.server
    import webbrowser

    if not args.no_index:
        concepts = load_concepts()
        index = build_index(concepts)
        INDEX_PATH.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        write_provenance(concepts)
        print(f"Indexed {index['count']} concept(s).")

    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # silence per-request logging
            pass

    handler = functools.partial(_QuietHandler, directory=str(REPO_ROOT))
    try:
        httpd = http.server.ThreadingHTTPServer((LOOPBACK, args.port), handler)
    except OSError:
        # Requested port busy/unavailable -> let the OS pick a free one.
        httpd = http.server.ThreadingHTTPServer((LOOPBACK, 0), handler)
    port = httpd.server_address[1]
    url = viewer_url(port)
    print(f"OKF graph viewer: {url}")
    print("Press Ctrl+C to stop.")
    print(f"Note: serving on loopback ({LOOPBACK}) only — not exposed to the network.",
          file=sys.stderr)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0

# --- suggest-links (find missing semantic cross-links) ---------------------


def _tag_set(fm: dict) -> set:
    """Extract lowercase tag set from frontmatter."""
    return {t.strip().lower() for t in as_list(fm.get("tags")) if t.strip()}


def suggest_links(index, max_pairs: int = 100, min_tag_overlap: int = 2, min_score: float = 0.5, concept_id: str = None):
    """Find concept pairs that should probably be linked but aren't.
    
    When concept_id is set, only returns pairs involving that concept (fast, ingest-time use).
    When concept_id is None, scans all orphans + tag pairs (vault-wide audit).
    """
    concepts = {d["id"]: d for d in index["concepts"]}
    ids = set(concepts.keys())
    
    if concept_id and concept_id not in concepts:
        return []
    
    # Build existing-link set for quick lookup (undirected)
    existing = set()
    for d in index["concepts"]:
        for t in d["links"]:
            if t in ids:
                pair = tuple(sorted([d["id"], t]))
                existing.add(pair)
    
    # Build tag inverted index (skip in scoped mode — we iterate directly instead)
    tag_index: dict = {}
    if not concept_id:
        for cid, d in concepts.items():
            for tag in _tag_set(d):
                tag_index.setdefault(tag, []).append(cid)
    
    # Phase 1: tag-based candidate pairs
    tag_candidates: dict = {}  # (id_a, id_b) -> shared_tags
    if concept_id:
        # Scoped: iterate all concepts, check tag overlap with target
        target_tags = _tag_set(concepts[concept_id])
        for cid, d in concepts.items():
            if cid == concept_id:
                continue
            pair = tuple(sorted([concept_id, cid]))
            if pair in existing:
                continue
            shared = target_tags & _tag_set(d)
            if len(shared) >= min_tag_overlap:
                tag_candidates[pair] = shared
    else:
        for tag, cids in tag_index.items():
            if len(cids) < 2:
                continue
            for i in range(len(cids)):
                for j in range(i + 1, len(cids)):
                    pair = tuple(sorted([cids[i], cids[j]]))
                    if pair in existing:
                        continue
                    tag_candidates[pair] = tag_candidates.get(pair, set()) | {tag}
    # Filter out same-stem cross-domain duplicates (e.g. learning/skills/X ↔ tools/agents/X)
    def _slug_match(a: str, b: str) -> bool:
        return a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1]
    
    scored: dict = {}  # pair -> (score, reason)
    
    # Score tag candidates
    for pair, shared_tags in tag_candidates.items():
        if len(shared_tags) < min_tag_overlap:
            continue
        id_a, id_b = pair
        if _slug_match(id_a, id_b):
            continue
        weight = len(shared_tags) * 10.0  # strong signal
        reason = f"tags: {', '.join(sorted(shared_tags))}"
        scored[pair] = (weight, reason)
    # Phase 2: BM25 cross-scoring
    # When concept_id set: score that concept against all others
    # When global: score orphans (0 inbound) against all others
    n = index["count"] or 1
    avgdl = index["avgdl"] or 1.0
    df = index["df"]
    k1, b = 1.5, 0.75
    
    def _make_query(d: dict) -> str:
        parts = []
        title = (d.get("title") or "").strip()
        desc = (d.get("description") or "").strip()
        if title:
            parts.append(title)
        if desc:
            parts.append(desc)
        parts.extend(_tag_set(d))
        return " ".join(parts)
    
    if concept_id:
        targets = {concept_id: _make_query(concepts[concept_id])}
    else:
        inbound = {cid: 0 for cid in ids}
        for d in index["concepts"]:
            for t in d["links"]:
                if t in ids:
                    inbound[t] += 1
        targets = {cid: _make_query(concepts[cid]) for cid in ids if inbound[cid] == 0}
    
    for target_id, query in targets.items():
        if not query.strip():
            continue
        q_terms = [t for t in tokenize(query) if len(t) >= 2]
        if not q_terms:
            continue
        
        for cid, d in concepts.items():
            if cid == target_id:
                continue
            pair = tuple(sorted([target_id, cid]))
            if pair in existing:
                continue
            if _slug_match(target_id, cid):
                continue
            
            score = 0.0
            dl = d["length"] or 1
            for term in q_terms:
                f = d["tf"].get(term, 0)
                if f == 0:
                    continue
                idf = math.log(1 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
                score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
            
            if score > min_score:
                pair_key = pair
                if pair_key not in scored or scored[pair_key][0] < score:
                    d_terms = set(d["tf"].keys())
                    q_set = set(q_terms)
                    shared = d_terms & q_set
                    reason = f"BM25: {score:.2f}; terms: {', '.join(sorted(shared)[:5])}"
                    scored[pair_key] = (score, reason)
    
    # Rank and return top N
    results = sorted(scored.items(), key=lambda x: -x[1][0])
    return [(score, id_a, id_b, reason) for (id_a, id_b), (score, reason) in results[:max_pairs]]


def cmd_suggest_links(args):
    """Suggest missing cross-links between concepts based on tags and BM25 similarity."""
    idx = load_index()
    pairs = suggest_links(
        idx,
        max_pairs=args.max,
        min_tag_overlap=args.min_tags,
        min_score=args.min_score,
        concept_id=getattr(args, "concept", None),
    )
    
    if not pairs:
        print("No candidate links found above thresholds.")
        return 0
    
    if args.json:
        out = [{"score": s, "a": a, "b": b, "reason": r} for s, a, b, r in pairs]
        print(json.dumps(out, indent=2))
    else:
        print(f"Top {len(pairs)} missing link candidates:")
        print()
        for rank, (score, id_a, id_b, reason) in enumerate(pairs, 1):
            print(f"{rank:3d}. score={score:.2f}")
            print(f"     {id_a}")
            print(f"     ↔ {id_b}")
            print(f"     {reason}")
            print()
    
    print(f"Hint: okf suggest-links --json | jq '.[].a' | xargs -I{{}} echo '{{}}' > /tmp/pairs.txt")
    return 0


# --- relink (canonicalize intra-corpus links) ------------------------------

_RELINK_RE = re.compile(r"(\[[^\]]*\]\()([^)\s]+)(\))")
# Original-structure path segment -> canonical domain (for disambiguating slugs).
DOMAIN_HINTS = {
    "ecosystem": "tools", "terminals": "tools", "orchestration": "tools",
    "governance": "tools", "execution-surfaces": "tools", "ai-coding-agents": "tools",
    "protocols": "tools", "tools": "tools",
    "specs": "specs", "skills": "skills", "learning": "learning",
    "topics": "life", "habits": "life", "goals": "life", "projects": "life",
    "resources": "learning", "life": "life",
    "people": "people", "organizations": "orgs", "orgs": "orgs",
    "documents": "documents",
}


def _slug(stem):
    return re.sub(r"[^a-z0-9]+", "-", stem.strip().lower()).strip("-")


def build_slug_map(concepts):
    """slug -> [concept_id, ...] (a slug may exist in more than one domain)."""
    m: dict = {}
    for c in concepts:
        m.setdefault(c.id.rsplit("/", 1)[-1], []).append(c.id)
    return m


# Repo-root / non-concept path prefixes that must never be rewritten to concepts/.
_ROOT_LINK_PREFIXES = (
    "/IDENTITY.md", "/CONTEXT.md", "/AGENTS.md", "/CLAUDE.md", "/GEMINI.md",
    "/index.md", "/log.md", "/decisions.md", "/VAULT-IMPROVEMENTS.md",
    "/_config/", "/rules/", "/skills/", "/tools/", "/themes/", "/specs/",
    "/raw/", "/provenance/", "/inbox/",
)


def _is_protected_root_link(path: str) -> bool:
    """True for vault orientation/tooling paths that are not concept ids."""
    if not path:
        return False
    # Normalize: strip leading ./ and collapse
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    # Absolute-from-repo-root style
    if p.startswith("/"):
        if any(p == pref.rstrip("/") or p.startswith(pref) for pref in _ROOT_LINK_PREFIXES):
            return True
        # bare root files without leading path segments beyond one
        if p.count("/") == 1 and p.endswith(".md"):
            name = p[1:]
            if name in {
                "IDENTITY.md", "CONTEXT.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md",
                "index.md", "log.md", "decisions.md", "VAULT-IMPROVEMENTS.md",
            }:
                return True
        return False
    # relative link to root file from a concept (../ or multi-up) ending at known root files
    base = p.rsplit("/", 1)[-1]
    if base in {
        "IDENTITY.md", "CONTEXT.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md",
        "index.md", "log.md", "decisions.md", "VAULT-IMPROVEMENTS.md",
    }:
        # only protect if not clearly under concepts/
        if "concepts/" not in p:
            return True
    if p.startswith(("_config/", "rules/", "skills/", "tools/", "themes/", "specs/", "raw/", "provenance/", "inbox/")):
        return True
    return False


def resolve_to_concept(url, source_id, slug_map):
    """Map a markdown link URL to a canonical concept id, or None to leave it alone."""
    path = url.split("#", 1)[0].split("?", 1)[0]
    if not path or path.startswith(("http://", "https://", "mailto:", "#")):
        return None
    if _is_protected_root_link(path):
        return None  # orientation / tooling paths — never map to concept slugs
    if not path.endswith(".md") or path.startswith("/concepts/"):
        return None  # not a concept link, or already canonical
    slug = _slug(path.rsplit("/", 1)[-1][:-3])
    cands = slug_map.get(slug)
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    # Disambiguate: domain hinted by the original path, else the source's domain.
    segs = [p.lower() for p in path.split("/") if p]
    hint = next((DOMAIN_HINTS[s] for s in segs if s in DOMAIN_HINTS), None)
    src_domain = source_id.split("/", 1)[0]
    by_hint = next((c for c in cands if c.split("/", 1)[0] == hint), None) if hint else None
    return by_hint or next((c for c in cands if c.split("/", 1)[0] == src_domain), None)

def rewrite_links(text, source_id, slug_map):
    """Rewrite resolvable [text](old.md) links to /concepts/<id>.md. Returns (new_text, count)."""
    count = [0]

    def repl(m):
        pre, url, post = m.group(1), m.group(2), m.group(3)
        # Split off fragment (#) and query (?) — preserve both in output.
        base, rest = (url.split("#", 1) + [""])[:2]
        query = ""
        if "?" in base:
            base, query = (base.split("?", 1) + [""])[:2]
        tid = resolve_to_concept(base, source_id, slug_map)
        if not tid or tid == source_id:
            return m.group(0)
        count[0] += 1
        suffix = ""
        if query:
            suffix += f"?{query}"
        if rest:
            suffix += f"#{rest}"
        return f"{pre}/concepts/{tid}.md{suffix}{post}"

    return _RELINK_RE.sub(repl, text), count[0]


def cmd_relink(args):
    concepts = load_concepts()
    slug_map = build_slug_map(concepts)
    total, files = 0, 0
    for c in concepts:
        text = c.path.read_text(encoding="utf-8")
        new, n = rewrite_links(text, c.id, slug_map)
        if n and new != text:
            total += n
            files += 1
            if not args.dry_run:
                c.path.write_text(new, encoding="utf-8")
    verb = "Would rewrite" if args.dry_run else "Rewrote"
    print(f"{verb} {total} link(s) across {files} file(s).")
    if not args.dry_run and total:
        print("Now run `okf index` then `okf lint`.")
    return 0



# --- link (apply suggested cross-links) ------------------------------------

def _append_related_links(path: Path, target_id: str, reason: str):
    """Append a related concept link under ## Related Concepts.
    Returns True if a link was added, False if it already existed."""
    target_path = CONCEPTS_DIR / (target_id + ".md")
    if not target_path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    target_link = f"/concepts/{target_id}.md"
    if target_link in text:
        return False
    title = target_id.rsplit("/", 1)[-1].replace("-", " ").title()
    link_line = f"- [{title}](/concepts/{target_id}.md) — {reason}"
    fm, body = split_frontmatter(text)
    if "## Related Concepts" in body:
        lines = body.split("\n")
        insert_idx = len(lines)
        for i, line in enumerate(lines):
            if line.strip() == "## Related Concepts":
                for j in range(i + 1, len(lines)):
                    if lines[j].startswith("## "):
                        insert_idx = j
                        break
                    if lines[j].strip() and not lines[j].strip().startswith("- "):
                        insert_idx = j
                        break
                else:
                    insert_idx = len(lines)
                break
        lines.insert(insert_idx, link_line)
        body = "\n".join(lines)
    else:
        body = body.rstrip("\n") + "\n\n## Related Concepts\n" + link_line + "\n"
    # Find the true closing frontmatter delimiter — the second line that is exactly "---".
    # text.index("---", 3) was wrong: it matched any "---" substring inside body/frontmatter.
    delims = list(re.finditer(r"^---$", text, re.MULTILINE))
    fm_end = delims[1].end() if len(delims) >= 2 else len(text)
    path.write_text(text[:fm_end] + "\n" + body, encoding="utf-8")
    return True


def cmd_link(args):
    """Apply cross-links from suggest-links output or inline specs."""
    idx = load_index()
    if args.auto:
        pairs = suggest_links(idx, max_pairs=args.max, min_tag_overlap=args.min_tags, min_score=args.min_score, concept_id=getattr(args, "concept", None))
        noise_targets = {"tools/cc-switch"}
        pairs = [(s, a, b, r) for s, a, b, r in pairs if a not in noise_targets and b not in noise_targets]
    else:
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"Error: invalid JSON on stdin: {exc}", file=sys.stderr)
            return 1
        if not isinstance(data, list):
            print(f"Error: expected a JSON array on stdin, got {type(data).__name__}",
                  file=sys.stderr)
            return 1
        _LINK_KEYS = ("score", "a", "b", "reason")
        pairs = []
        for i, d in enumerate(data):
            missing = [k for k in _LINK_KEYS if k not in d]
            if missing:
                print(f"Error: entry {i} missing required keys: {', '.join(missing)}", file=sys.stderr)
                print(f"Each entry must have: {', '.join(_LINK_KEYS)}", file=sys.stderr)
                return 1
            pairs.append((d["score"], d["a"], d["b"], d["reason"]))
    applied = 0
    skipped = 0
    for score, id_a, id_b, reason in pairs:
        path_a = CONCEPTS_DIR / (id_a + ".md")
        path_b = CONCEPTS_DIR / (id_b + ".md")
        if not path_a.exists() or not path_b.exists():
            skipped += 1
            continue
        # Parse reason: "BM25: 108.58; terms: 3blue1brown, and, animation, by, for"
        # or "tags: math, physics, visualization"
        clean = reason.split(";", 1)[-1] if ";" in reason else reason
        clean = clean.split(":", 1)[-1].strip() if ":" in clean else clean.strip()
        tokens = [t.strip() for t in clean.split(",") if t.strip() and t.strip() not in ("and", "by", "the", "of", "for", "in", "as", "an", "a")]
        if tokens:
            prose = ", ".join(tokens[:5])
        else:
            prose = "related topic"
        added_a = _append_related_links(path_a, id_b, prose)
        added_b = _append_related_links(path_b, id_a, prose)
        if added_a or added_b:
            applied += 1
            if not args.quiet:
                print(f"linked: {id_a} ↔ {id_b}")
        else:
            skipped += 1
    print(f"Applied: {applied} pairs; Skipped: {skipped}")
    return 0

# --- sql (ad-hoc DuckDB analytical queries) --------------------------------

_SQL_SCHEMA_VERSION = 1


def _try_duckdb():
    try:
        import duckdb
        return duckdb
    except ImportError:
        print(
            "DuckDB is not installed. Install it with:\n"
            '  pip install "okf-tools[sql]"  # or: pip install duckdb',
            file=sys.stderr,
        )
        sys.exit(1)


def _get_concepts_mtime():
    """Return the max mtime of any file under concepts/."""
    best = 0.0
    for p in CONCEPTS_DIR.rglob("*.md"):
        try:
            mt = p.stat().st_mtime
            if mt > best:
                best = mt
        except OSError:
            pass
    return best


def _cache_is_fresh():
    """Check whether the cached DB exists and is at least as recent as concepts/."""
    if not SQL_CACHE.exists():
        return False
    try:
        cache_mtime = SQL_CACHE.stat().st_mtime
    except OSError:
        return False
    # Read schema version from cache
    duckdb = _try_duckdb()
    try:
        con = duckdb.connect(str(SQL_CACHE))
        con.execute("PRAGMA enable_external_access=false")
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        con.close()
        if row is None or int(row[0]) != _SQL_SCHEMA_VERSION:
            return False
        return cache_mtime >= _get_concepts_mtime()
    except Exception:
        return False


def _populate_duck(con, concepts):
    """Load concepts, tags, and links into DuckDB tables."""
    con.execute("""
        CREATE TABLE concepts (
            id         TEXT PRIMARY KEY,
            path       TEXT,
            domain     TEXT,
            type       TEXT,
            visibility TEXT,
            title      TEXT,
            status     TEXT,
            body       TEXT
        )
    """)
    con.execute("CREATE TABLE tags (concept_id TEXT, tag TEXT)")
    con.execute("CREATE TABLE links (source_id TEXT, target_id TEXT)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO meta VALUES (?, ?)", ('schema_version', str(_SQL_SCHEMA_VERSION)))

    concept_rows = [
        (
            c.id,
            str(c.path),
            c.fm.get("domain", "") or "",
            c.fm.get("type", "") or "",
            c.fm.get("visibility", "") or "",
            c.fm.get("title", "") or "",
            c.fm.get("status", "") or "",
            c.body or "",
        )
        for c in concepts
    ]
    if concept_rows:
        con.executemany("INSERT INTO concepts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", concept_rows)

    tag_rows = [
        (c.id, t)
        for c in concepts
        for t in as_list(c.fm.get("tags"))
    ]
    if tag_rows:
        con.executemany("INSERT INTO tags VALUES (?, ?)", tag_rows)

    # Build a set of known ids for filtering orphans
    known_ids = {c.id for c in concepts}
    link_rows = [
        (c.id, t)
        for c in concepts
        for t in c.links
        if t in known_ids
    ]
    if link_rows:
        con.executemany("INSERT INTO links VALUES (?, ?)", link_rows)


def _atomic_cache_swap(temp_path: Path, target: Path):
    """Atomically replace target with temp_path, cleaning up WAL files."""
    # Clean up stale WAL/SHM files from previous crashed runs
    for ext in (".wal", "-shm"):
        wal = target.with_name(target.name + ext)
        if wal.exists():
            wal.unlink()
    # Move current target aside
    if target.exists():
        shutil.move(str(target), str(target) + ".old")
    # Move new cache into place
    shutil.move(str(temp_path), str(target))
    # Clean up old cache artifacts
    for ext in (".old", ".old.wal", ".old-shm"):
        old = target.with_name(target.name + ext)
        if old.exists():
            old.unlink(missing_ok=True)

# Keywords that must never appear in a user-supplied query, even inside a
# SELECT — they enable side-effects (file access, schema mutation, etc.).
_FORBIDDEN_SQL_RE = re.compile(
    r"\b(ATTACH|PRAGMA|CREATE|INSERT|UPDATE|DELETE|DROP|readfile|read_csv)\b",
    re.IGNORECASE,
)
_MAX_QUERY_BYTES = 1_048_576  # 1 MB


def _is_select(sql: str) -> bool:
    """Check if SQL is a safe read-only SELECT query.

    Must start with SELECT and must not contain any forbidden keywords
    (ATTACH, PRAGMA, CREATE, INSERT, UPDATE, DELETE, DROP, readfile,
    read_csv) even inside the body — these enable side-effects.
    """
    if not sql.lstrip().upper().startswith("SELECT"):
        return False
    if _FORBIDDEN_SQL_RE.search(sql):
        return False
    return True


def cmd_sql(args):
    """Run an ad-hoc SQL query over the corpus using DuckDB."""
    duckdb = _try_duckdb()

    # Read query before opening any DB connection
    if args.query:
        sql = " ".join(args.query).strip()
    else:
        if sys.stdin.isatty():
            print("Read SQL from stdin (Ctrl-D to execute), or pass query as positional arg.", file=sys.stderr)
            sys.exit(1)
        sql = sys.stdin.read().strip()

    if not sql:
        print("Empty query.", file=sys.stderr)
        sys.exit(1)

    # Enforce query size limit
    if len(sql.encode("utf-8", errors="replace")) > _MAX_QUERY_BYTES:
        print(f"Query too large (limit {_MAX_QUERY_BYTES >> 20} MB).", file=sys.stderr)
        sys.exit(1)


    # Restrict to read-only queries
    if not _is_select(sql):
        print("Only SELECT queries are supported.", file=sys.stderr)
        sys.exit(1)

    # Use disk cache if fresh; otherwise rebuild
    if _cache_is_fresh():
        con = duckdb.connect(str(SQL_CACHE))
        con.execute("PRAGMA enable_external_access=false")
        con.execute("PRAGMA lock_configuration=true")
    else:
        # Rebuild into a temp file for atomic swap
        fd, tmp = tempfile.mkstemp(suffix=".duckdb", dir=str(SQL_CACHE.parent))
        os.close(fd)
        os.unlink(tmp)  # remove empty file so DuckDB can create it fresh
        temp_path = Path(tmp)
        try:
            con = duckdb.connect(str(temp_path))
            con.execute("PRAGMA enable_external_access=false")
            con.execute("PRAGMA lock_configuration=true")
            concepts = load_concepts()
            _populate_duck(con, concepts)
            con.commit()
            con.close()
            # Atomic swap: move temp → final location
            _atomic_cache_swap(temp_path, SQL_CACHE)
            # Re-open the final cache for querying
            con = duckdb.connect(str(SQL_CACHE))
            con.execute("PRAGMA enable_external_access=false")
            con.execute("PRAGMA lock_configuration=true")
        except Exception:
            # Clean up temp on failure
            if temp_path.exists():
                temp_path.unlink()
            raise

    # Escape tabs and newlines in values for TSV safety
    def _cell(v):
        s = str(v) if v is not None else ""
        return s.replace("\n", "\\n").replace("\t", "\\t")

    try:
        cur = con.execute(sql)
        headers = [desc[0] for desc in cur.description] if cur.description else []
        if headers:
            print("\t".join(headers))
        for row in cur.fetchall():
            print("\t".join(_cell(v) for v in row))
    except duckdb.Error as e:
        print(f"Query failed: {e}", file=sys.stderr)
        con.close()
        sys.exit(1)
    finally:
        con.close()

# --- doctor (agent-surface integrity) --------------------------------------

_FBC_MARKERS = (
    "full-body",
    "FULL-BODY",
    "FBC",
    "read-FULL",
    "read the full body",
    "full body",
)


def cmd_doctor(args):
    """Check ICM orientation, AGENTS uniqueness, skill routing, AAAK parity."""
    issues = []  # (level, code, msg)
    def err(code, msg):
        issues.append(("error", code, msg))
    def warn(code, msg):
        issues.append(("warn", code, msg))
    def info(code, msg):
        issues.append(("info", code, msg))

    # ICM files
    for name in ("IDENTITY.md", "CONTEXT.md"):
        if not (REPO_ROOT / name).exists():
            err("icm.missing", f"missing {name}")
    tax = REPO_ROOT / "_config" / "taxonomy.md"
    if not tax.exists():
        warn("tax.missing", "missing _config/taxonomy.md (P1 reference map)")

    agents = REPO_ROOT / "AGENTS.md"
    if agents.exists():
        at = agents.read_text(encoding="utf-8")
        n = at.count("# OKF Brain — Operating Contract")
        if n != 1:
            err("agents.dup", f"AGENTS.md Operating Contract heading count={n}, want 1")
        if "Only `skills/` and `inbox/`" in at or "Only `skills/` and `inbox/`" in at:
            err("agents.path", "AGENTS.md still has obsolete skills+inbox-only path rule")
        if "rules/path-access-control.md" not in at:
            warn("agents.path_ref", "AGENTS.md missing path-access-control reference")
    else:
        err("agents.missing", "missing AGENTS.md")

    # Skills vs CONTEXT routing
    skills_dir = REPO_ROOT / "skills"
    skill_names = []
    if skills_dir.exists():
        for d in sorted(skills_dir.iterdir()):
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            if (d / "SKILL.md").exists():
                skill_names.append(d.name)
    ctx_path = REPO_ROOT / "CONTEXT.md"
    ctx = ctx_path.read_text(encoding="utf-8") if ctx_path.exists() else ""
    for name in skill_names:
        if name not in ctx and f"skills/{name}" not in ctx:
            warn("route.skill", f"skill {name} not mentioned in CONTEXT.md routing")

    # AAAK dual-layer
    for name in skill_names:
        sm = skills_dir / name / "SKILL.md"
        sf = skills_dir / name / "SKILL.full.md"
        if not sm.exists():
            continue
        sm_t = sm.read_text(encoding="utf-8")
        if sf.exists():
            sf_t = sf.read_text(encoding="utf-8")
            def fm_field(t, key):
                if not t.startswith("---"):
                    return None
                lines = t.splitlines()
                for i, line in enumerate(lines[1:], 1):
                    if line.strip() == "---":
                        break
                    if line.startswith(f"{key}:"):
                        return line.split(":", 1)[1].strip().strip('"').strip("'")
                return None
            for key in ("name", "description"):
                a, b = fm_field(sm_t, key), fm_field(sf_t, key)
                if a != b:
                    err("aaak.fm", f"{name}: SKILL.md {key} != SKILL.full.md")
            if "FMT:AAAK" in sm_t or "lossy-agent-overlay" in sm_t:
                info("aaak.compressed", f"{name}: compressed overlay present")
            if name in ("okf-ingest", "okf-batch-ingest", "okf-ingest-channel", "okf-core"):
                if not any(m in sm_t for m in _FBC_MARKERS):
                    warn("aaak.fbc", f"{name}: compressed SKILL.md may lack FBC mandate markers")
        else:
            if name != "okf-icm-sync":
                info("aaak.no_full", f"{name}: no SKILL.full.md (ok if never compressed)")

    # Stale bad links — scan only skills concept subtree (fast path)
    skills_cx = REPO_ROOT / "concepts" / "skills"
    if skills_cx.exists():
        for path in skills_cx.rglob("*.md"):
            try:
                t = path.read_text(encoding="utf-8")
            except Exception:
                continue
            if "okf-ingest.md-channel" in t:
                warn("stale.link", f"{path.relative_to(REPO_ROOT)}: okf-ingest.md-channel")

    # Summarize
    counts = {"error": 0, "warn": 0, "info": 0}
    for level, code, msg in issues:
        counts[level] = counts.get(level, 0) + 1
        if args.json:
            continue
        print(f"{level:5} {code}: {msg}")
    if args.json:
        print(json.dumps({"issues": [
            {"level": l, "code": c, "msg": m} for l, c, m in issues
        ], "counts": counts}, indent=2))
    else:
        print(f"doctor: {counts['error']} error(s), {counts['warn']} warning(s), {counts['info']} info")
    if args.strict and counts["error"]:
        return 1
    return 0


# --- status (single gate: lint + doctor + log chain) ------------------------

def cmd_status(args):
    """One pass for agents: lint the corpus, run doctor, verify the log chain.

    READY (exit 0) — no errors anywhere. NEEDS ATTENTION (exit 0) — warnings
    or info only. BLOCKED (exit 1) — any lint error, doctor error, or log-chain
    failure.
    """
    concepts = load_concepts()
    lint = lint_concepts(concepts)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_doctor(argparse.Namespace(**{**vars(args), "json": True, "strict": False}))
    doctor = json.loads(buf.getvalue())

    ok_log, msg_log, _unsealed = log_verify(LOG_PATH)

    lint_err = [f for f in lint if f["level"] == "error"]
    lint_flag = [f for f in lint if f["level"] != "error"]
    doc_err = [i for i in doctor["issues"] if i["level"] == "error"]
    doc_flag = [i for i in doctor["issues"] if i["level"] != "error"]

    blocked = bool(lint_err or doc_err) or not ok_log
    status = "BLOCKED" if blocked else ("NEEDS ATTENTION" if (lint_flag or doc_flag) else "READY")

    result = {"status": status,
              "lint": {"errors": len(lint_err), "warnings": len(lint_flag), "details": lint},
              "doctor": {"errors": len(doc_err), "warnings": len(doc_flag), "issues": doctor["issues"]},
              "log": {"ok": ok_log, "msg": msg_log}}
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"okf status: {status}  (lint: {len(lint_err)} error / {len(lint_flag)} warn-info; "
              f"doctor: {len(doc_err)} error / {len(doc_flag)}; log: {'ok' if ok_log else 'FAIL'})")
        for f in lint_err[:10]:
            print(f"  lint error [{f['kind']}] {f['concept']} — {f['detail']}")
        for i in doc_err[:10]:
            print(f"  doctor error [{i['code']}] {i['msg']}")
        if not ok_log:
            print(f"  log FAIL: {msg_log}")
    return 1 if blocked else 0


# --- log chain (append-only integrity for log.md) ---------------------------
# JeVMind-style hash chain: h_n = sha256(h_{n-1} | entry_text_n), seeded by the
# hash of the log's head (everything before the first canonical
# `## [YYYY-MM-DD]` entry header). The log is append-only at EOF, so every
# entry anchors its successors: editing, prepending, reordering, or deleting
# a sealed entry invalidates the chain.
# State: committed repo-root log.chain.jsonl (JSONL), sealed in the same
# commit as log.md — an external anchor. Missing state is a hard error,
# never a silent re-anchor (that would launder a tampered log).
_LOG_ENTRY_RE = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\] (.+?)\s*$", re.M)


def log_parse_entries(path: Path):
    """Return (head_text, [(header, entry_text), ...]) from a log.md file.
    Entry text runs from its header line to the next canonical header, with
    trailing newlines stripped so appending a new entry at EOF never alters
    the previous entry's bytes; non-canonical headers inside the range
    belong to the previous entry."""
    text = path.read_text(encoding="utf-8")
    matches = list(_LOG_ENTRY_RE.finditer(text))
    if not matches:
        return text, []
    head = text[: matches[0].start()]
    entries = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append((f"{m.group(1)} | {m.group(2)}", text[m.start():end].rstrip("\n")))
    return head, entries


def log_text_hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log_compute_chain(head: str, entries):
    prev = log_text_hash(head)
    chain = []
    for header, text in entries:
        prev = log_text_hash(prev + text)
        chain.append({"header": header, "hash": prev})
    return prev, chain  # (tail hash, per-entry records)


def log_write_state(path: Path, head: str, chain):
    """Write JSONL state: line 1 {"head"}, then {"n","header","hash"} per entry."""
    lines = [json.dumps({"head": log_text_hash(head)})]
    for n, rec in enumerate(chain, 1):
        lines.append(json.dumps({"n": n, "header": rec["header"], "hash": rec["hash"]}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def log_load_state(path: Path):
    """Return (head_hash, [{"n","header","hash"}, ...]) from JSONL state."""
    head, entries = None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if "head" in rec:
            head = rec["head"]
        else:
            entries.append(rec)
    return head, entries


def log_verify(path: Path, state_path: Path = LOG_STATE):
    """Verify the log.md chain against committed state.
    Returns (ok, msg, unsealed): ok = head + sealed prefix intact; unsealed =
    canonical entries appended since the last seal (0..N, informational; 0
    on failure paths). Never bootstraps: missing state is a hard error."""
    head, entries = log_parse_entries(path)
    if not entries:
        return False, f"no canonical `## [YYYY-MM-DD]` entries in {path}", 0
    tail, recomputed = log_compute_chain(head, entries)
    if not state_path.exists():
        return False, f"no chain state at {state_path} — run `okf log --bootstrap` (or restore from git)", 0
    state_head, stored = log_load_state(state_path)
    if state_head != log_text_hash(head):
        return False, "log.md head (text before first entry) changed since chain seed", 0
    for n, (s, r) in enumerate(zip(stored, recomputed), 1):
        if s.get("header") != r["header"]:
            return False, f"entry {n} header differs: {r['header']!r} != stored {s.get('header')!r}", 0
        if s.get("hash") != r["hash"]:
            return False, f"entry {n} ({r['header']}) hash mismatch — edited, moved, or replaced since seal", 0
    unsealed = len(recomputed) - len(stored)
    if unsealed < 0:
        word = "entry" if unsealed == -1 else "entries"
        return False, f"{-unsealed} sealed {word} missing (deleted or truncated?)", 0
    msg = f"OK: {len(stored)} entries sealed"
    if unsealed:
        msg += f", {unsealed} unsealed — run `okf log --seal`"
    return True, msg + f", tail {tail[:12]}…", unsealed


def log_seal(path: Path, state_path: Path = LOG_STATE):
    """Verify the sealed prefix, then fold unsealed appends into state. (ok, msg)"""
    ok, msg, unsealed = log_verify(path, state_path)
    if not ok:
        return False, msg
    if not unsealed:
        return True, msg
    head, entries = log_parse_entries(path)
    _, recomputed = log_compute_chain(head, entries)
    log_write_state(state_path, head, recomputed)
    word = "entry" if unsealed == 1 else "entries"
    return True, f"sealed {unsealed} new {word} → {len(recomputed)} total"


def log_bootstrap(path: Path, state_path: Path = LOG_STATE):
    """Explicit first-run seal: chain the log's current content into state. (ok, msg)"""
    head, entries = log_parse_entries(path)
    if not entries:
        return False, f"no canonical `## [YYYY-MM-DD]` entries in {path}"
    tail, chain = log_compute_chain(head, entries)
    log_write_state(state_path, head, chain)
    return True, f"bootstrap: sealed {len(chain)} entries, tail {tail[:12]}… → {state_path}"


def cmd_log(args):
    if args.bootstrap:
        ok, msg = log_bootstrap(LOG_PATH)
    elif args.seal:
        ok, msg = log_seal(LOG_PATH)
    else:
        ok, msg, _ = log_verify(LOG_PATH)
    print(msg)
    return 0 if ok else 1


# --- schema (CLI manifest as JSON) ------------------------------------------

def cmd_schema(args):
    """Emit the CLI's own command manifest as JSON, derived from the live
    argparse parser (single source of truth — no hand-maintained list)."""
    p = build_parser()
    cmds = {}
    for action in p._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        # 3.12 keeps the subparser `help` strings on _ChoicesParsersAction's
        # pseudo-actions, positionally aligned with the choices dict.
        for (name, sp), pseudo in zip(action.choices.items(), action._choices_actions):
            cmds[name] = {"help": sp.description or pseudo.help,
                          "args": [a.dest for a in sp._actions if not a.option_strings]}
    print(json.dumps({"name": p.prog, "description": p.description, "commands": cmds},
                     indent=args.indent))
    return 0


# --- export (shareable-only bundle) ------------------------------------------

def _render_llms(concepts):
    lines = ["# OKF Shareable Bundle", ""]
    for c in concepts:
        title = c.fm.get("title") or c.id.rsplit("/", 1)[-1]
        desc = (c.fm.get("description") or "").strip().replace("\n", " ")
        lines.append(f"## [{title}](/concepts/{c.id}.md)")
        if desc:
            lines.append(f"{desc}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _export_attachments(shareable, out: Path) -> int:
    """Copy raw/attachments files referenced from exported (shareable) bodies.

    Preserves the raw/attachments/ layout so relative (../../../raw/…) and
    root-relative (/raw/…) link forms both resolve in the bundle. Only files
    actually referenced by shareable concepts are copied — private concepts'
    attachments stay in the vault.
    """
    need = set()
    for c in shareable:
        need.update(_ATTACH_REF_RE.findall(c.body))
    att_root = REPO_ROOT / "raw" / "attachments"
    att_dir = out / "raw" / "attachments"
    count = 0
    for name in sorted(need):
        if not name:
            continue
        cand = (att_root / unquote(name)).resolve()
        if not cand.is_relative_to(att_root.resolve()):
            continue  # name escapes the attachments dir (e.g. ..%2F)
        if not cand.is_file():
            continue  # dead ref in corpus — tolerated, link stays broken in bundle
        dest = att_dir / cand.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cand, dest)
        count += 1
    return count



def _render_sitemap(concepts):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for c in sorted(concepts, key=lambda c: c.id):
        title = _xml_escape(c.fm.get("title") or c.id.rsplit("/", 1)[-1])
        lines.append(f"  <url><loc>/concepts/{c.id}.md</loc><title>{title}</title></url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def _write_manifest(out: Path):
    """Write MANIFEST.json: sha256 of every bundle file (written last, so it
    covers index.json/llms.txt/sitemap.xml but not itself)."""
    files = sorted(p for p in out.rglob("*")
                   if p.is_file() and p.name != "MANIFEST.json")
    manifest = [{"path": p.relative_to(out).as_posix(), "sha256": sha256(p.read_bytes()).hexdigest()}
                for p in files]
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _verify_bundle(out: Path, shareable):
    """Re-read the bundle and check: every concept is shareable (hard error), every
    attachment ref either resolves back inside the vault attachments dir (refs that
    escape, e.g. ..%2F, are hard errors — the copier refuses them by design), and
    no attachment that exists in the vault was lost by the export (hard error);
    dangling internal links and attachment refs dead in the vault are counted,
    never fatal (contract: broken links are tolerated; cross-visibility links to
    private concepts are normal in a healthy vault).
    Returns (files_checked, links_checked, [error strings], n_dangling, n_dead)."""
    bundle_ids = {c.id for c in shareable}
    files = 0
    links = 0
    errors = []
    dangling = 0
    dead = 0
    att_dir = (out / "raw" / "attachments").resolve()
    for c in sorted(shareable, key=lambda c: c.id):
        f = out / c.path.relative_to(REPO_ROOT)
        try:
            fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
        except OSError as e:
            errors.append(f"cannot re-read {c.path.relative_to(REPO_ROOT)}: {e}")
            continue
        files += 1
        if fm.get("visibility") != "shareable":
            errors.append(f"{c.id}: bundle copy visibility is {fm.get('visibility')!r}, expected shareable")
        for ref in _ATTACH_REF_RE.findall(c.body):
            links += 1
            name = unquote(ref)
            vault_root = (REPO_ROOT / "raw" / "attachments").resolve()
            vault_cand = (vault_root / name).resolve()
            cand = (att_dir / name).resolve()
            if not cand.is_relative_to(att_dir) or not vault_cand.is_relative_to(vault_root):
                errors.append(f"{c.id}: attachment ref raw/attachments/{name!r} escapes the attachments dir")
            elif not cand.is_file():
                if vault_cand.is_file():
                    errors.append(f"{c.id}: attachment ref raw/attachments/{name!r} is in the vault but not in the bundle")
                else:
                    dead += 1  # dead ref in corpus — tolerated, link stays broken in bundle
        for target in extract_links(c.body, c.id):
            links += 1
            if target not in bundle_ids:
                dangling += 1
    return files, links, errors, dangling, dead


def cmd_export(args):
    """Export shareable concepts to a bundle dir. Hard visibility filter:
    private concepts NEVER leave the vault. Copies the raw/attachments files
    referenced from shareable bodies; private attachments stay put."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    concepts = load_concepts()
    ignore = args.ignore or []
    kept = [c for c in concepts
            if not any(fnmatch(c.path.relative_to(REPO_ROOT).as_posix(), g) for g in ignore)]
    n_ignored = len(concepts) - len(kept)
    shareable = [c for c in kept if c.fm.get("visibility") == "shareable"]
    skipped = len(kept) - len(shareable)
    for c in shareable:
        dest = out / c.path.relative_to(REPO_ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(c.path, dest)
    n_attach = _export_attachments(shareable, out)
    (out / "index.json").write_text(
        json.dumps(build_index(shareable), indent=2, ensure_ascii=False),
        encoding="utf-8")
    (out / "llms.txt").write_text(_render_llms(shareable), encoding="utf-8")
    (out / "sitemap.xml").write_text(_render_sitemap(shareable), encoding="utf-8")
    _write_manifest(out)
    n_files, n_links, verrors, n_dangling, n_dead = _verify_bundle(out, shareable)
    for e in verrors[:10]:
        print(f"verify: {e}", file=sys.stderr)
    tail = f", {n_ignored} ignored" if n_ignored else ""
    print(f"export: {len(shareable)} shareable concept(s) → {out} "
          f"({skipped} private excluded){tail}, {n_attach} attachment(s) copied")
    print(f"verified: {n_files} files, {n_links} links checked, "
          f"{len(verrors)} lost, {n_dangling} dangling, {n_dead} dead-refs")
    return 1 if verrors else 0


# --- icm-sync (refresh CONTEXT skill routing) -------------------------------

def _list_invocable_skills():
    skills_dir = REPO_ROOT / "skills"
    names = []
    if not skills_dir.exists():
        return names
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        if (d / "SKILL.md").exists():
            names.append(d.name)
    return names


def cmd_icm_sync(args):
    """Diff invocable skills vs CONTEXT.md; optionally append missing routing rows."""
    skills = _list_invocable_skills()
    ctx_path = REPO_ROOT / "CONTEXT.md"
    if not ctx_path.exists():
        print("error: CONTEXT.md missing", file=sys.stderr)
        return 1
    ctx = ctx_path.read_text(encoding="utf-8")
    missing = [s for s in skills if s not in ctx and f"skills/{s}" not in ctx]
    present = [s for s in skills if s in ctx or f"skills/{s}" in ctx]
    print(f"icm-sync: {len(skills)} skill(s); {len(present)} routed; {len(missing)} missing")
    for s in missing:
        print(f"  missing: {s}")
    if args.dry_run or not args.write:
        if missing and not args.write:
            print("hint: re-run with --write to append stub rows to CONTEXT.md")
        return 0 if not missing else (1 if args.strict else 0)
    if not missing:
        return 0
    # Append a small section before ## Do not if present
    rows = []
    for s in missing:
        rows.append(f"| Skill `{s}` | `skills/{s}/` | Auto-added by okf icm-sync; refine notes |")
    block = "\n".join(rows) + "\n"
    if "| Deep vault ops skill |" in ctx:
        ctx = ctx.replace(
            "| Deep vault ops skill |",
            block + "| Deep vault ops skill |",
            1,
        )
    else:
        ctx = ctx.rstrip() + "\n\n## Auto-routed skills\n" + block + "\n"
    if not args.dry_run:
        ctx_path.write_text(ctx, encoding="utf-8")
        print(f"wrote {len(missing)} routing stub(s) into CONTEXT.md")
    return 0


# --- cli -------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="okf", description="OKF brain tooling.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("index", help="Build search index + provenance map.")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("search", help="Search concepts (BM25).")
    sp.add_argument("query")
    sp.add_argument("--visibility", choices=VALID_VISIBILITY)
    sp.add_argument("--type")
    sp.add_argument("--domain")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("lint", help="Health-check the corpus.")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--strict", action="store_true", help="Exit non-zero on errors.")
    sp.set_defaults(func=cmd_lint)

    sp = sub.add_parser("view", help="Build index, serve locally, and open the graph viewer in a browser.")
    sp.add_argument("--port", type=int, default=8000, help="Port to serve on (auto-picks a free one if busy).")
    sp.add_argument("--no-open", action="store_true", help="Start the server but don't open a browser.")
    sp.add_argument("--no-index", action="store_true", help="Skip rebuilding the index before serving.")
    sp.set_defaults(func=cmd_view)

    sp = sub.add_parser("relink", help="Rewrite intra-corpus markdown links to canonical /concepts/<id>.md ids.")
    sp.add_argument("--dry-run", action="store_true", help="Preview rewrites; write nothing.")
    sp.set_defaults(func=cmd_relink)

    sp = sub.add_parser("backlinks", help="List inbound links to one concept (id, path, or unique slug).")
    sp.add_argument("concept", help="Concept id under concepts/, path, or unique slug.")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_backlinks)

    sp = sub.add_parser("affected", help="Transitive inbound links (what would need review if X changes); --git BASE seeds from diff.")
    sp.add_argument("concept", help="Concept id under concepts/, path, or unique slug.")
    sp.add_argument("--git", metavar="BASE", help="Also seed roots from git diff + untracked concepts vs BASE.")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_affected)
    sp = sub.add_parser("doctor", help="Agent-surface integrity (ICM, AGENTS, AAAK, routing).")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--strict", action="store_true", help="Exit non-zero on errors.")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("icm-sync", help="Diff skills/ vs CONTEXT.md routing; optional --write stubs.")
    sp.add_argument("--write", action="store_true", help="Append missing skill rows to CONTEXT.md")
    sp.add_argument("--dry-run", action="store_true", help="Report only (default if --write omitted)")
    sp.add_argument("--strict", action="store_true", help="Exit 1 if any skill missing from CONTEXT")
    sp.set_defaults(func=cmd_icm_sync)

    sp = sub.add_parser("link", help="Apply cross-links from suggest-links candidates.")
    sp.add_argument("--auto", action="store_true", help="Auto-generate and apply links (default: read JSON pairs from stdin).")
    sp.add_argument("--max", type=int, default=50, help="Max candidate pairs to apply (default 50).")
    sp.add_argument("--min-tags", type=int, default=2, help="Min shared tags for tag-based candidates.")
    sp.add_argument("--min-score", type=float, default=10.0, help="Min BM25 score to apply (default 10.0).")
    sp.add_argument("--concept", help="Scope to a single concept id (e.g. learning/dev/javascript)")
    sp.add_argument("--quiet", action="store_true", help="Suppress per-pair output.")
    sp.set_defaults(func=cmd_link)

    sp = sub.add_parser("suggest-links", help="Find missing cross-links between concepts.")
    sp.add_argument("--max", type=int, default=50, help="Max candidate pairs to output (default 50).")
    sp.add_argument("--min-tags", type=int, default=2, help="Min shared tags for tag-based candidates (default 2).")
    sp.add_argument("--min-score", type=float, default=0.5, help="Min BM25 score for orphan candidates (default 0.5).")
    sp.add_argument("--concept", help="Scope to a single concept id (e.g. learning/dev/javascript)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_suggest_links)

    sp = sub.add_parser("sql", help="Run ad-hoc SQL queries over the corpus (requires duckdb).")
    sp.add_argument("query", nargs="*", help="SQL query (read from stdin if omitted).")
    sp.set_defaults(func=cmd_sql)

    sp = sub.add_parser("log", help="Verify the hash chain over log.md (state: root log.chain.jsonl).")
    sp.add_argument("--seal", action="store_true", help="Fold unsealed appends into the chain.")
    sp.add_argument("--bootstrap", action="store_true", help="Explicit first-run seal (never automatic).")
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("status", help="Single gate: lint + doctor + log chain → READY / NEEDS ATTENTION / BLOCKED.")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("schema", help="Emit this CLI's command manifest as JSON.")
    sp.add_argument("--indent", type=int, default=2, help="JSON indent width (default 2).")
    sp.set_defaults(func=cmd_schema)

    sp = sub.add_parser("export", help="Export shareable concepts to a bundle dir (private never exported).")
    sp.add_argument("--out", required=True, help="Destination directory for the bundle.")
    sp.add_argument("--ignore", action="append", default=[], metavar="GLOB",
                    help="Ignore repo-relative concept path GLOB(s), repeated (excluded from bundle).")
    sp.set_defaults(func=cmd_export)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
