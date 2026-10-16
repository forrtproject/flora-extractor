"""Compressed OpenAlex entries must remain hits and share the same remote bytes."""
import gzip
import json
from unittest.mock import Mock

from shared import cache, cache_sync, config, openalex_client, pdf_sources


def compressed(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
    path.with_suffix(path.suffix + '.gz').write_bytes(gzip.compress(raw, mtime=0))
    return raw


def test_openalex_references_hit_gzip_without_api(tmp_path, monkeypatch):
    expected = [{'id': 'W2', 'title': 'Résumé'}]
    compressed(tmp_path / 'refs_W1.json', expected)
    monkeypatch.setattr(openalex_client, 'OA_CACHE_DIR', tmp_path)
    api = Mock(side_effect=AssertionError('cache hit must not call OpenAlex'))
    monkeypatch.setattr(openalex_client, '_oa_get', api)
    assert openalex_client.fetch_referenced_works_metadata('W1') == expected
    api.assert_not_called()


def test_acquisition_cached_absence_hits_gzip(tmp_path, monkeypatch):
    compressed(tmp_path / f'unpaywall_{pdf_sources.cache_key("10.1/example")}.json', {'__none__': True})
    monkeypatch.setattr(pdf_sources, 'OA_CACHE_DIR', tmp_path)
    api = Mock(side_effect=AssertionError('cache hit must not call Unpaywall'))
    monkeypatch.setattr(pdf_sources.requests, 'get', api)
    assert pdf_sources._fetch_unpaywall_data('10.1/example') is None
    api.assert_not_called()


def test_writes_compress_only_openalex_and_preserve_old_reads(tmp_path, monkeypatch):
    oa = tmp_path / 'openalex'
    monkeypatch.setattr(config, 'OA_CACHE_DIR', oa)
    oa.mkdir()
    (oa / 'answer.json').write_text('{"old": true}')
    cache.write_cache(oa, 'answer', {'new': 'é'})
    assert not (oa / 'answer.json').exists()
    assert json.loads(gzip.decompress((oa / 'answer.json.gz').read_bytes())) == {'new': 'é'}
    assert cache.read_cache(oa, 'answer') == {'new': 'é'}
    cache.write_cache(tmp_path / 'llm', 'answer', {'plain': True})
    assert (tmp_path / 'llm/answer.json').exists()


def test_clear_removes_both_encodings(tmp_path):
    key = cache.content_key('candidates', '10.1/example', 'v1')
    (tmp_path / f'{key}.json').write_text('{}')
    compressed(tmp_path / f'{key}.json', {})
    assert len(cache.clear_content_keys(tmp_path, 'candidates', '10.1/example')) == 2
    assert cache.read_cache(tmp_path, key) is None
    compressed(tmp_path / 'other.json', {})
    cache.clear_cache(tmp_path, 'other')
    assert cache.read_cache(tmp_path, 'other') is None


def test_sync_compressed_entry_keeps_shard_name_and_archive_digest(tmp_path):
    entries = tmp_path / 'entries'
    entries.mkdir()
    part = cache_sync.Part('openalex', entries, '*.json', 1)
    path = entries / 'answer.json'
    raw = json.dumps({'answer': 'é'}, ensure_ascii=False, indent=2).encode()
    path.write_bytes(raw)
    original = cache_sync.pack_shard([path], tmp_path / 'before.tar.gz')
    expected_shard = part.shard_of(path.name)
    compressed(path, {'answer': 'é'})
    path.unlink()
    shards = cache_sync._shards_of(part)
    assert list(shards) == [expected_shard]
    assert cache_sync.pack_shard(shards[expected_shard], tmp_path / 'after.tar.gz') == original


def test_sync_pull_recognizes_existing_compressed_entry(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'answer.json').write_text('{"remote": true}')
    archive = tmp_path / 'remote.tar.gz'
    cache_sync.pack_shard([source / 'answer.json'], archive)
    local = tmp_path / 'local'
    local.mkdir()
    compressed(local / 'answer.json', {'local': True})
    part = cache_sync.Part('openalex', local, '*.json', 1)
    cache_sync._unpack(part, archive, set())
    assert not (local / 'answer.json').exists()
    assert cache.read_cache(local, 'answer') == {'local': True}


def test_sync_pull_compresses_new_openalex_entries(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'answer.json').write_text('{"remote": true}')
    archive = tmp_path / 'remote.tar.gz'
    cache_sync.pack_shard([source / 'answer.json'], archive)
    local = tmp_path / 'local'
    cache_sync._unpack(cache_sync.Part('openalex', local, '*.json', 1), archive, set())
    assert (local / 'answer.json.gz').exists()
    assert cache.read_cache(local, 'answer') == {'remote': True}


def test_migration_preserves_original_bytes_and_resumes(tmp_path):
    from shared.compress_cache import compress_entry
    path = tmp_path / 'answer.json'
    raw = b'{ "spacing": [1, 2], "unicode": "\\u00e9" }\n'
    path.write_bytes(raw)
    original, stored = compress_entry(path)
    assert original == len(raw)
    assert not path.exists()
    assert gzip.decompress((tmp_path / 'answer.json.gz').read_bytes()) == raw
    path.write_bytes(raw)
    assert compress_entry(path) == (original, stored)


def test_migration_conflict_retains_plain_source(tmp_path):
    import pytest
    from shared.compress_cache import compress_entry
    path = tmp_path / 'answer.json'
    path.write_text('{"plain": true}')
    compressed(path, {'different': True})
    with pytest.raises(ValueError, match='Conflicting compressed entry'):
        compress_entry(path)
    assert path.read_text() == '{"plain": true}'
    assert not list(tmp_path.glob('*.tmp'))
