"""Tests for the memoised sidecar key index (shared/csv_index.py).

The snapshot scanner merges once per pyarrow batch — thousands of times per run —
and every merge starts by loading the candidates index, which at 7M keys costs
seconds and a GB of RSS. The load is therefore memoised, and the only thing worth
testing about a cache is when it must NOT be trusted.
"""

import pandas as pd
import pytest

from shared.csv_index import KeyIndex


def _keys(row: dict) -> list[str]:
    return [str(row.get("doi_r") or "")]


@pytest.fixture
def index(tmp_path) -> KeyIndex:
    return KeyIndex(tmp_path / "index.txt", _keys, "Test")


def test_a_second_load_does_not_touch_the_disk(index):
    index.save({"a", "b"})
    first = index.load()

    assert first == {"a", "b"}
    assert index.load() is first, "the index must be read from disk once, not per call"


def test_append_keeps_the_cached_set_and_the_file_in_step(index):
    index.save({"a"})
    index.load()
    index.append(["b"])

    assert index.load() == {"a", "b"}
    assert set(index.path.read_text(encoding="utf-8").split()) == {"a", "b"}


def test_a_rebuild_replaces_the_cached_copy(index, tmp_path):
    """Recovery after a crashed merge rebuilds the index from the CSV. A rebuild that
    only fixed the file on disk would be no recovery at all: the merge reads the cache."""
    csv_path = tmp_path / "candidates.csv"
    index.save({"stale"})
    index.load()

    pd.DataFrame({"doi_r": ["10.1/a", "10.1/b"]}).to_csv(csv_path, index=False,
                                                        encoding="utf-8-sig")
    index.build(csv_path)

    assert index.load() == {"10.1/a", "10.1/b"}


def test_a_file_changed_underneath_us_is_read_again(index):
    """Another process (or a hand edit) can grow the index file. The cache is keyed on
    the file's size and mtime, so a change outside this instance invalidates it."""
    index.save({"a"})
    index.load()

    with open(index.path, "a", encoding="utf-8") as f:
        f.write("c\n")

    assert index.load() == {"a", "c"}


def test_appending_to_another_file_drops_the_cache_rather_than_moving_it(index, tmp_path):
    """`path` is repointed between runs (and between tests). An append must never fold
    the previous file's keys into the cache it then serves for the new one — those rows
    are not in the new file, and every one of them would be skipped as a duplicate."""
    index.save({"old"})
    index.load()

    index.path = tmp_path / "other.txt"
    index.append(["new"])

    assert index.load() == {"new"}


def test_a_repointed_index_does_not_serve_the_old_files_keys(index, tmp_path):
    """Tests monkeypatch `path` onto a tmp file after construction; a cache keyed only
    on content would hand the previous file's keys to the new one."""
    index.save({"a"})
    index.load()

    index.path = tmp_path / "other.txt"
    assert index.load() == set()
