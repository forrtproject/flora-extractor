"""Compress OpenAlex cache entries without changing their JSON bytes or cache keys.

Run while cache writers are stopped. The default is a dry run; --apply streams,
verifies and publishes each gzip file before removing its unchanged plain source.
A restart skips completed entries. Conflicting gzip files are never overwritten.
"""
import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO

from .config import OA_CACHE_DIR


def _signature(path: Path) -> tuple[int, int, int]:
    """Detect replacement or modification before removing a source entry."""
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def _digest(handle: BinaryIO) -> str:
    """Hash a stream in bounded chunks, including gzip CRC verification at EOF."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1 << 20), b''):
        digest.update(chunk)
    return digest.hexdigest()


def compress_entry(source: Path) -> tuple[int, int]:
    """Return original/stored bytes; retain the original on any verification error."""
    before = _signature(source)
    mode = stat.S_IMODE(source.stat().st_mode)
    target = source.with_suffix('.json.gz')
    fd, name = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=source.parent)
    tmp = Path(name)
    try:
        original = hashlib.sha256()
        with os.fdopen(fd, 'wb') as out:
            # mkstemp creates 0600 and os.link keeps it; the entry keeps its own mode.
            os.fchmod(out.fileno(), mode)
            with gzip.GzipFile(filename='', fileobj=out, mode='wb', compresslevel=6, mtime=0) as gz:
                with source.open('rb') as src:
                    for chunk in iter(lambda: src.read(1 << 20), b''):
                        original.update(chunk)
                        gz.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        with gzip.open(tmp, 'rb') as check:
            if _digest(check) != original.hexdigest():
                raise ValueError(f'Gzip verification failed: {source.name}')
        if _signature(source) != before:
            raise RuntimeError(f'Source changed during compression: {source.name}')
        try:
            os.link(tmp, target)
        except FileExistsError:
            with gzip.open(target, 'rb') as existing:
                if _digest(existing) != original.hexdigest():
                    raise ValueError(f'Conflicting compressed entry: {source.name}')
        directory_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if _signature(source) != before:
            raise RuntimeError(f'Source changed before removal: {source.name}')
        source.unlink()
        return before[1], target.stat().st_size
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    """Count entries by default, or migrate with a bounded worker queue."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--workers', type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    files = sorted(OA_CACHE_DIR.glob('*.json'))
    total = sum(p.stat().st_size for p in files)
    print(json.dumps({'plain_files': len(files), 'plain_bytes': total, 'apply': args.apply}), flush=True)
    if not args.apply:
        return
    original = stored = done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        # Keep only a bounded number of futures, rather than 200k pending jobs.
        for offset in range(0, len(files), 200):
            for old, new in pool.map(compress_entry, files[offset:offset + 200]):
                original += old
                stored += new
                done += 1
            if done % 1000 == 0 or done == len(files):
                print(json.dumps({'done': done, 'total': len(files), 'original_bytes': original,
                                  'compressed_bytes': stored, 'saved_bytes': original - stored}), flush=True)
    print('compression_complete', flush=True)


if __name__ == '__main__':
    main()
