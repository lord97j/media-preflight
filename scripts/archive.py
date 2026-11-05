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


def managed_markdown(path):
    data = read_file(path)
    media.require(data is None or data.startswith((media.GENERATED + "\n").encode()),
                  f"Existing Markdown is not managed by this skill: {path}")
    return data


def folder_for(row):
    category = row["archive_type"]
    media.require(isinstance(category, str) and 0 < len(category.encode()) <= 100
                  and not media.BAD_NAME.search(category) and not category.startswith(".")
                  and category == category.strip() and not category.endswith(".")
                  and not media.DOS_NAME.fullmatch(category.split(".")[0]),
                  f"Invalid archive category: {category}")
    captured = row["annotation"].get("capture_time")
    month = date.fromisoformat(captured[:10]).strftime("%Y-%m") if captured else "日期未知"
    return Path("视频" if row["kind"] == "video" else "图片") / month / category


def destination_folder(library, relative):
    folder = library / relative
    media.require(folder.resolve() == folder and folder.is_relative_to(library),
                  f"Invalid archive folder: {folder}")
    parent = folder
    while not os.path.lexists(parent):
        parent = parent.parent
    media.require(parent.is_dir(), f"Archive parent is not a directory: {parent}")
    return folder, parent


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
        media.require(not media.sidecars(path), f"Paired files cannot be archived individually: {path}")
        media.check_name(row["standard_name"], path, review["pattern"])
        media.check_annotation(row["annotation"])
        relative = folder_for(row)
        folder, parent = destination_folder(library, relative)
        media.require(not folder.is_relative_to(source), f"Archive destination overlaps source: {folder}")
        media.require(stamp[0] == parent.stat().st_dev, f"Archive requires the same volume: {path} / {folder}")
        target = folder / row["standard_name"]
        normalized = media.key(str(target))
        media.require(normalized not in destinations, f"Duplicate destination: {target}")
        destinations.add(normalized)
        if folder not in folders:
            dest_state = state_dir(folder)
            media.require(not os.path.lexists(dest_state / "transaction.json"),
                          f"Destination has an interrupted batch: {folder}")
            dest_index = media.index_at(dest_state)
            occupied = {media.key(peer.name) for peer in folder.iterdir()} if folder.exists() else set()
            occupied.update(media.key(entry["standard_name"]) for current, entry in dest_index["assets"].items()
                            if media.fingerprint(media.source_path(folder, current)) == entry["fingerprint"])
            existing_md = managed_markdown(folder / media.MARKDOWN)
            folders[folder] = (dest_index, occupied, digest(existing_md) if existing_md else None)
        dest_index, occupied, markdown_digest = folders[folder]
        media.check_name(row["standard_name"], path, dest_index.get("pattern", review["pattern"]))
        media.require(media.key(target.name) not in occupied, f"Destination name occupied: {target}")
        previous = index["assets"].get(row["source"], {})
        prepared = {"source": row["source"], "kind": row["kind"], "action": "archive",
                     "fingerprint": stamp, "standard_name": row["standard_name"],
                     "annotation": row["annotation"], "archive_type": row["archive_type"],
                     "target": str(relative / target.name),
                     "original_path": previous.get("original_path", row["source"]),
                     "write_file_description": True, "prior_file_comment": media.file_comment(path),
                     "prior_file_tags": media.file_attribute(path, media.TAGS_ATTR),
                     "destination_generation": dest_index["generation"], "destination_markdown_sha256": markdown_digest}
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


def apply_plan(source, library, journal, plan):
    media.require(make_plan(source, library, plan) == plan, "Plan differs from current validation.")
    source_md = source / media.MARKDOWN
    managed_markdown(source_md)
    source_index = state_dir(source) / "index.json"
    source_catalog = media.index_at(source_index.parent)
    common = Path(os.path.commonpath([source, library]))
    groups, moves = {}, []
    for row in plan["rows"]:
        target = library / row["target"]
        groups.setdefault(target.parent, []).append(row)
        moves.append({**row, "source": str((source / row["source"]).relative_to(common)),
                      "target": str(target.relative_to(common))})
    files = [source_md, source_index]
    for folder in groups:
        files.extend([folder / media.MARKDOWN, state_dir(folder) / "index.json"])
    batch = {"source": str(source), "common_root": str(common), "moves": moves, "backups": {str(p): snapshot(p) for p in files}}
    media.save(journal, batch)
    try:
        for folder in groups:
            folder.mkdir(parents=True, exist_ok=True)
            state_dir(folder).mkdir(mode=0o700, exist_ok=True)
        for row in moves:
            origin = media.source_path(common, row["source"])
            target = media.source_path(common, row["target"])
            media.require(media.fingerprint(origin) == row["fingerprint"] and not media.sidecars(origin),
                          f"Source changed or new paired files: {origin}")
            media.require(media.file_comment(origin) == row["prior_file_comment"], f"Comment changed: {origin}")
            media.require(media.file_attribute(origin, media.TAGS_ATTR) == row["prior_file_tags"],
                          f"Tags changed: {origin}")
            media.move_exclusive(origin, target)
            media.set_comment(target, media.planned_comment(row))
            media.set_attribute(target, media.TAGS_ATTR, media.planned_tags(row))
        for folder, rows in groups.items():
            state = state_dir(folder)
            index = media.index_at(state)
            for row in rows:
                target = Path(row["target"]).name
                index["assets"][target] = {k: row[k] for k in
                                           ("kind", "fingerprint", "standard_name", "annotation", "original_path")}
            index.update(pattern=index.get("pattern", plan["pattern"]), generation=uuid.uuid4().hex)
            media.save(state / "index.json", index)
            media.render_markdown(folder, state, argparse.Namespace(plan=None))
        if source_index.is_file():
            for row in plan["rows"]:
                source_catalog["assets"].pop(row["source"], None)
            source_catalog["generation"] = uuid.uuid4().hex
            media.save(source_index, source_catalog)
        if os.path.lexists(source_md):
            source_md.unlink()
            media.sync_dir(source)
    except (OSError, ValueError, KeyError, TypeError, KeyboardInterrupt):
        rollback(journal, batch)
        raise
    journal.unlink()
    media.sync_dir(journal.parent)
    return {"archived": len(moves), "updated_indexes": [str(folder / media.MARKDOWN) for folder in groups]}


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
    args = parser.parse_args()
    try:
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
            proposed = {row["source"]: {**row, "archive_destination": str(library / row["target"])}
                        for row in plan["rows"]}
            result = media.render_markdown(source, state_dir(source), argparse.Namespace(plan=None),
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
