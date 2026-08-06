"""Per-stage token attribution: which bucket a recorded call lands in.

The counter's whole job is answering "what did each stage cost", and it is read
once, at the end of a run, from a table nothing else checks. The seams are: the
stage a thread set is the bucket its tokens go to, switching the stage moves the
next call and not the previous one, a thread that never set one is "unknown"
rather than inheriting another thread's stage, and get_summary() hands back a copy.
"""
import threading

import pytest

from shared import token_counter


@pytest.fixture(autouse=True)
def _empty_counts(monkeypatch):
    """A fresh table per test — the real one is module state for the whole run."""
    from collections import defaultdict

    monkeypatch.setattr(token_counter, "_counts",
                        defaultdict(lambda: defaultdict(int)))
    monkeypatch.setattr(token_counter, "_local", threading.local())


def test_tokens_are_attributed_to_the_stage_the_thread_set():
    token_counter.set_stage("extract_outcome")
    token_counter.record("gemini", 100)
    token_counter.record("gemini", 50)
    token_counter.record("openai", 7)
    assert token_counter.get_summary() == {
        "extract_outcome": {"gemini": 150, "openai": 7}}


def test_switching_stage_moves_only_the_calls_after_it():
    token_counter.set_stage("extract_abstract")
    token_counter.record("gemini", 10)
    token_counter.set_stage("extract_fulltext")
    token_counter.record("gemini", 30)
    assert token_counter.get_summary() == {
        "extract_abstract": {"gemini": 10},
        "extract_fulltext": {"gemini": 30}}


def test_a_thread_that_never_set_a_stage_records_unknown():
    """Not the main thread's stage — that would mis-bill another row's tokens."""
    token_counter.set_stage("engine_screen_expensive")

    def worker() -> None:
        token_counter.record("openrouter", 5)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert token_counter.get_summary() == {"unknown": {"openrouter": 5}}


def test_each_thread_keeps_its_own_stage():
    token_counter.set_stage("extract_abstract")

    def worker() -> None:
        token_counter.set_stage("extract_outcome")
        token_counter.record("gemini", 2)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    token_counter.record("gemini", 1)
    assert token_counter.get_summary() == {
        "extract_abstract": {"gemini": 1}, "extract_outcome": {"gemini": 8}}


@pytest.mark.parametrize("n", [0, -3])
def test_a_non_positive_count_creates_no_bucket(n):
    # A provider that reported no usage must not invent a stage in the summary.
    token_counter.set_stage("extract_refscreen")
    token_counter.record("gemini", n)
    assert token_counter.get_summary() == {}


def test_get_summary_returns_a_copy_the_caller_cannot_write_through():
    token_counter.set_stage("extract_outcome")
    token_counter.record("gemini", 10)
    summary = token_counter.get_summary()
    summary["extract_outcome"]["gemini"] = 999
    summary["invented"] = {"gemini": 1}
    assert token_counter.get_summary() == {"extract_outcome": {"gemini": 10}}


def test_print_summary_says_so_when_nothing_was_recorded(capsys):
    token_counter.print_summary()
    assert "no LLM calls recorded" in capsys.readouterr().out


def test_print_summary_totals_every_stage_and_provider(capsys):
    token_counter.set_stage("extract_abstract")
    token_counter.record("gemini", 1000)
    token_counter.set_stage("extract_outcome")
    token_counter.record("openai", 250)
    token_counter.print_summary()
    out = capsys.readouterr().out
    assert "extract_abstract" in out and "1,000" in out
    assert "TOTAL" in out and "1,250" in out
