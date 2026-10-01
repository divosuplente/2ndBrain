"""Unit tests for the okf CLI library (stdlib + pytest only)."""
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from hashlib import sha256

import okf
import pytest

def make_concept(cid, fm, body=""):
    """Build a Concept in-memory (no disk access needed)."""
    return okf.Concept(cid, okf.CONCEPTS_DIR / (cid + ".md"), fm, body)


# --- frontmatter parsing ---------------------------------------------------

def test_parse_frontmatter_scalars_inline_and_block_lists():
    block = "\n".join([
        "type: tool",
        "visibility: shareable",
        'title: "Nub"',
        "tags: [nodejs, runtime]",
        "source:",
        "  - toolswiki:ecosystem/nub.md",
        "  - pka:PKM/My Life/Topics/ai-tooling.md",
        "# a comment line",
        "timestamp: 2026-06-30T00:00:00Z",
    ])
    fm = okf.parse_frontmatter(block)
    assert fm["type"] == "tool"
    assert fm["visibility"] == "shareable"
    assert fm["title"] == "Nub"
    assert fm["tags"] == ["nodejs", "runtime"]
    assert fm["source"] == [
        "toolswiki:ecosystem/nub.md",
        "pka:PKM/My Life/Topics/ai-tooling.md",
    ]
    assert fm["timestamp"] == "2026-06-30T00:00:00Z"


def test_split_frontmatter_roundtrip_and_absent():
    text = "---\ntype: note\nvisibility: private\n---\n# Body\nhello"
    fm, body = okf.split_frontmatter(text)
    assert fm == {"type": "note", "visibility": "private"}
    assert body.strip() == "# Body\nhello"

    fm2, body2 = okf.split_frontmatter("no frontmatter here")
    assert fm2 == {}
    assert body2 == "no frontmatter here"


def test_tokenize_lowercases_and_splits():
    assert okf.tokenize("Hello, OKF-World 123!") == ["hello", "okf", "world", "123"]


# --- link extraction -------------------------------------------------------

def test_extract_links_bundle_relative_and_external():
    body = (
        "See [Nub](/concepts/tools/nub.md) and "
        "[OKF](concepts/specs/open-knowledge-format.md) and "
        "[site](https://example.com) and [anchor](#section)."
    )
    links = okf.extract_links(body, "tools/rtk")
    assert "tools/nub" in links
    assert "specs/open-knowledge-format" in links
    assert all(not l.startswith("http") for l in links)
    assert len(links) == 2


def test_extract_links_relative_path():
    body = "Neighbor [x](./customers.md), parent [y](../orgs/acme.md)."
    links = okf.extract_links(body, "people/jane")
    assert "people/customers" in links
    assert "orgs/acme" in links


# --- index + search --------------------------------------------------------

def _sample_concepts():
    return [
        make_concept("tools/nub", {
            "type": "tool", "visibility": "shareable", "domain": "tools",
            "title": "Nub", "description": "All-in-one Node.js toolkit.",
            "tags": ["nodejs", "runtime"],
        }, "Nub augments stock Node with a fast runtime and package manager."),
        make_concept("tools/rtk", {
            "type": "tool", "visibility": "shareable", "domain": "tools",
            "title": "RTK", "description": "Token reduction proxy.",
        }, "RTK is a CLI proxy that reduces tokens for coding agents."),
        make_concept("life/ai-tooling", {
            "type": "topic", "visibility": "private", "domain": "life",
            "title": "AI Tooling", "description": "Agents and workflows I use.",
        }, "Tracking node runtime experiments and agent delegation."),
    ]


def test_build_index_shape():
    index = okf.build_index(_sample_concepts())
    assert index["count"] == 3
    assert index["avgdl"] > 0
    assert "node" in index["df"]
    ids = {d["id"] for d in index["concepts"]}
    assert ids == {"tools/nub", "tools/rtk", "life/ai-tooling"}


def test_search_ranks_relevant_first():
    index = okf.build_index(_sample_concepts())
    results = okf.bm25_search(index, "node runtime")
    assert results, "expected at least one hit"
    assert results[0][1]["id"] == "tools/nub"


def test_search_visibility_filter():
    index = okf.build_index(_sample_concepts())
    results = okf.bm25_search(index, "node runtime agent")
    shareable = okf.apply_filters(results, visibility="shareable")
    assert shareable
    assert all(d["visibility"] == "shareable" for _, d in shareable)
    assert all(d["id"] != "life/ai-tooling" for _, d in shareable)


def test_search_type_and_domain_filters():
    index = okf.build_index(_sample_concepts())
    results = okf.bm25_search(index, "node runtime agent")
    topics = okf.apply_filters(results, type="topic", domain="life")
    assert all(d["type"] == "topic" and d["domain"] == "life" for _, d in topics)


# --- lint ------------------------------------------------------------------

def test_lint_missing_field_and_bad_visibility():
    concepts = [
        make_concept("tools/a", {"type": "tool"}, "no visibility"),
        make_concept("tools/b", {"type": "tool", "visibility": "public"}, "bad vis"),
    ]
    findings = okf.lint_concepts(concepts)
    kinds = {(f["kind"], f["concept"]) for f in findings}
    assert ("missing-field", "tools/a") in kinds
    assert ("bad-visibility", "tools/b") in kinds


def test_lint_broken_link_and_orphan():
    concepts = [
        make_concept("tools/a", {"type": "tool", "visibility": "shareable"},
                     "links [x](/concepts/tools/missing.md)"),
    ]
    findings = okf.lint_concepts(concepts)
    kinds = {f["kind"] for f in findings}
    assert "broken-link" in kinds
    assert "orphan" in kinds  # single concept has no inbound links


def test_lint_no_privacy_warning_for_nonpersonal_domain_shareable():
    """D-015: a shareable concept in a non-personal domain should NOT be flagged,
    even with historical pka: sources."""
    concepts = [
        make_concept("tools/leak", {
            "type": "tool", "visibility": "shareable", "domain": "tools",
            "source": ["pka:PKM/My Life/Topics/secret.md"],
        }, "should not be flagged"),
    ]
    findings = okf.lint_concepts(concepts)
    assert not any(f["kind"] == "privacy" for f in findings)


def test_lint_warns_on_personal_domain_shareable():
    """D-015: a shareable concept in a personal domain should be flagged."""
    concepts = [
        make_concept("life/journal", {
            "type": "topic", "visibility": "shareable", "domain": "life",
        }, ""),
    ]
    findings = okf.lint_concepts(concepts)
    assert any(f["kind"] == "privacy" and "life" in f["detail"] for f in findings)


def test_lint_no_warning_on_personal_domain_private():
    """D-015: a private concept in a personal domain should NOT be flagged."""
    concepts = [
        make_concept("people/jane", {
            "type": "person", "visibility": "private", "domain": "people",
        }, ""),
    ]
    findings = okf.lint_concepts(concepts)
    assert not any(f["kind"] == "privacy" for f in findings)


def test_lint_duplicate_titles():
    concepts = [
        make_concept("tools/a", {"type": "tool", "visibility": "shareable", "title": "Same"}, ""),
        make_concept("specs/b", {"type": "spec", "visibility": "shareable", "title": "same"}, ""),
    ]
    findings = okf.lint_concepts(concepts)
    assert any(f["kind"] == "duplicate" for f in findings)


def test_lint_missing_tag_and_tag_conventions():
    concepts = [
        make_concept("tools/a", {"type": "tool", "visibility": "shareable", "domain": "tools"}, ""),  # no tags
        make_concept("tools/b", {"type": "tool", "visibility": "shareable", "domain": "tools",
                                 "tags": ["Bad_Case", "tools", "clippings", "nodejs"]}, ""),
    ]
    findings = okf.lint_concepts(concepts)
    by_concept = {(f["kind"], f["concept"]) for f in findings}
    assert ("missing-tag", "tools/a") in by_concept
    assert ("bad-tag", "tools/b") in by_concept          # Bad_Case + clippings
    assert ("domain-tag", "tools/b") in by_concept        # 'tools' redundant with domain
    details = [f["detail"] for f in findings if f["kind"] == "bad-tag"]
    assert any("Bad_Case" in d for d in details)
    assert any("clippings" in d for d in details)


def test_lint_plural_tag_pair_reported_once():
    concepts = [
        make_concept("tools/a", {"type": "tool", "visibility": "shareable", "tags": ["agent"]}, ""),
        make_concept("tools/b", {"type": "tool", "visibility": "shareable", "tags": ["agent"]}, ""),
        make_concept("tools/c", {"type": "tool", "visibility": "shareable", "tags": ["agents"]}, ""),
    ]
    findings = okf.lint_concepts(concepts)
    pairs = [f for f in findings if f["kind"] == "plural-tag"]
    assert len(pairs) == 1
    assert "'agent'(2)" in pairs[0]["detail"] and "'agents'(1)" in pairs[0]["detail"]


def test_lint_malformed_tag_double_dash():
    concepts = [
        make_concept("tools/a", {"type": "tool", "visibility": "shareable",
                                 "tags": ["- life/neurodivergent", "ok"]}, ""),
    ]
    findings = okf.lint_concepts(concepts)
    malform = [f for f in findings if f["kind"] == "malformed-tag"]
    assert len(malform) == 1
    assert "- life/neurodivergent" in malform[0]["detail"]
    # the malformed entry must not pollute tag counts / plural logic
    assert not any(f["kind"] == "plural-tag" for f in findings)


# --- view wiring -----------------------------------------------------------

def test_viewer_url():
    assert okf.viewer_url(8000) == f"http://{okf.LOOPBACK}:8000/tools/viewer.html"


def test_view_parser_defaults():
    args = okf.build_parser().parse_args(["view"])
    assert args.func is okf.cmd_view
    assert args.port == 8000
    assert args.no_open is False
    assert args.no_index is False


# --- relink ----------------------------------------------------------------

def test_resolve_to_concept_unique_and_skips():
    sm = {"rtk": ["tools/rtk"]}
    assert okf.resolve_to_concept("../ecosystem/rtk.md", "tools/claude-code", sm) == "tools/rtk"
    assert okf.resolve_to_concept("rtk.md", "tools/x", sm) == "tools/rtk"
    assert okf.resolve_to_concept("https://rtk.ai/", "tools/x", sm) is None
    assert okf.resolve_to_concept("/concepts/tools/rtk.md", "tools/x", sm) is None  # already canonical
    assert okf.resolve_to_concept("unknown.md", "tools/x", sm) is None


def test_resolve_to_concept_disambiguates():
    sm = {"x": ["tools/x", "learning/x"]}
    assert okf.resolve_to_concept("../learning/x.md", "tools/a", sm) == "learning/x"
    assert okf.resolve_to_concept("../ecosystem/x.md", "life/a", sm) == "tools/x"
    # no hint -> fall back to source domain
    assert okf.resolve_to_concept("x.md", "learning/a", sm) == "learning/x"


def test_rewrite_links():
    sm = {"rtk": ["tools/rtk"], "prompting-101": ["learning/prompting-101"]}
    text = ("See [RTK](../ecosystem/rtk.md) and [P](../learning/prompting-101.md#step-2) "
            "and [ext](https://x.com) and [self](rtk.md).")
    new, n = okf.rewrite_links(text, "tools/claude-code", sm)
    assert "[RTK](/concepts/tools/rtk.md)" in new
    assert "[P](/concepts/learning/prompting-101.md#step-2)" in new  # fragment preserved
    assert "[ext](https://x.com)" in new  # external untouched
    assert n == 3


def test_relink_parser_defaults():
    args = okf.build_parser().parse_args(["relink"])
    assert args.func is okf.cmd_relink
    assert args.dry_run is False
    args = okf.build_parser().parse_args(["relink", "--dry-run"])
    assert args.dry_run is True


def test_rewrite_links_preserves_query_string():
    sm = {"rtk": ["tools/rtk"]}
    new, n = okf.rewrite_links("[RTK](rtk.md?version=2#section)", "tools/x", sm)
    assert "[RTK](/concepts/tools/rtk.md?version=2#section)" in new
    assert n == 1


def test_resolve_to_concept_cross_domain_no_hint():
    """Slug in multiple domains with no DOMAIN_HINT for the path segment.
    Falls back to source domain — which may differ from the original author's intent."""
    sm = {"shared": ["tools/shared", "life/shared"]}
    # No hint for 'random/' segment -> falls back to source domain 'tools'
    assert okf.resolve_to_concept("../random/shared.md", "tools/a", sm) == "tools/shared"
    # Same link from a life/ source -> resolves to life/shared
    assert okf.resolve_to_concept("../random/shared.md", "life/a", sm) == "life/shared"


# --- resolve_concept_arg + cmd_backlinks -------------------------------------

def test_resolve_concept_arg_forms():
    """Exact id, .md path form, and unique slug all resolve."""
    ids = {"tools/nub", "life/diary"}
    cid, note = okf.resolve_concept_arg("tools/nub", ids)
    assert cid == "tools/nub" and note == []
    cid, note = okf.resolve_concept_arg("/concepts/life/diary.md", ids)
    assert cid == "life/diary" and note == []
    cid, note = okf.resolve_concept_arg("nub", ids)
    assert cid == "tools/nub" and note == ["resolved slug 'nub'"]


def test_resolve_concept_arg_ambiguous_and_missing():
    cid, matches = okf.resolve_concept_arg("nub", {"tools/nub", "life/nub"})
    assert cid is None and matches == ["life/nub", "tools/nub"]
    cid, note = okf.resolve_concept_arg("zzz", {"tools/nub", "life/nub"})
    assert cid is None and note == []


def _backlinks_concepts():
    a = make_concept("tools/a", {"type": "tool", "visibility": "shareable"},
                     "[B](/concepts/tools/b.md)")
    b = make_concept("tools/b", {"type": "tool", "visibility": "shareable"},
                     "see [A](/concepts/tools/a.md) and [self](/concepts/tools/b.md)")
    c = make_concept("learning/c", {"type": "learning", "visibility": "shareable"},
                     "[B](/concepts/tools/b.md)")
    return [a, b, c]


def test_cmd_backlinks_lists_inbound_excluding_self():
    args = okf.build_parser().parse_args(["backlinks", "tools/b"])
    with patch.object(okf, "load_concepts", return_value=_backlinks_concepts()):
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = okf.cmd_backlinks(args)
        finally:
            out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    assert "2 inbound link(s)" in out
    assert "  tools/a" in out and "  learning/c" in out
    assert "  tools/b" not in out  # self-link excluded


def test_cmd_backlinks_json():
    args = okf.build_parser().parse_args(["backlinks", "tools/b", "--json"])
    with patch.object(okf, "load_concepts", return_value=_backlinks_concepts()):
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = okf.cmd_backlinks(args)
        finally:
            out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    rows = json.loads(out)
    assert {r["id"] for r in rows} == {"tools/a", "learning/c"}


def test_cmd_backlinks_not_found_exits_1():
    args = okf.build_parser().parse_args(["backlinks", "no/such"])
    with patch.object(okf, "load_concepts", return_value=[]):
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            rc = okf.cmd_backlinks(args)
        finally:
            err, sys.stderr = sys.stderr.getvalue(), old
    assert rc == 1
    assert "not found" in err


# --- relink root protection + doctor ---------------------------------------

def test_protected_root_links_not_resolved_to_concepts():
    slug_map = {"agents": ["tools/agents/agents"], "identity": ["life/identity"]}
    assert okf.resolve_to_concept("/AGENTS.md", "tools/x", slug_map) is None
    assert okf.resolve_to_concept("/IDENTITY.md", "tools/x", slug_map) is None
    assert okf.resolve_to_concept("/_config/taxonomy.md", "tools/x", slug_map) is None
    assert okf.resolve_to_concept("skills/okf-ingest/SKILL.md", "tools/x", slug_map) is None
    assert okf._is_protected_root_link("/CONTEXT.md") is True
    assert okf._is_protected_root_link("/concepts/tools/nub.md") is False


def test_rewrite_links_leaves_root_agents_alone():
    text = "See [contract](/AGENTS.md) and [nub](nub.md)."
    slug_map = {"agents": ["tools/agents/agents"], "nub": ["tools/nub"]}
    new, n = okf.rewrite_links(text, "tools/warp", slug_map)
    assert "/AGENTS.md" in new
    assert "/concepts/tools/agents/agents.md" not in new
    # nub may rewrite
    assert n >= 0


def test_doctor_runs_without_error(tmp_path, monkeypatch):
    class A:
        json = False
        strict = False
    # doctor uses REPO_ROOT; just ensure callable returns int
    rc = okf.cmd_doctor(A())
    assert rc in (0, 1)


def test_icm_sync_lists_skills_without_write():
    class A:
        write = False
        dry_run = True
        strict = False
    rc = okf.cmd_icm_sync(A())
    assert rc in (0, 1)


def test_list_invocable_skills_nonempty():
    names = okf._list_invocable_skills()
    assert "okf-core" in names
    assert "okf-ingest" in names


# --- sql -------------------------------------------------------------------

def _sample_sql_concepts():
    """Return a small set of Concept objects for SQL tests."""
    return [
        okf.Concept("tools/a", okf.CONCEPTS_DIR / "tools/a.md",
                     {"type": "tool", "visibility": "shareable", "domain": "tools", "title": "Tool A", "tags": ["dev", "agent"]},
                     "Body of A. Links to [b](./b.md)."),
        okf.Concept("tools/b", okf.CONCEPTS_DIR / "tools/b.md",
                     {"type": "tool", "visibility": "private", "domain": "tools", "title": "Tool B", "tags": ["dev"]},
                     "Body of B."),
        okf.Concept("life/c", okf.CONCEPTS_DIR / "life/c.md",
                     {"type": "topic", "visibility": "private", "domain": "life", "title": "Life C", "tags": ["health"]},
                     "Body with\ttab and\nnewline."),
    ]


def test_populate_duck_creates_tables_and_populates():
    """_populate_duck creates concepts, tags, links tables with correct row counts."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    con = duckdb.connect()
    concepts = _sample_sql_concepts()
    okf._populate_duck(con, concepts)

    assert con.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 4  # dev, agent, dev, health
    # tools/a links to tools/b (resolved from ./b.md)
    assert con.execute("SELECT COUNT(*) FROM links").fetchone()[0] >= 1
    con.close()


def test_populate_duck_filters_orphan_links():
    """Links to non-existent concept ids are dropped."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    # Concept that links to a ghost id
    concepts = [
        okf.Concept("tools/x", okf.CONCEPTS_DIR / "tools/x.md",
                     {"type": "tool", "visibility": "shareable", "domain": "tools", "title": "X", "tags": []},
                     "Links to [ghost](../ghost/missing.md)."),
    ]
    con = duckdb.connect()
    okf._populate_duck(con, concepts)
    # No links should exist because ghost/missing doesn't exist in concepts
    count = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    assert count == 0
    con.close()


def test_populate_duck_handles_empty_concepts():
    """_populate_duck with zero concepts creates empty tables without error."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    con = duckdb.connect()
    okf._populate_duck(con, [])
    assert con.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0
    con.close()


def test_populate_duck_stores_meta_schema_version():
    """meta table records the schema version."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    con = duckdb.connect()
    okf._populate_duck(con, [])
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert row is not None
    assert int(row[0]) == okf._SQL_SCHEMA_VERSION
    con.close()


def test_populate_duck_preserves_unicode_and_newlines():
    """Unicode and control characters in body survive round-trip."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    body = "日本語 🚀\nLine 2\n\tTabbed"
    concepts = [
        okf.Concept("tools/u", okf.CONCEPTS_DIR / "tools/u.md",
                     {"type": "tool", "visibility": "shareable", "domain": "tools", "title": "Unicode", "tags": []},
                     body),
    ]
    con = duckdb.connect()
    okf._populate_duck(con, concepts)
    stored = con.execute("SELECT body FROM concepts WHERE id='tools/u'").fetchone()[0]
    assert stored == body
    con.close()


def test_cmd_sql_runs_select_query():
    """cmd_sql executes a simple SELECT and prints TSV with headers."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    # Create a temp cache dir to avoid touching real cache
    with tempfile.TemporaryDirectory() as td:
        cache_path = os.path.join(td, "test.duckdb")
        with patch.object(okf, 'SQL_CACHE', Path(cache_path)):
            args = MagicMock(query=["SELECT", "domain,", "COUNT(*)", "as", "n", "FROM", "concepts", "GROUP", "BY", "domain"])
            with patch('sys.exit') as mock_exit:
                # Capture stdout
                old_stdout, sys.stdout = sys.stdout, io.StringIO()
                try:
                    okf.cmd_sql(args)
                finally:
                    output = sys.stdout.getvalue()
                    sys.stdout = old_stdout

            mock_exit.assert_not_called()
            lines = output.strip().split("\n")
            assert len(lines) >= 2  # header + at least 1 row
            assert lines[0] == "domain\tn"


def test_cmd_sql_rejects_whitespace_only_query():
    """Whitespace-only query exits with error."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    with tempfile.TemporaryDirectory() as td:
        cache_path = os.path.join(td, "test.duckdb")
        with patch.object(okf, 'SQL_CACHE', Path(cache_path)):
            args = MagicMock(query=["   "])
            with patch('sys.exit', side_effect=lambda code: (_ for _ in ()).throw(SystemExit(code))):
                old_stderr, sys.stderr = sys.stderr, io.StringIO()
                try:
                    try:
                        okf.cmd_sql(args)
                    except SystemExit as e:
                        exit_code = e.code
                finally:
                    stderr = sys.stderr.getvalue()
                    sys.stderr = old_stderr
            assert exit_code == 1
            assert "Empty query" in stderr


def test_cmd_sql_escapes_tabs_and_newlines_in_output():
    """TSV output escapes literal tabs and newlines in cell values."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    with tempfile.TemporaryDirectory() as td:
        cache_path = os.path.join(td, "test.duckdb")
        with patch.object(okf, 'SQL_CACHE', Path(cache_path)):
            args = MagicMock(query=["SELECT", "body", "FROM", "concepts", "WHERE", "id='tools/u'"])
            # Populate with a concept first so cache exists
            con = duckdb.connect(cache_path)
            con.execute("PRAGMA enable_external_access=false")
            okf._populate_duck(con, [
                okf.Concept("tools/u", okf.CONCEPTS_DIR / "tools/u.md",
                             {"type": "tool", "visibility": "shareable", "domain": "tools", "title": "T", "tags": []},
                             "line1\ttab\nline2"),
            ])
            con.commit()
            con.close()

            with patch('sys.exit') as mock_exit:
                old_stdout, sys.stdout = sys.stdout, io.StringIO()
                try:
                    okf.cmd_sql(args)
                finally:
                    output = sys.stdout.getvalue()
                    sys.stdout = old_stdout

            mock_exit.assert_not_called()
            lines = output.strip().split("\n")
            # Header line
            # Data line should have escaped chars, not literal tab/newline
            assert "\\t" in lines[1], "Tab should be escaped as \\t"
            assert "\\n" in lines[1], "Newline should be escaped as \\n"


def test_cmd_sql_bad_sql_reports_error():
    """Malformed SQL prints error and exits 1."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    with tempfile.TemporaryDirectory() as td:
        cache_path = os.path.join(td, "test.duckdb")
        with patch.object(okf, 'SQL_CACHE', Path(cache_path)):
            args = MagicMock(query=["SELECT", "*", "FROM", "nonexistent_table"])
            with patch('sys.exit', side_effect=lambda code: (_ for _ in ()).throw(SystemExit(code))):
                old_stderr, sys.stderr = sys.stderr, io.StringIO()
                try:
                    try:
                        okf.cmd_sql(args)
                    except SystemExit as e:
                        exit_code = e.code
                finally:
                    stderr = sys.stderr.getvalue()
                    sys.stderr = old_stderr
            assert exit_code == 1
            assert "Query failed" in stderr


def test_cmd_sql_blocks_file_read():
    """enable_external_access=false prevents reading arbitrary files."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    with tempfile.TemporaryDirectory() as td:
        cache_path = os.path.join(td, "test.duckdb")
        with patch.object(okf, 'SQL_CACHE', Path(cache_path)):
            args = MagicMock(query=["SELECT", "read_text('/etc/hosts')"])
            with patch('sys.exit', side_effect=lambda code: (_ for _ in ()).throw(SystemExit(code))):
                old_stderr, sys.stderr = sys.stderr, io.StringIO()
                try:
                    try:
                        okf.cmd_sql(args)
                    except SystemExit as e:
                        exit_code = e.code
                finally:
                    stderr = sys.stderr.getvalue()
                    sys.stderr = old_stderr
            assert exit_code == 1
            assert "failed" in stderr.lower() or "error" in stderr.lower()


def test_get_concepts_mtime_returns_positive():
    """_get_concepts_mtime returns a positive float for a non-empty concepts/."""
    mt = okf._get_concepts_mtime()
    assert mt > 0


def test_cache_is_fresh_false_when_no_cache():
    """_cache_is_fresh returns False when cache file doesn't exist."""
    with tempfile.TemporaryDirectory() as td:
        fake_cache = Path(td) / "ghost.duckdb"
        with patch.object(okf, 'SQL_CACHE', fake_cache):
            assert okf._cache_is_fresh() is False


def test_sql_parser_wiring():
    """okf sql subcommand is wired in the argument parser."""
    parser = okf.build_parser()
    args = parser.parse_args(["sql", "SELECT", "1"])
    assert args.cmd == "sql"
    assert args.query == ["SELECT", "1"]


def test_cmd_sql_rejects_ddl():
    """cmd_sql rejects non-SELECT statements."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    with tempfile.TemporaryDirectory() as td:
        cache_path = os.path.join(td, "test.duckdb")
        with patch.object(okf, 'SQL_CACHE', Path(cache_path)):
            for bad_query in ["DROP TABLE concepts", "INSERT INTO concepts VALUES ('x','','','','','','','')", "DELETE FROM concepts", "ALTER TABLE concepts ADD COLUMN foo TEXT"]:
                args = MagicMock(query=bad_query.split())
                with patch('sys.exit', side_effect=lambda code: (_ for _ in ()).throw(SystemExit(code))):
                    old_stderr, sys.stderr = sys.stderr, io.StringIO()
                    try:
                        try:
                            okf.cmd_sql(args)
                        except SystemExit as e:
                            exit_code = e.code
                    finally:
                        stderr = sys.stderr.getvalue()
                        sys.stderr = old_stderr
                assert exit_code == 1
                assert "SELECT" in stderr


def test_cmd_sql_stdin_path():
    """cmd_sql reads SQL from stdin when args.query is empty."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    with tempfile.TemporaryDirectory() as td:
        cache_path = os.path.join(td, "test.duckdb")
        with patch.object(okf, 'SQL_CACHE', Path(cache_path)):
            args = MagicMock(query=[])
            # Simulate non-tty stdin with SQL
            with patch.object(sys, 'stdin', new_callable=lambda: MagicMock(isatty=lambda: False, read=lambda: "SELECT COUNT(*) FROM concepts")):
                with patch('sys.exit') as mock_exit:
                    old_stdout, sys.stdout = sys.stdout, io.StringIO()
                    try:
                        okf.cmd_sql(args)
                    finally:
                        output = sys.stdout.getvalue()
                        sys.stdout = old_stdout
                mock_exit.assert_not_called()
                # Should have output a count
                assert output.strip() != ""


def test_cache_hit_path_reuses_existing():
    """cmd_sql uses existing cache when it is fresh."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    with tempfile.TemporaryDirectory() as td:
        cache_path = Path(td) / "test.duckdb"
        with patch.object(okf, 'SQL_CACHE', cache_path):
            # First call builds the cache
            args1 = MagicMock(query=["SELECT", "1", "as", "cold"])
            with patch('sys.exit') as mock_exit:
                old_stdout, sys.stdout = sys.stdout, io.StringIO()
                try:
                    okf.cmd_sql(args1)
                finally:
                    sys.stdout = old_stdout
            mock_exit.assert_not_called()
            assert cache_path.exists()

            # Second call should use cache (no rebuild needed)
            # Verify by checking _cache_is_fresh returns True
            assert okf._cache_is_fresh() is True

            args2 = MagicMock(query=["SELECT", "1", "as", "cached"])
            with patch('sys.exit') as mock_exit:
                old_stdout, sys.stdout = sys.stdout, io.StringIO()
                try:
                    okf.cmd_sql(args2)
                finally:
                    output = sys.stdout.getvalue()
                    sys.stdout = old_stdout
            mock_exit.assert_not_called()
            assert "cached" in output
            assert "1" in output


def test_cache_invalidates_on_schema_version_mismatch():
    """_cache_is_fresh returns False when schema version doesn't match."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    with tempfile.TemporaryDirectory() as td:
        cache_path = Path(td) / "test.duckdb"
        # Build a cache with version 1
        with patch.object(okf, 'SQL_CACHE', cache_path):
            args = MagicMock(query=["SELECT", "1"])
            with patch('sys.exit') as mock_exit:
                old_stdout, sys.stdout = sys.stdout, io.StringIO()
                try:
                    okf.cmd_sql(args)
                finally:
                    sys.stdout = old_stdout
            mock_exit.assert_not_called()
            assert okf._cache_is_fresh() is True

        # Bump version — cache should now be stale
        old_version = okf._SQL_SCHEMA_VERSION
        okf._SQL_SCHEMA_VERSION = 99
        try:
            with patch.object(okf, 'SQL_CACHE', cache_path):
                assert okf._cache_is_fresh() is False
        finally:
            okf._SQL_SCHEMA_VERSION = old_version


def test_populate_duck_handles_missing_frontmatter_keys():
    """_populate_duck doesn't crash when concept fm is missing optional keys."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    # Concept with minimal frontmatter (only required fields)
    concepts = [
        okf.Concept("tools/minimal", okf.CONCEPTS_DIR / "tools/minimal.md",
                     {"type": "tool", "visibility": "shareable"},
                     "Body."),
    ]
    con = duckdb.connect()
    okf._populate_duck(con, concepts)
    row = con.execute("SELECT id, domain, title, status FROM concepts WHERE id='tools/minimal'").fetchone()
    assert row[0] == "tools/minimal"
    assert row[1] == ""  # domain missing → empty
    assert row[2] == ""  # title missing → empty
    assert row[3] == ""  # status missing → empty
    con.close()


def test_is_select_identifies_queries():
    """_is_select correctly identifies SELECT queries and rejects others."""
    assert okf._is_select("SELECT * FROM x") is True
    assert okf._is_select("  SELECT * FROM x") is True
    assert okf._is_select("select * from x") is True
    assert okf._is_select("SELECT * FROM x; DROP TABLE y") is True  # starts with SELECT
    assert okf._is_select("DROP TABLE x") is False
    assert okf._is_select("INSERT INTO x VALUES (1)") is False
    assert okf._is_select("DELETE FROM x") is False
    assert okf._is_select("UPDATE x SET y=1") is False


# --- _append_related_links delimiter bug (S-C1) ----------------------------

def test_append_related_links_body_with_hrule(tmp_path, monkeypatch):
    """_append_related_links must not truncate body when it contains ---."""
    monkeypatch.setattr(okf, "CONCEPTS_DIR", tmp_path)
    (tmp_path / "tools").mkdir()
    src = tmp_path / "tools" / "src.md"
    src.write_text(
        "---\ntype: tool\nvisibility: shareable\n---\n"
        "# Source\n\nSome intro\n\n---\n\nMore content after hrule\n",
        encoding="utf-8",
    )
    tgt = tmp_path / "tools" / "tgt.md"
    tgt.write_text("---\ntype: tool\nvisibility: shareable\n---\n# Target\n", encoding="utf-8")
    assert okf._append_related_links(src, "tools/tgt", "related")
    result = src.read_text(encoding="utf-8")
    assert "More content after hrule" in result, f"Body was truncated: {result!r}"


# --- cmd_link JSON validation (S-H3) ----------------------------------------

def test_cmd_link_rejects_invalid_json_stdin():
    """cmd_link exits 1 when stdin contains invalid JSON."""
    args = okf.build_parser().parse_args(["link"])
    with patch("sys.stdin", io.StringIO("not json {{{")):
        with patch("okf.load_index"):
            rc = okf.cmd_link(args)
    assert rc == 1


def test_cmd_link_rejects_non_array_json_stdin():
    """cmd_link exits 1 when stdin JSON is not an array."""
    args = okf.build_parser().parse_args(["link"])
    with patch("sys.stdin", io.StringIO('{"score": 1, "a": "x", "b": "y", "reason": "z"}')):
        with patch("okf.load_index"):
            rc = okf.cmd_link(args)
    assert rc == 1


def test_cmd_link_rejects_missing_required_keys():
    """cmd_link exits 1 when an entry is missing required keys."""
    args = okf.build_parser().parse_args(["link"])
    bad_entry = json.dumps([{"score": 0.9, "a": "tools/x"}])  # missing b, reason
    with patch("sys.stdin", io.StringIO(bad_entry)):
        with patch("okf.load_index"):
            rc = okf.cmd_link(args)
    assert rc == 1


def test_cmd_link_accepts_valid_json_stdin():
    """cmd_link returns 0 for valid JSON array with all required keys (concepts may not exist)."""
    args = okf.build_parser().parse_args(["link"])
    good = json.dumps([{"score": 0.9, "a": "tools/x", "b": "tools/y", "reason": "tags: a, b"}])
    with patch("sys.stdin", io.StringIO(good)):
        with patch("okf.load_index"):
            # Concepts don't exist so they'll be skipped, but no validation error
            rc = okf.cmd_link(args)
    assert rc == 0


# --- cmd_view exposure warning (S-H4) ---------------------------------------

def test_cmd_view_prints_loopback_warning():
    """cmd_view prints a warning about loopback-only scope before serving."""
    args = okf.build_parser().parse_args(["view", "--no-open", "--no-index"])

    class _StopServer(Exception):
        """Raised to short-circuit serve_forever so the test doesn't hang."""
        pass

    with patch("okf.http.server.ThreadingHTTPServer") as MockServer:
        instance = MagicMock()
        instance.server_address = (okf.LOOPBACK, 8765)
        instance.serve_forever.side_effect = _StopServer()
        instance.server_close = MagicMock()
        MockServer.return_value = instance

        captured = io.StringIO()
        with patch("sys.stderr", captured):
            with pytest.raises(_StopServer):
                okf.cmd_view(args)

    output = captured.getvalue()
    assert "loopback" in output.lower()
    assert "not exposed" in output.lower()


# --- log chain (append-only integrity for log.md) ---------------------------

LOG_SAMPLE = (
    "# Log\n\n"
    "Preamble text.\n\n"
    "## [2026-09-01] ingest | first entry\n"
    "- did a thing\n\n"
    "## [2026-09-02] ingest | second entry\n"
    "- did another thing\n"
)


def _fresh_log(tmp_path):
    log = tmp_path / "log.md"
    state = tmp_path / "log.chain.jsonl"
    log.write_text(LOG_SAMPLE, encoding="utf-8")
    return log, state


def test_log_chain_missing_state_is_hard_error(tmp_path):
    log, state = _fresh_log(tmp_path)
    ok, msg, unsealed = okf.log_verify(log, state)
    assert not ok and "--bootstrap" in msg and unsealed == 0


def test_log_chain_bootstrap_and_verify(tmp_path):
    log, state = _fresh_log(tmp_path)
    ok, msg = okf.log_bootstrap(log, state)
    assert ok and "bootstrap" in msg
    head, stored = okf.log_load_state(state)
    assert len(stored) == 2 and head
    ok, msg, unsealed = okf.log_verify(log, state)
    assert ok and "OK" in msg and unsealed == 0


def test_log_chain_detects_tampered_entry(tmp_path):
    log, state = _fresh_log(tmp_path)
    okf.log_bootstrap(log, state)
    log.write_text(
        log.read_text(encoding="utf-8").replace(
            "- did another thing", "- did something else"),
        encoding="utf-8")
    ok, msg, _ = okf.log_verify(log, state)
    assert not ok
    assert "entry 2" in msg and "hash mismatch" in msg


def test_log_chain_detects_head_change(tmp_path):
    log, state = _fresh_log(tmp_path)
    okf.log_bootstrap(log, state)
    log.write_text(
        log.read_text(encoding="utf-8").replace(
            "# Log\n\n", "# Log\n\nExtra preamble line.\n\n"),
        encoding="utf-8")
    ok, msg, _ = okf.log_verify(log, state)
    assert not ok
    assert "head" in msg


def test_log_chain_detects_deleted_entry(tmp_path):
    log, state = _fresh_log(tmp_path)
    okf.log_bootstrap(log, state)
    text = log.read_text(encoding="utf-8")
    first = text.index("## [2026-09-01]")
    second = text.index("## [2026-09-02]")
    log.write_text(text[:first] + text[second:], encoding="utf-8")
    ok, msg, _ = okf.log_verify(log, state)
    assert not ok
    assert msg


def test_log_chain_append_unsealed_then_seal(tmp_path):
    log, state = _fresh_log(tmp_path)
    okf.log_bootstrap(log, state)
    with log.open("a", encoding="utf-8") as f:
        f.write("\n## [2026-09-03] ingest | third entry\n- more\n")
    ok, msg, unsealed = okf.log_verify(log, state)
    assert ok and "1 unsealed" in msg and unsealed == 1
    ok, msg = okf.log_seal(log, state)
    assert ok and "sealed 1" in msg
    ok, msg, unsealed = okf.log_verify(log, state)
    assert ok and "3 entries sealed" in msg and "unsealed" not in msg and unsealed == 0


def test_log_chain_detects_prepended_entry(tmp_path):
    """Inserting an entry between existing ones shifts every later entry's
    position — the chain must flag it (the log appends at EOF only)."""
    log, state = _fresh_log(tmp_path)
    okf.log_bootstrap(log, state)
    text = log.read_text(encoding="utf-8")
    second = text.index("## [2026-09-02]")
    log.write_text(
        text[:second]
        + "## [2026-09-01] ingest | sneaky entry\n- planted\n\n"
        + text[second:],
        encoding="utf-8")
    ok, msg, _ = okf.log_verify(log, state)
    assert not ok


# --- status (single gate) ---------------------------------------------------

def _status_env(tmp_path, monkeypatch, concepts):
    """Isolate status from the real repo: doctor runs on an empty tmp root."""
    monkeypatch.setattr(okf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(okf, "load_concepts", lambda: concepts)
    (tmp_path / "AGENTS.md").write_text(
        "# OKF Brain — Operating Contract\n\nSee rules/path-access-control.md.\n", encoding="utf-8")
    (tmp_path / "IDENTITY.md").write_text("# id\n", encoding="utf-8")
    (tmp_path / "CONTEXT.md").write_text("# ctx\n", encoding="utf-8")
    cfg = tmp_path / "_config"
    cfg.mkdir()
    (cfg / "taxonomy.md").write_text("# tax\n", encoding="utf-8")


def test_status_ready_when_clean(tmp_path, monkeypatch):
    a = make_concept("tools/a", {"type": "tool", "visibility": "shareable", "tags": ["x"]},
                     "body [b](/concepts/tools/b.md)")
    b = make_concept("tools/b", {"type": "tool", "visibility": "shareable", "tags": ["y"]},
                     "body [a](/concepts/tools/a.md)")
    _status_env(tmp_path, monkeypatch, [a, b])
    monkeypatch.setattr(okf, "log_verify", lambda *a, **k: (True, "OK: 3 entries sealed", 0))
    args = okf.build_parser().parse_args(["status"])
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = okf.cmd_status(args)
    finally:
        out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    assert "READY" in out


def test_status_needs_attention_on_warnings(tmp_path, monkeypatch):
    a = make_concept("tools/a", {"type": "tool", "visibility": "shareable"}, "body")  # orphan + no tags
    _status_env(tmp_path, monkeypatch, [a])
    monkeypatch.setattr(okf, "log_verify", lambda *a, **k: (True, "OK", 0))
    args = okf.build_parser().parse_args(["status"])
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = okf.cmd_status(args)
    finally:
        out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    assert "NEEDS ATTENTION" in out


def test_status_blocked_on_error_and_exit_1(tmp_path, monkeypatch):
    a = make_concept("tools/a", {"visibility": "shareable"}, "body")  # missing type → error
    _status_env(tmp_path, monkeypatch, [a])
    monkeypatch.setattr(okf, "log_verify", lambda *a, **k: (True, "OK", 0))
    args = okf.build_parser().parse_args(["status"])
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = okf.cmd_status(args)
    finally:
        out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 1
    assert "BLOCKED" in out and "missing-field" in out


def test_status_blocked_on_log_failure(tmp_path, monkeypatch):
    a = make_concept("tools/a", {"type": "tool", "visibility": "shareable", "tags": ["x"]}, "body")
    _status_env(tmp_path, monkeypatch, [a])
    monkeypatch.setattr(okf, "log_verify", lambda *a, **k: (False, "hash mismatch", 0))
    args = okf.build_parser().parse_args(["status"])
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = okf.cmd_status(args)
    finally:
        out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 1
    assert "BLOCKED" in out and "log FAIL" in out


# --- schema (CLI manifest) ----------------------------------------------------

def test_schema_lists_all_subcommands():
    args = okf.build_parser().parse_args(["schema"])
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = okf.cmd_schema(args)
    finally:
        out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    manifest = json.loads(out)
    names = set(manifest["commands"])
    assert {"index", "search", "lint", "backlinks", "affected", "relink", "doctor", "log",
            "status", "schema", "export", "view", "link", "suggest-links", "sql",
            "icm-sync"} <= names
    assert all(manifest["commands"][n]["help"] for n in names)
    assert "query" in manifest["commands"]["search"]["args"]


# --- export (shareable-only bundle) -------------------------------------------

def test_export_hard_filters_to_shareable(tmp_path, monkeypatch):
    (tmp_path / "concepts" / "tools").mkdir(parents=True)
    (tmp_path / "concepts" / "life").mkdir(parents=True)
    a = tmp_path / "concepts" / "tools" / "a.md"
    a.write_text("---\ntype: tool\nvisibility: shareable\n"
                 "title: A Tool\ndescription: shares well\n---\nbody\n", encoding="utf-8")
    b = tmp_path / "concepts" / "life" / "b.md"
    b.write_text("---\ntype: note\nvisibility: private\ntitle: Secret\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(okf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(okf, "CONCEPTS_DIR", tmp_path / "concepts")
    out_dir = tmp_path / "bundle"
    args = okf.build_parser().parse_args(["export", "--out", str(out_dir)])
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = okf.cmd_export(args)
    finally:
        out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    assert (out_dir / "concepts" / "tools" / "a.md").exists()
    assert not (out_dir / "concepts" / "life" / "b.md").exists()
    idx = json.loads((out_dir / "index.json").read_text(encoding="utf-8"))
    assert idx["count"] == 1 and idx["concepts"][0]["id"] == "tools/a"
    llms = (out_dir / "llms.txt").read_text(encoding="utf-8")
    assert "## [A Tool](/concepts/tools/a.md)" in llms
    assert "Secret" not in llms
    assert "1 shareable" in out and "1 private excluded" in out
    manifest = json.loads((out_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    mpaths = {m["path"] for m in manifest}
    assert {"index.json", "llms.txt", "sitemap.xml", "concepts/tools/a.md"} <= mpaths
    mfile = next(m for m in manifest if m["path"] == "concepts/tools/a.md")
    assert mfile["sha256"] == sha256((out_dir / "concepts" / "tools" / "a.md").read_bytes()).hexdigest()
    sitemap = (out_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>/concepts/tools/a.md</loc>" in sitemap and "<title>A Tool</title>" in sitemap


def test_export_copies_only_shareable_referenced_attachments(tmp_path, monkeypatch):
    (tmp_path / "concepts" / "tools").mkdir(parents=True)
    (tmp_path / "concepts" / "life").mkdir(parents=True)
    att = tmp_path / "raw" / "attachments"
    att.mkdir(parents=True)
    (att / "shared.png").write_bytes(b"\x89PNG-shared")
    (att / "secret.png").write_bytes(b"\x89PNG-secret")
    (att / "IMG encoded.png").write_bytes(b"\x89PNG-spc")
    a = tmp_path / "concepts" / "tools" / "a.md"
    a.write_text("---\ntype: tool\nvisibility: shareable\ntitle: A Tool\n"
                 "description: shares well\n---\n"
                 "![img](../../../raw/attachments/shared.png)\n"
                 "![enc](/raw/attachments/IMG%20encoded.png)\n"
                 "[evil](/raw/attachments/..%2Fsecret.png)\n", encoding="utf-8")
    b = tmp_path / "concepts" / "life" / "b.md"
    b.write_text("---\ntype: note\nvisibility: private\ntitle: Secret\n---\n"
                 "![img](../raw/attachments/secret.png)\n", encoding="utf-8")
    monkeypatch.setattr(okf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(okf, "CONCEPTS_DIR", tmp_path / "concepts")
    out_dir = tmp_path / "bundle"
    args = okf.build_parser().parse_args(["export", "--out", str(out_dir)])
    oldout, olderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        rc = okf.cmd_export(args)
    finally:
        out, err, sys.stdout, sys.stderr = (sys.stdout.getvalue(), sys.stderr.getvalue(),
                                            oldout, olderr)
    # ..%2F ref escapes the attachments dir: copier refuses it, verify flags it (hard error —
    # traversal refusal is a security guarantee, never a tolerated dead-ref)
    assert rc == 1
    assert "1 lost" in out
    assert "0 dead-refs" in out
    assert "escapes the attachments dir" in err
    assert (out_dir / "raw" / "attachments" / "shared.png").exists()
    assert (out_dir / "raw" / "attachments" / "IMG encoded.png").exists()
    # private-referenced attachment must NOT be copied (only shareable bodies are scanned;
    # and the ..%2F traversal ref must be refused even from a shareable body)
    assert not (out_dir / "raw" / "attachments" / "secret.png").exists()
    assert "2 attachment(s) copied" in out


# --- affected (transitive backlinks) -------------------------------------------

def test_transitive_inbound_chain_diamond_selfloop():
    # linear chain: b->a, c->b
    lm = {"tools/a": [], "tools/b": ["tools/a"], "learning/c": ["tools/b"]}
    assert okf._transitive_inbound(lm, {"tools/a"}) == {"tools/b", "learning/c"}
    # diamond: b->d, c->d, a->b, a->c  →  inbound to d = {a, b, c}
    lm = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
    assert okf._transitive_inbound(lm, {"d"}) == {"a", "b", "c"}
    # self-loop: root itself is never reported
    lm = {"a": ["a", "b"], "b": ["a"]}
    assert okf._transitive_inbound(lm, {"a"}) == {"b"}


def _affected_concepts():
    a = make_concept("tools/a", {"type": "tool", "visibility": "shareable", "title": "A"},
                     "root")
    b = make_concept("tools/b", {"type": "tool", "visibility": "shareable", "title": "B"},
                     "[A](/concepts/tools/a.md)")
    c = make_concept("tools/c", {"type": "tool", "visibility": "shareable", "title": "C"},
                     "[B](/concepts/tools/b.md)")
    return [a, b, c]


def test_cmd_affected_lists_transitive_inbound():
    args = okf.build_parser().parse_args(["affected", "tools/a"])
    with patch.object(okf, "load_concepts", return_value=_affected_concepts()):
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = okf.cmd_affected(args)
        finally:
            out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    assert "2 transitive inbound link(s) to /concepts/tools/a.md:" in out
    assert "  tools/b — B" in out and "  tools/c — C" in out
    assert "  tools/a" not in out  # root excluded


def test_cmd_affected_json_and_not_found():
    args = okf.build_parser().parse_args(["affected", "tools/a", "--json"])
    with patch.object(okf, "load_concepts", return_value=_affected_concepts()):
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = okf.cmd_affected(args)
        finally:
            out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    rows = json.loads(out)
    assert {r["id"] for r in rows} == {"tools/b", "tools/c"}
    assert all(r["path"].endswith(".md") for r in rows)

    args = okf.build_parser().parse_args(["affected", "no/such"])
    with patch.object(okf, "load_concepts", return_value=_affected_concepts()):
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            rc = okf.cmd_affected(args)
        finally:
            err, sys.stderr = sys.stderr.getvalue(), old
    assert rc == 1
    assert "not found" in err


def test_cmd_affected_git_seeds_and_git_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(okf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(okf, "load_concepts", lambda: _affected_concepts())

    class R:
        stdout = "concepts/tools/b.md\nconcepts/index.md\n"
    monkeypatch.setattr(okf.subprocess, "run", lambda cmd, **kw: R())
    args = okf.build_parser().parse_args(["affected", "tools/a", "--git", "HEAD"])
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = okf.cmd_affected(args)
    finally:
        out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    assert "1 transitive inbound link(s)" in out
    assert "  tools/b:" in out

    monkeypatch.setattr(okf.subprocess, "run",
                        MagicMock(side_effect=okf.subprocess.CalledProcessError(1, "git")))
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        rc = okf.cmd_affected(args)
    finally:
        err, sys.stderr = sys.stderr.getvalue(), old
    assert rc == 1
    assert "git failed" in err


# --- lint: status / trust / generated / legacy / actor ------------------------

def test_lint_bad_status_with_v02_hint():
    c = make_concept("tools/a", {"type": "tool", "visibility": "shareable", "status": "stable"})
    kinds = {(f["kind"], f["level"]) for f in okf.lint_concepts([c])}
    assert ("bad-status", "error") in kinds
    detail = next(f["detail"] for f in okf.lint_concepts([c]) if f["kind"] == "bad-status")
    assert "okf v0.2 value" in detail


def test_lint_malformed_trust_verified_scalar_and_trust_tier():
    c = make_concept("tools/a", {"type": "tool", "visibility": "shareable",
                                 "verified": "false", "trust_tier": "3"})
    finds = [f for f in okf.lint_concepts([c]) if f["kind"] == "malformed-trust"]
    assert len(finds) == 2  # verified not a list + trust_tier key
    assert all(f["level"] == "warn" for f in finds)


def test_lint_malformed_trust_ok_when_list_of_dicts():
    c = make_concept("tools/a", {"type": "tool", "visibility": "shareable",
                                 "verified": ["human:user", "agent:okf"]})
    assert not [f for f in okf.lint_concepts([c]) if f["kind"] == "malformed-trust"]


def test_lint_bad_generated_at_variants():
    c = make_concept("tools/a", {"type": "tool", "visibility": "shareable",
                                 "generated": "{by: human:user}"})
    f = next(f for f in okf.lint_concepts([c]) if f["kind"] == "bad-generated-at")
    assert f["level"] == "warn" and "generated without at" in f["detail"]
    c2 = make_concept("tools/a", {"type": "tool", "visibility": "shareable",
                                  "generated": "{by: human:user, at: 2026-01-02}"})
    f2 = next(f for f in okf.lint_concepts([c2]) if f["kind"] == "bad-generated-at")
    assert "2026-01-02" in f2["detail"]
    c3 = make_concept("tools/a", {"type": "tool", "visibility": "shareable",
                                  "generated": "{by: human:user, at: 2026-01-02T00:00:00Z}"})
    assert not [f for f in okf.lint_concepts([c3]) if f["kind"] == "bad-generated-at"]


def test_lint_legacy_timestamp_info():
    c = make_concept("tools/a", {"type": "tool", "visibility": "shareable",
                                 "timestamp": "2026-06-30T00:00:00Z"})
    f = next(f for f in okf.lint_concepts([c]) if f["kind"] == "legacy-timestamp")
    assert f["level"] == "info" and "v0.1" in f["detail"]


def test_lint_actor_format_bad_and_good():
    c = make_concept("tools/a", {"type": "tool", "visibility": "shareable",
                                 "generated": "{by: bad-actor, at: 2026-01-02T00:00:00Z}"})
    f = next(f for f in okf.lint_concepts([c]) if f["kind"] == "actor-format")
    assert f["level"] == "info" and "bad-actor" in f["detail"]
    c2 = make_concept("tools/b", {"type": "tool", "visibility": "shareable",
                                  "generated": "{by: human:user, at: 2026-01-02T00:00:00Z}"})
    assert not [f for f in okf.lint_concepts([c2]) if f["kind"] == "actor-format"]


def test_lint_clean_concept_no_new_kinds():
    c = make_concept("tools/a", {"type": "tool", "visibility": "shareable", "tags": ["dev"],
                                 "status": "active",
                                 "generated": "{by: human:user, at: 2026-01-02T00:00:00Z}"})
    new_kinds = {"bad-status", "malformed-trust", "bad-generated-at",
                 "legacy-timestamp", "actor-format"}
    assert not {f["kind"] for f in okf.lint_concepts([c])} & new_kinds


def test_export_ignore_and_escape(tmp_path, monkeypatch):
    (tmp_path / "concepts" / "tools").mkdir(parents=True)
    (tmp_path / "concepts" / "life").mkdir(parents=True)
    a = tmp_path / "concepts" / "tools" / "a.md"
    a.write_text("---\ntype: tool\nvisibility: shareable\ntitle: A & B <x>\n"
                 "---\n[B](/concepts/tools/b.md)\n", encoding="utf-8")
    b = tmp_path / "concepts" / "tools" / "b.md"
    b.write_text("---\ntype: tool\nvisibility: shareable\ntitle: B\n---\nbody\n", encoding="utf-8")
    for slug in ("s1", "s2"):
        (tmp_path / "concepts" / "life" / f"{slug}.md").write_text(
            "---\ntype: note\nvisibility: shareable\ntitle: S\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(okf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(okf, "CONCEPTS_DIR", tmp_path / "concepts")
    out_dir = tmp_path / "bundle"
    args = okf.build_parser().parse_args(["export", "--out", str(out_dir),
                                          "--ignore", "concepts/life/*"])
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = okf.cmd_export(args)
    finally:
        out, sys.stdout = sys.stdout.getvalue(), old
    assert rc == 0
    assert "2 shareable" in out and "0 private excluded" in out and "2 ignored" in out
    assert not (out_dir / "concepts" / "life").exists()
    idx = json.loads((out_dir / "index.json").read_text(encoding="utf-8"))
    assert idx["count"] == 2
    sitemap = (out_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "<title>A &amp; B &lt;x&gt;</title>" in sitemap
    manifest = json.loads((out_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    mfile = next(m for m in manifest if m["path"] == "concepts/tools/a.md")
    assert mfile["sha256"] == sha256((out_dir / "concepts" / "tools" / "a.md").read_bytes()).hexdigest()



def test_export_verify_flags_attachment_lost_after_copy(tmp_path, monkeypatch):
    # the only hard failure class: a ref that EXISTS in the vault but is missing
    # from the bundle (export lost a shared file)
    (tmp_path / "concepts" / "tools").mkdir(parents=True)
    att = tmp_path / "raw" / "attachments"
    att.mkdir(parents=True)
    (att / "shared.png").write_bytes(b"\x89PNG")
    a = tmp_path / "concepts" / "tools" / "a.md"
    a.write_text("---\ntype: tool\nvisibility: shareable\ntitle: A\n---\n"
                 "![x](/raw/attachments/shared.png)\n", encoding="utf-8")
    monkeypatch.setattr(okf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(okf, "CONCEPTS_DIR", tmp_path / "concepts")
    out_dir = tmp_path / "bundle"
    args = okf.build_parser().parse_args(["export", "--out", str(out_dir)])
    oldout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = okf.cmd_export(args)
    finally:
        out, sys.stdout = sys.stdout.getvalue(), oldout
    assert rc == 0
    assert (p := out_dir / "raw" / "attachments" / "shared.png").exists()
    p.unlink()  # simulate export losing a vault-present file
    c = make_concept("tools/a", {"type": "tool", "visibility": "shareable"},
                     "![x](/raw/attachments/shared.png)\n")
    files, links, errors, dangling, dead = okf._verify_bundle(out_dir, [c])
    assert len(errors) == 1 and "not in the bundle" in errors[0]


def test_export_dead_attachment_ref_is_tolerated(tmp_path, monkeypatch):
    # ref dead in the vault (file absent): tolerated dead-ref tally, rc stays 0
    (tmp_path / "concepts" / "tools").mkdir(parents=True)
    a = tmp_path / "concepts" / "tools" / "a.md"
    a.write_text("---\ntype: tool\nvisibility: shareable\ntitle: A\n---\n"
                 "![x](/raw/attachments/nope.png)\n", encoding="utf-8")
    monkeypatch.setattr(okf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(okf, "CONCEPTS_DIR", tmp_path / "concepts")
    out_dir = tmp_path / "bundle"
    args = okf.build_parser().parse_args(["export", "--out", str(out_dir)])
    oldout, olderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        rc = okf.cmd_export(args)
    finally:
        out, err, sys.stdout, sys.stderr = (sys.stdout.getvalue(), sys.stderr.getvalue(),
                                            oldout, olderr)
    assert rc == 0
    assert "verified: 1 files, 1 links checked, 0 lost, 0 dangling, 1 dead-refs" in out


def test_export_dangling_internal_link_is_warning_not_error(tmp_path, monkeypatch):
    # cross-visibility / not-yet-written links are tolerated (contract: broken
    # links never fatal) — they count as "dangling", rc stays 0
    (tmp_path / "concepts" / "tools").mkdir(parents=True)
    a = tmp_path / "concepts" / "tools" / "a.md"
    a.write_text("---\ntype: tool\nvisibility: shareable\ntitle: A\n---\n"
                 "See [B](/concepts/life/b.md) for the private side.\n", encoding="utf-8")
    monkeypatch.setattr(okf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(okf, "CONCEPTS_DIR", tmp_path / "concepts")
    out_dir = tmp_path / "bundle"
    args = okf.build_parser().parse_args(["export", "--out", str(out_dir)])
    oldout, olderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        rc = okf.cmd_export(args)
    finally:
        out, err, sys.stdout, sys.stderr = (sys.stdout.getvalue(), sys.stderr.getvalue(),
                                            oldout, olderr)
    assert rc == 0
    assert "verified: 1 files, 1 links checked, 0 lost, 1 dangling, 0 dead-refs" in out
    assert "not in the bundle" not in err
