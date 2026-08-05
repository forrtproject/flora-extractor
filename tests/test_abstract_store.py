"""Tests for the abstract store.

One test per seam: the three-way answer that used to need two files, the merge
that keeps a local answer, the per-source evidence `cache_sync` shares, the
miss-only delete, and the migration off the file-per-identifier layout.

The store is pointed at a throwaway database by the autouse fixture in
``conftest.py`` — no test can reach the real one.
"""

import json

from shared import abstract_store as store


def test_never_tried_and_tried_with_nothing_are_different_answers():
    """The distinction the old layout needed a cache file AND a checkpoint line to
    express: "nobody has asked" versus "asked, and this source has nothing"."""
    assert store.lookup("epmc:10.1/x") == (False, None)
    store.record("epmc:10.1/x", None)
    assert store.lookup("epmc:10.1/x") == (True, None)
    store.record("epmc:10.1/x", "Body")
    assert store.lookup("epmc:10.1/x") == (True, "Body")


def test_import_keeps_the_local_answer():
    """A pull merges. The local row was produced by this machine's configuration;
    the remote's is at best the same one."""
    store.record("epmc:10.1/x", "mine")
    written = store.import_rows([("epmc:10.1/x", "theirs"), ("epmc:10.1/y", "new")])
    assert written == 1
    assert store.lookup("epmc:10.1/x") == (True, "mine")
    assert store.lookup("epmc:10.1/y") == (True, "new")


def test_source_evidence_counts_hits_and_misses_per_namespace():
    """What `cache_sync` shares so a puller can tell a source that had nothing
    from a source the pusher could not read."""
    store.record_many([("scopus:10.1/a", "Body"), ("scopus:10.1/b", None),
                       ("epmc:10.1/c", None)])
    assert store.source_evidence() == {
        "scopus": {"hits": 1, "misses": 1},
        "epmc": {"hits": 0, "misses": 1},
    }


def test_drop_misses_reopens_only_the_misses():
    """Reopening an unproven source must not throw away the abstracts it did find."""
    store.record_many([("scopus:10.1/a", "Body"), ("scopus:10.1/b", None),
                       ("epmc:10.1/c", None)])
    assert store.drop_misses(["scopus"]) == 1
    assert store.lookup("scopus:10.1/a") == (True, "Body")
    assert store.lookup("scopus:10.1/b") == (False, None)
    assert store.lookup("epmc:10.1/c") == (True, None)


def test_migration_takes_the_files_and_the_checkpoint_only_identifiers(tmp_path):
    """`__none__` becomes NULL, and an identifier the old batch phase checkpointed
    without writing a cache file comes in as the miss that line meant."""
    old = tmp_path / "abstracts"
    old.mkdir()
    for name, ident, abstract in [("1.json", "epmc:10.1/a", "Body"),
                                  ("2.json", "epmc:10.1/b", "__none__")]:
        (old / name).write_text(json.dumps({"ident": ident, "abstract": abstract}))
    checkpoint = tmp_path / "done.txt"
    checkpoint.write_text("epmc:10.1/a\nepmc:10.1/b\noa:W99\n")

    stats = store.migrate_from_files(old, checkpoint)

    assert stats["rows"] == 3 and stats["hits"] == 1 and stats["checkpoint_only"] == 1
    assert store.lookup("epmc:10.1/a") == (True, "Body")
    assert store.lookup("epmc:10.1/b") == (True, None)
    assert store.lookup("oa:W99") == (True, None)
