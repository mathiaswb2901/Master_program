"""The content-search service: it finds matches, and it hides what the tree hides.

The behaviour under test is the walk and the ignore rules, not the wire shape — so
these drive :class:`SearchService` directly over a seeded temp workspace. The two
load-bearing claims are that a match inside a build cache (``CACHEDIR.TAG``) or a
named-ignored directory (``node_modules``) is never returned, because search and
the file tree must agree about what exists, and that a capped result says so.
"""

from collections.abc import Iterator
from pathlib import Path

from workbench_server.models.search import MAX_LINE_CHARS, SearchRequest
from workbench_server.services.search import SearchService
from workbench_server.services.workspace import Workspace

_CACHEDIR_TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n"


def _seed(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "model.py").write_text("PRICE_AREA = 'SE3'\nbid_curve = []\n", encoding="utf-8")
    (root / "notes.md").write_text("# Notes\n\nSE3 battery notes.\n", encoding="utf-8")
    (root / "readme.txt").write_text("nothing to see here\n", encoding="utf-8")

    # A named-ignored directory with a match inside — must never surface.
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("var SE3 = 1;\n", encoding="utf-8")

    # A build cache that tags itself disposable — the tree hides it, so must search.
    cache = root / "target"
    cache.mkdir()
    (cache / "CACHEDIR.TAG").write_bytes(_CACHEDIR_TAG)
    (cache / "artifact.txt").write_text("SE3 leaked from a build cache\n", encoding="utf-8")

    # A binary file carrying the query in its bytes — skipped by the NUL sniff.
    (root / "blob.bin").write_bytes(b"SE3\x00\x00binary\n")


def _service(root: Path) -> SearchService:
    return SearchService(Workspace(root))


def test_finds_matches_across_files_with_line_numbers(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = _service(tmp_path).search(SearchRequest(query="SE3"))

    by_path = {f.path: f for f in result.files}
    # Two visible files carry it: src/model.py and notes.md. The cache, the
    # node_modules copy and the binary are all excluded.
    assert set(by_path) == {"notes.md", "src/model.py"}
    assert result.files_with_matches == 2
    assert result.total_hits == 2
    assert result.truncated is False

    model_hit = by_path["src/model.py"].hits[0]
    assert model_hit.line == 1
    assert model_hit.col == 14  # the S of SE3 in PRICE_AREA = 'SE3'
    assert model_hit.text == "PRICE_AREA = 'SE3'"


def test_the_column_is_where_the_match_starts(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("....needle here\n", encoding="utf-8")
    hit = _service(tmp_path).search(SearchRequest(query="needle")).files[0].hits[0]
    assert hit.col == 4


def test_case_insensitive_by_default_and_exact_when_asked(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("MixedCase\n", encoding="utf-8")
    insensitive = _service(tmp_path).search(SearchRequest(query="mixedcase"))
    assert insensitive.total_hits == 1
    sensitive = _service(tmp_path).search(SearchRequest(query="mixedcase", case_sensitive=True))
    assert sensitive.files == []
    assert sensitive.total_hits == 0


def test_a_build_cache_and_ignored_dir_are_skipped(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = _service(tmp_path).search(SearchRequest(query="leaked from a build cache"))
    # The only file carrying this text sits under a CACHEDIR.TAG'd folder.
    assert result.files == []
    assert result.total_hits == 0
    assert result.truncated is False

    in_modules = _service(tmp_path).search(SearchRequest(query="var SE3"))
    assert in_modules.files == []


def test_a_binary_file_is_never_scanned(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"SE3\x00more\n")
    result = _service(tmp_path).search(SearchRequest(query="SE3"))
    assert result.files == []


def test_no_matches_is_an_honest_empty(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = _service(tmp_path).search(SearchRequest(query="no-such-token-anywhere"))
    assert result.files == []
    assert result.total_hits == 0
    assert result.files_with_matches == 0
    assert result.truncated is False
    assert result.query == "no-such-token-anywhere"


def test_truncation_states_the_cap_was_hit(tmp_path: Path) -> None:
    # Twelve matches, capped at five: the scan stops and says the window was cut.
    body = "".join(f"needle line {i}\n" for i in range(12))
    (tmp_path / "many.txt").write_text(body, encoding="utf-8")
    result = _service(tmp_path).search(SearchRequest(query="needle", max_results=5))
    assert result.total_hits == 5
    assert result.truncated is True


def test_the_exact_cap_with_no_more_is_not_truncated(tmp_path: Path) -> None:
    (tmp_path / "five.txt").write_text("".join("needle\n" for _ in range(5)), encoding="utf-8")
    result = _service(tmp_path).search(SearchRequest(query="needle", max_results=5))
    assert result.total_hits == 5
    assert result.truncated is False


def test_truncation_spans_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")
    result = _service(tmp_path).search(SearchRequest(query="needle", max_results=4))
    assert result.total_hits == 4
    assert result.truncated is True
    # The cap is respected across the file boundary, not per file.
    assert sum(len(f.hits) for f in result.files) == 4


def test_a_capped_file_followed_by_a_file_with_no_matches_is_not_truncated(
    tmp_path: Path,
) -> None:
    """The false positive the flag used to carry: another *file*, but no other hit.

    ``a.txt`` holds exactly ``max_results`` matches and ``b.txt`` holds none. The
    window really is the whole answer, so claiming otherwise sends both surfaces
    ("narrow the query") chasing matches that do not exist.
    """
    (tmp_path / "a.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("nothing of interest here\n", encoding="utf-8")
    result = _service(tmp_path).search(SearchRequest(query="needle", max_results=3))
    assert result.total_hits == 3
    assert result.truncated is False


def test_truncation_is_asserted_only_when_a_further_hit_exists(tmp_path: Path) -> None:
    """Same shape, but ``b.txt`` really does carry one more — now it is truncated."""
    (tmp_path / "a.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("nothing\nneedle\n", encoding="utf-8")
    result = _service(tmp_path).search(SearchRequest(query="needle", max_results=3))
    assert result.total_hits == 3
    assert result.truncated is True
    # The probe hit is not handed back — the cap is exact.
    assert [f.path for f in result.files] == ["a.txt"]


def test_a_very_long_line_is_clipped_with_a_flag(tmp_path: Path) -> None:
    long_line = "x" * 50 + "needle" + "y" * (MAX_LINE_CHARS * 2)
    (tmp_path / "wide.txt").write_text(long_line + "\n", encoding="utf-8")
    hit = _service(tmp_path).search(SearchRequest(query="needle")).files[0].hits[0]
    assert len(hit.text) == MAX_LINE_CHARS
    assert hit.line_truncated is True
    assert hit.text[hit.col : hit.col + len("needle")] == "needle"


def test_a_match_past_the_clip_is_still_inside_the_returned_text(tmp_path: Path) -> None:
    """The minified-bundle case: clipping from the start would lose the match.

    A hit whose preview does not contain what matched is worse than no preview,
    so the window is placed around the match and ``col`` is remapped into it.
    """
    long_line = "x" * (MAX_LINE_CHARS * 3) + "needle" + "y" * 100
    (tmp_path / "bundle.min.js").write_text(long_line + "\n", encoding="utf-8")
    hit = _service(tmp_path).search(SearchRequest(query="needle")).files[0].hits[0]

    assert "needle" in hit.text
    assert hit.text[hit.col : hit.col + len("needle")] == "needle"
    assert len(hit.text) <= MAX_LINE_CHARS
    assert hit.line_truncated is True
    assert hit.text.startswith("…")  # the elided head is stated, not silent


def test_the_walk_is_lazy_so_a_small_cap_does_not_enumerate_the_tree(
    tmp_path: Path,
) -> None:
    """A one-hit query stops walking; it does not first list every file."""
    (tmp_path / "a.txt").write_text("needle\nneedle\n", encoding="utf-8")
    service = _service(tmp_path)
    walked: list[Path] = []
    real_walk = service._walk

    def spy(root: Path) -> "Iterator[Path]":
        for path in real_walk(root):
            walked.append(path)
            yield path

    service._walk = spy  # type: ignore[method-assign]
    for i in range(50):
        (tmp_path / f"z{i:02d}.txt").write_text("needle\n", encoding="utf-8")

    result = service.search(SearchRequest(query="needle", max_results=1))
    assert result.total_hits == 1
    assert result.truncated is True
    # a.txt alone carries the cap *and* the probe hit — nothing after it is visited.
    assert walked == [tmp_path / "a.txt"]


def test_results_are_grouped_and_path_sorted(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text("match\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("match\nmatch\n", encoding="utf-8")
    result = _service(tmp_path).search(SearchRequest(query="match"))
    assert [f.path for f in result.files] == ["a.txt", "z.txt"]
    assert len(result.files[0].hits) == 2
