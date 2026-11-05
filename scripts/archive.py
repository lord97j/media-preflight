#!/usr/bin/env python3
"""Preview and apply media moves into a classified library."""
import argparse
import base64
from datetime import date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import media

JOURNAL = "archive-transaction.json"


def directory(path):
    result = path.resolve(strict=True)
    media.require(result.is_dir(), f"Not a directory: {path}")
    return result


def state_dir(path):
    state = path / media.STATE
    media.require(not state.is_symlink() and (not state.exists() or state.is_dir()),
                  f"Invalid catalog directory: {state}")
    return state


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_file(path):
    if not os.path.lexists(path):
        return None
    media.require(path.is_file() and not path.is_symlink(), f"Invalid file: {path}")
    return path.read_bytes()


def published_files(folder):
    media.document_at(folder)
    files = [folder / media.INDEX_DOCUMENT]
    legacy = media.legacy_document(folder)
    if legacy:
        files.append(legacy)
    return files


def published_digest(folder):
    return {path.name: digest(data) for path in published_files(folder)
            if (data := read_file(path)) is not None}


def folder_for(row):
    return media.folder_for(row)


def align_name(name, kind, category):
    return media.align_name(name, kind, category)


def destination_folder(library, relative):
    folder = library / relative
    media.require(folder.resolve() == folder and folder.is_relative_to(library),
                  f"Invalid archive folder: {folder}")
    parent = folder
    while not os.path.lexists(parent):
        parent = parent.parent
    media.require(parent.is_dir(), f"Archive parent is not a directory: {parent}")
    return folder, parent


def companion_moves(source, library, path, target, row):
    peers = media.sidecars(path)
    if not peers:
        return []
    media.require(row.get("archive_companions") is True and row["kind"] == "video",
                  f"Paired files cannot be archived individually: {path}")
    result = []
    for name in peers:
        peer = path.with_name(name)
        stamp = media.fingerprint(peer)
        media.require(stamp and peer.stem == path.stem and peer.suffix.lower() in {".srt", ".lrf"},
                      f"Unsupported companion: {peer}")
        destination = target.with_suffix(peer.suffix)
        result.append({"source": str(peer.relative_to(source)),
                       "target": str(destination.relative_to(library)), "fingerprint": stamp})
    return result


def group_members(row):
    return [row, *row.get("companions", [])]


def validate_group(origin, row):
    expected = {Path(companion["source"]).name for companion in row.get("companions", [])}
    media.require(set(media.archive_sidecars(origin)) == expected, f"Companion set changed: {origin}")


def make_plan(source, library, review):
    media.require(not library.is_relative_to(source), "Library must be outside the source directory.")
    state = state_dir(source)
    media.require(not os.path.lexists(state / "transaction.json"), "Source has an interrupted batch.")
    index = media.index_at(state)
    media.require(review["root"] == str(source) and review["generation"] == index["generation"],
                  "Source directory or catalog changed. Scan again.")
    source_set, destinations, folders, rows = set(), set(), {}, []
    for row in review["rows"]:
        if row["action"] == "skip":
            continue
        media.require(row["action"] == "archive", "Review actions must be archive or skip.")
        path = media.source_path(source, row["source"])
        stamp = media.fingerprint(path)
        media.require(path not in source_set and stamp and stamp[2] > 0 and stamp == row["fingerprint"]
                      and media.kind(path) == row["kind"], f"Missing, repeated or changed source: {path}")
        source_set.add(path)
        annotation = media.annotation_with_folder(row["annotation"], path, state)
        media.require_voice(annotation, path)
        relative = folder_for(row)
        standard_name = align_name(row["standard_name"], row["kind"], row["archive_type"])
        media.check_name(standard_name, path, review["pattern"])
        folder, parent = destination_folder(library, relative)
        media.require(not folder.is_relative_to(source), f"Archive destination overlaps source: {folder}")
        media.require(stamp[0] == parent.stat().st_dev, f"Archive requires the same volume: {path} / {folder}")
        target = folder / standard_name
        companions = companion_moves(source, library, path, target, row)
        if folder not in folders:
            dest_state = state_dir(folder)
            media.require(not os.path.lexists(dest_state / "transaction.json"),
                          f"Destination has an interrupted batch: {folder}")
            dest_index = media.index_at(dest_state)
            occupied = {media.key(peer.name) for peer in folder.iterdir()} if folder.exists() else set()
            occupied.update(media.key(entry["standard_name"]) for current, entry in dest_index["assets"].items()
                            if media.fingerprint(media.source_path(folder, current)) == entry["fingerprint"])
            folders[folder] = (dest_index, occupied, published_digest(folder))
        dest_index, occupied, index_digests = folders[folder]
        media.check_name(standard_name, path, dest_index.get("pattern", review["pattern"]))
        for member in [target, *(library / companion["target"] for companion in companions)]:
            normalized = media.key(str(member))
            media.require(normalized not in destinations, f"Duplicate destination: {member}")
            media.require(media.key(member.name) not in occupied, f"Destination name occupied: {member}")
            destinations.add(normalized)
        previous = index["assets"].get(row["source"], {})
        prepared = {"source": row["source"], "kind": row["kind"], "action": "archive",
                     "fingerprint": stamp, "standard_name": standard_name,
                     "annotation": annotation, "archive_type": row["archive_type"],
                     "target": str(relative / target.name),
                     "original_path": previous.get("original_path", row["source"]),
                     "write_file_description": True, "prior_file_comment": media.file_comment(path),
                     "prior_file_tags": media.file_attribute(path, media.TAGS_ATTR),
                     "destination_generation": dest_index["generation"], "destination_index_digests": index_digests}
        if companions:
            prepared.update(archive_companions=True, companions=companions)
        media.planned_tags(prepared)
        rows.append(prepared)
    media.require(rows, "No rows selected for archiving.")
    return {"root": str(source), "library": str(library), "generation": index["generation"],
            "pattern": review["pattern"], "rows": rows}


def snapshot(path):
    old = read_file(path)
    return base64.b64encode(old).decode() if old is not None else None


def restore_files(backups):
    for filename, contents in backups.items():
        path = Path(filename)
        if contents is None:
            if os.path.lexists(path):
                media.require(path.is_file() and not path.is_symlink(), f"Cannot restore: {path}")
                path.unlink()
                media.sync_dir(path.parent)
        else:
            media.save_bytes(path, base64.b64decode(contents))


def rollback(journal, batch):
    media.restore(Path(batch["common_root"]), batch["moves"])
    restore_files(batch["backups"])
    journal.unlink()
    media.sync_dir(journal.parent)


def archive_record(documents, source, library, rows):
    by_source = {row["source"]: row for row in rows}
    for document in documents:
        if document.suffix != ".json" or not document.exists():
            continue
        doc = media.read(document)
        media.require(isinstance(doc, dict) and doc.get("generated_by") == media.GENERATED_BY,
                      f"Not a media-preflight index: {document}")
        for asset in doc["assets"]:
            row = by_source.get(asset["path"])
            if not row:
                continue
            asset["status"] = "archived"
            asset["archived_path"] = str(library / row["target"])
            asset["archived_filename"] = Path(row["target"]).name
            asset.setdefault("original_filename", asset.get("filename"))
            asset.setdefault("source_path", str((source / row["source"]).resolve()))
        doc["mode"] = "archived"
        media.save(document, doc)


def execute(source, library, journal, rows, pattern, keep_source_document=False):
    source_documents = published_files(source)
    source_index = state_dir(source) / "index.json"
    source_catalog = media.index_at(source_index.parent)
    common = Path(os.path.commonpath([source, library]))
    groups, moves, primary_moves = {}, [], []
    for row in rows:
        target = library / row["target"]
        groups.setdefault(target.parent, []).append(row)
        primary_moves.append(row)
        for member in group_members(row):
            moves.append({**member, "source": str((source / member["source"]).relative_to(common)),
                          "target": str((library / member["target"]).relative_to(common))})
    export_folders = {library}
    for folder in groups:
        while folder not in export_folders:
            export_folders.add(folder)
            folder = folder.parent
    ordered_exports = sorted(export_folders, key=lambda folder: (-len(folder.parts), str(folder)))
    files = {*source_documents, source_index}
    for folder in ordered_exports:
        files.update(published_files(folder))
        if folder in groups:
            files.add(state_dir(folder) / "index.json")
    batch = {"source": str(source), "common_root": str(common), "moves": moves,
             "backups": {str(p): snapshot(p) for p in files}}
    media.save(journal, batch)
    try:
        for folder in groups:
            folder.mkdir(parents=True, exist_ok=True)
            state_dir(folder).mkdir(mode=0o700, exist_ok=True)
        for row in primary_moves:
            origin = media.source_path(source, row["source"])
            target = media.source_path(library, row["target"])
            validate_group(origin, row)
            if "prior_file_comment" not in row:
                row["prior_file_comment"] = media.file_comment(origin)
                row["prior_file_tags"] = media.file_attribute(origin, media.TAGS_ATTR)
            for member in group_members(row):
                path = media.source_path(source, member["source"])
                media.require(media.fingerprint(path) == member["fingerprint"], f"Source changed: {path}")
            media.require(media.file_comment(origin) == row["prior_file_comment"], f"Comment changed: {origin}")
            media.require(media.file_attribute(origin, media.TAGS_ATTR) == row["prior_file_tags"],
                          f"Tags changed: {origin}")
            media.move_exclusive(origin, target, sync=False)
            media.set_comment(target, media.planned_comment(row))
            media.set_attribute(target, media.TAGS_ATTR, media.planned_tags(row))
            for member in row.get("companions", []):
                media.move_exclusive(media.source_path(source, member["source"]),
                                     media.source_path(library, member["target"]), sync=False)
        for folder in {folder for folder in groups} | {source}:
            media.sync_dir(folder)
        for folder, folder_rows in groups.items():
            state = state_dir(folder)
            index = media.index_at(state)
            for row in folder_rows:
                target = Path(row["target"]).name
                entry = {k: row[k] for k in ("kind", "fingerprint", "standard_name", "annotation")}
                entry["original_path"] = row.get("original_path", row["source"])
                entry["original_filename"] = row.get("original_filename") or Path(row["source"]).name
                entry["source_path"] = row.get("source_path") or str((source / row["source"]).resolve())
                if row.get("archive_type"):
                    entry["archive_type"] = row["archive_type"]
                if row.get("companions"):
                    entry["companions"] = [Path(member["target"]).name for member in row["companions"]]
                index["assets"][target] = entry
            index.update(pattern=index.get("pattern", pattern), generation=uuid.uuid4().hex)
            media.save(state / "index.json", index)
        if source_index.is_file():
            for row in rows:
                source_catalog["assets"].pop(row["source"], None)
            source_catalog["generation"] = uuid.uuid4().hex
            media.save(source_index, source_catalog)
        if keep_source_document:
            archive_record(source_documents, source, library, rows)
        else:
            for path in source_documents:
                if os.path.lexists(path):
                    path.unlink()
                    media.sync_dir(source)
        metadata_cache = {}
        for folder in ordered_exports:
            media.render_index(folder, state_dir(folder), argparse.Namespace(plan=None), metadata_cache=metadata_cache)
    except (OSError, ValueError, KeyError, TypeError, KeyboardInterrupt):
        rollback(journal, batch)
        raise
    journal.unlink()
    media.sync_dir(journal.parent)
    return {"archived": len(rows), "companions_archived": len(moves) - len(rows),
            "updated_indexes": [str(folder / media.INDEX_DOCUMENT) for folder in ordered_exports]}


def apply_plan(source, library, journal, plan):
    media.require(make_plan(source, library, plan) == plan, "Plan differs from current validation.")
    return execute(source, library, journal, plan["rows"], plan["pattern"])


INDEX_SYSTEM = {"path", "filename", "standard_name", "kind", "status", "issues",
                "companions", "size_bytes", "mtime_ns", "proposal", "original_path",
                "original_filename", "source_path", "archive_type", "archive_target",
                "archived_path", "archived_filename", "plan_sha256"}


def index_rows(source, library, doc):
    pattern = doc.get("pattern") or media.DEFAULT_PATTERN
    rows, skipped, seen = [], [], set()
    for asset in doc["assets"]:
        category = asset.get("archive_type")
        if not category:
            skipped.append(asset["path"])
            continue
        path = media.source_path(source, asset["path"])
        stamp = media.fingerprint(path)
        media.require(stamp and stamp[2] > 0 and stamp[2] == asset.get("size_bytes")
                      and stamp[3] == asset.get("mtime_ns"),
                      f"File changed since the index was written: {path}")
        annotation = media.annotation_with_folder(
            {k: v for k, v in asset.items() if k not in INDEX_SYSTEM and v is not None},
            path, source / media.STATE)
        media.require_voice(annotation, path)
        relative = media.folder_for({"kind": asset["kind"], "archive_type": category,
                                     "annotation": annotation, "standard_name": asset["standard_name"]})
        standard_name = media.align_name(asset["standard_name"], asset["kind"], category)
        media.check_name(standard_name, path, pattern)
        target_rel = relative / standard_name
        media.require(target_rel not in seen, f"Duplicate destination: {target_rel}")
        seen.add(target_rel)
        folder, parent = destination_folder(library, relative)
        media.require(stamp[0] == parent.stat().st_dev,
                      f"Archive requires the same volume: {path} / {folder}")
        target = folder / standard_name
        occupied = {media.key(peer.name) for peer in folder.iterdir()} if folder.exists() else set()
        dest_index = media.index_at(state_dir(folder))
        occupied.update(media.key(entry["standard_name"]) for current, entry in dest_index["assets"].items()
                        if media.fingerprint(media.source_path(folder, current)) == entry["fingerprint"])
        media.require(media.key(standard_name) not in occupied, f"Destination name occupied: {target}")
        peers = asset.get("companions") or []
        media.require(set(media.archive_sidecars(path)) == {Path(peer).name for peer in peers},
                      f"Companion set changed since the index was written: {path}")
        companions = []
        for peer in peers:
            peer_path = media.source_path(source, peer)
            peer_stamp = media.fingerprint(peer_path)
            media.require(peer_stamp and Path(peer).stem == Path(asset["path"]).stem
                          and Path(peer).suffix.lower() in {".srt", ".lrf"},
                          f"Unsupported companion: {peer_path}")
            companions.append({"source": peer,
                               "target": str(relative / (Path(standard_name).stem + Path(peer).suffix)),
                               "fingerprint": peer_stamp})
        rows.append({"source": asset["path"], "target": str(target_rel), "kind": asset["kind"],
                     "fingerprint": stamp, "standard_name": standard_name, "annotation": annotation,
                     "archive_type": category,
                     "original_path": asset.get("original_path") or asset["path"],
                     "original_filename": asset.get("original_filename") or asset["filename"],
                     "source_path": asset.get("source_path") or str(path.resolve()),
                     "companions": companions,
                     "prior_file_comment": media.file_comment(path),
                     "prior_file_tags": media.file_attribute(path, media.TAGS_ATTR)})
    return rows, skipped


def batch(index_path, library):
    index_path = index_path.expanduser().resolve()
    source = index_path.parent
    doc = media.read(index_path)
    media.require(isinstance(doc, dict) and doc.get("generated_by") == media.GENERATED_BY
                  and doc.get("schema_version") == 1 and isinstance(doc.get("assets"), list),
                  f"Not a media-preflight index: {index_path}")
    journal = state_dir(library) / JOURNAL
    media.require(not os.path.lexists(journal), "Library has an interrupted archive.")
    rows, skipped = index_rows(source, library, doc)
    media.require(rows, "No assets with archive_type to archive.")
    summary = execute(source, library, journal, rows, doc.get("pattern") or media.DEFAULT_PATTERN,
                      keep_source_document=True)
    archive_record([index_path], source, library, rows)
    return {**summary, "skipped_without_category": skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "preview", "apply", "recover"):
        cmd = commands.add_parser(name)
        cmd.add_argument("source", type=Path)
        cmd.add_argument("library", type=Path)
        if name in ("plan", "preview", "apply"):
            cmd.add_argument("input", type=Path)
        if name == "apply":
            cmd.add_argument("--sha256", required=True)
        if name == "recover":
            cmd.add_argument("--confirm", action="store_true")
    batch_parser = commands.add_parser("batch")
    batch_parser.add_argument("index", type=Path)
    batch_parser.add_argument("library", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "batch":
            library = directory(args.library)
            state = state_dir(library)
            state.mkdir(mode=0o700, exist_ok=True)
            lock = os.open(state, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = batch(args.index, library)
            finally:
                os.close(lock)
            sys.stdout.buffer.write(media.encode(result))
            return
        source, library = directory(args.source), directory(args.library)
        state = state_dir(library)
        journal = state / JOURNAL
        if args.command == "plan":
            media.require(not os.path.lexists(journal), "Library has an interrupted archive.")
            result = make_plan(source, library, media.read(args.input))
        elif args.command == "preview":
            media.require(not os.path.lexists(journal), "Library has an interrupted archive.")
            raw = args.input.read_bytes()
            plan = json.loads(raw)
            media.require(make_plan(source, library, plan) == plan, "Plan differs from current validation.")
            proposed = {row["source"]: {**row, "archive_destination": str(library / row["target"]),
                        "companion_destinations": [(item["source"], str(library / item["target"]))
                                                   for item in row.get("companions", [])]}
                        for row in plan["rows"]}
            result = media.render_index(source, state_dir(source), argparse.Namespace(plan=None),
                                           archive_preview=(proposed, digest(raw)))
        else:
            if args.command == "apply":
                raw = args.input.read_bytes()
                media.require(digest(raw) == args.sha256, "Plan SHA-256 does not match reviewed bytes.")
                plan = json.loads(raw)
                state.mkdir(mode=0o700, exist_ok=True)
            lock = os.open(state, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if args.command == "apply":
                    media.require(not os.path.lexists(journal), "Library has an interrupted archive.")
                    result = apply_plan(source, library, journal, plan)
                else:
                    result = media.read(journal)
                    media.require(result["source"] == str(source), "Archive journal belongs to another source.")
                    if args.confirm:
                        rollback(journal, result)
                        result = {"recovered": True}
            finally:
                os.close(lock)
        sys.stdout.buffer.write(media.encode(result))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
