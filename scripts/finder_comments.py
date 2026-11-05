#!/usr/bin/env python3
"""Restore missing Finder Get Info comments from indexed file attributes."""
import argparse
from collections import defaultdict
from pathlib import Path
import shutil
import stat
import sys
import tempfile

import media

COMMENT_CODE = b"cmmt"


def existing_records(path, store_class):
    if not path.exists():
        return []
    with store_class.open(str(path), "r") as store:
        return list(store)


def update_store(path, records, notes, backup, store_class, entry_class):
    previous = media.fingerprint(path)
    if previous:
        backup.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as source, backup.open("xb") as copy:
            shutil.copyfileobj(source, copy)
    with tempfile.TemporaryDirectory(prefix=".media-preflight-finder-", dir=path.parent) as temp:
        staged = Path(temp) / path.name
        replacements = {media.key(name) for name in notes}
        entries = [item for item in records
                   if item.code != COMMENT_CODE or media.key(item.filename) not in replacements]
        entries.extend(entry_class(name, COMMENT_CODE, b"ustr", comment) for name, comment in notes.items())
        media.require(all(item.byte_length() <= 4084 for item in entries),
                      f"An existing Finder record or comment exceeds the safe page size: {path}")
        with store_class.open(str(staged), "w+", initial_entries=entries):
            pass
        if previous:
            staged.chmod(stat.S_IMODE(path.stat().st_mode))
        def values(items):
            return {(item.filename, item.code): (item.type, item.value) for item in items}
        media.require(values(existing_records(staged, store_class)) == values(entries),
                      f"Cannot verify staged Finder settings and comments: {path}")
        media.require(media.fingerprint(path) == previous and not path.is_symlink(),
                      f"Finder folder settings changed during the update: {path}")
        staged.replace(path)
        media.sync_dir(path.parent)


def sync_comments(root, kind, backup_root, store_class, entry_class, replace_existing=False):
    index = media.document_at(root)
    media.require(index and index["mode"] == "current", "Run media.py render to generate a current JSON index.")
    groups = defaultdict(dict)
    skipped = 0
    for row in index["assets"]:
        if kind and row["kind"] != kind:
            continue
        path = media.source_path(root, row["path"])
        if not media.fingerprint(path):
            skipped += 1
            continue
        note = media.finder_metadata(path).get("finder_comment")
        if note:
            groups[path.parent][path.name] = note
        else:
            skipped += 1
    missing = 0
    preserved = 0
    different = 0
    updated = []
    for folder, candidates in groups.items():
        store_path = folder / ".DS_Store"
        media.require(not store_path.is_symlink() and (not store_path.exists() or store_path.is_file()),
                      f"Invalid Finder settings file: {store_path}")
        stamp = media.fingerprint(store_path)
        records = existing_records(store_path, store_class)
        existing = {media.key(record.filename): record.value for record in records if record.code == COMMENT_CODE}
        notes = {}
        for filename, note in candidates.items():
            value = existing.get(media.key(filename))
            if value:
                different += int(value != note)
                if value == note or not replace_existing:
                    preserved += 1
                    continue
            notes[filename] = note
        missing += len(notes)
        if notes and backup_root:
            media.require(media.fingerprint(store_path) == stamp, f"Finder settings changed: {store_path}")
            update_store(store_path, records, notes, backup_root / store_path.relative_to(root),
                         store_class, entry_class)
            updated.append(str(folder))
    return {"missing": missing, "written": missing if backup_root else 0, "preserved": preserved,
            "different_existing": different, "skipped": skipped, "updated_folders": updated}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--kind", choices=("video", "image"))
    parser.add_argument("--backup-dir", type=Path, help="Apply the repair and save original folder settings here.")
    parser.add_argument("--replace-existing", action="store_true",
                        help="Also replace Finder display comments that differ from the file description.")
    args = parser.parse_args()
    try:
        media.require(sys.platform == "darwin", "Finder comment repair requires macOS.")
        from ds_store import DSStore, DSStoreEntry
        root = args.directory.resolve(strict=True)
        backup = args.backup_dir.resolve() if args.backup_dir else None
        if backup:
            media.require(not backup.is_relative_to(root), "Backup directory must be outside the media library.")
        sys.stdout.buffer.write(media.encode(sync_comments(root, args.kind, backup, DSStore, DSStoreEntry, args.replace_existing)))
    except (ImportError, OSError, ValueError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
