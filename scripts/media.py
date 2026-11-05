#!/usr/bin/env python3
"""Inventory, review, apply, and search a directory-local media catalog."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes
import ctypes.util
from datetime import date, datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import plistlib
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid

IMAGES = set(".jpg .jpeg .png .heic .heif .webp .avif .gif .tif .tiff .bmp .dng .cr2 .cr3 .arw .nef .raf .orf .rw2".split())
VIDEOS = set(".mp4 .mov .m4v .mkv .avi .webm .mts .m2ts .mpg .mpeg .wmv .mxf .3gp .ts .r3d .braw .insv".split())
STATE = ".media-preflight"
VOICE_STATE = "voice.json"
VOICE_TAGS = ("有人声", "无人声")
_VOICE_CACHE = {}
INDEX_DOCUMENT = "素材索引.json"
LEGACY_MARKDOWN = "素材索引.md"
GENERATED_BY = "media-preflight"
LEGACY_HEADER = b"<!-- media-preflight -->\n"
DESCRIPTION_ATTR = "com.apple.metadata:kMDItemFinderComment"
TAGS_ATTR = "com.apple.metadata:_kMDItemUserTags"
DEFAULT_PATTERN = r"(?:[0-9]{8}|日期未知)_(?:视频|图片)_[^_]+_[^_]+_[0-9]{3,}"
BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
DOS_NAME = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", re.I)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def read(path, default=None):
    if not path.exists() and not path.is_symlink() and default is not None:
        return default
    require(path.is_file() and not path.is_symlink(), f"Invalid JSON path: {path}")
    return json.loads(path.read_bytes())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    save_bytes(path, encode(value))


def save_bytes(path, data):
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def key(text):
    return unicodedata.normalize("NFC", text).casefold()


def fingerprint(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def source_path(root, relative):
    path = root / relative
    require(not Path(relative).is_absolute() and path.is_relative_to(root)
            and path.parent.resolve() == path.parent and ".." not in Path(relative).parts
            and STATE not in Path(relative).parts, f"Unsafe path: {relative}")
    return path


def kind(path):
    suffix = path.suffix.lower()
    if suffix in IMAGES:
        return "image"
    if suffix in VIDEOS:
        return "video"
    return None


def sidecars(path):
    # Match both IMG_001.xmp and IMG_001.mov.xmp, including paired media.
    aliases = (key(path.stem), key(path.name))
    return sorted(peer.name for peer in path.parent.iterdir() if peer != path
                  and any(key(peer.name) == alias or key(peer.name).startswith(alias + ".")
                          for alias in aliases))


def archive_sidecars(path):
    """Only the companions the archive step knows how to move alongside the media."""
    return sorted(peer.name for peer in path.parent.iterdir() if peer != path
                  and key(peer.stem) == key(path.stem)
                  and peer.suffix.lower() in {".srt", ".lrf"})


def index_at(state):
    return read(state / "index.json", {"version": 1, "generation": None, "assets": {}})


def voice_records(state):
    cache_path = state / VOICE_STATE
    try:
        info = cache_path.stat()
    except OSError:
        return {}
    stamp = (info.st_mtime_ns, info.st_size)
    cached = _VOICE_CACHE.get(state)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        records = read(cache_path, {}).get("records", {})
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    _VOICE_CACHE[state] = (stamp, records)
    return records


def voice_label(state, path):
    """Return the cached 有人声/无人声 label for this exact file, if detection ran."""
    if state is None:
        return None
    stamp = fingerprint(path)
    if not stamp:
        return None
    record = voice_records(state).get(f"{stamp[0]}:{stamp[1]}")
    if not isinstance(record, dict) or record.get("fingerprint") != stamp:
        return None
    return record.get("label")


def scan(root, state, args):
    index = index_at(state)
    pattern = args.pattern or index.get("pattern", DEFAULT_PATTERN)
    re.compile(pattern)
    rows, skipped = [], []
    voice_missing = 0
    catalogs = {root: index}

    def onerror(error):
        skipped.append({"path": str(error.filename), "reason": str(error)})

    for directory, folders, files in os.walk(root, onerror=onerror):
        base = Path(directory)
        for folder in sorted(folders):
            path = base / folder
            if folder == STATE or path.is_symlink():
                folders.remove(folder)
                if folder != STATE:
                    skipped.append({"path": str(path.relative_to(root)), "reason": "linked directory"})
        folders.sort()
        for name in sorted(files):
            path = base / name
            media_type = kind(path)
            if not media_type:
                continue
            relative = str(path.relative_to(root))
            stamp = fingerprint(path)
            if stamp is None:
                skipped.append({"path": relative, "reason": "symlink or non-regular file"})
                continue
            old = index["assets"].get(relative, {})
            current = old if old.get("fingerprint") == stamp else {}
            if not current:
                _, _, found = export_catalog_entry(root, path, stamp, catalogs)
                current = found or {}
            standard_name = current.get("standard_name", name)
            reasons = []
            if not standard_stem(standard_name, media_type, pattern):
                reasons.append("nonstandard_name")
            if not current.get("annotation"):
                reasons.append("stale_annotation" if old else "missing_annotation")
            if stamp[2] == 0:
                reasons.append("empty_file")
            label = voice_label(state, path) if media_type == "video" else None
            if media_type == "video" and not label:
                voice_keys = {key(tag) for tag in VOICE_TAGS}
                noted = current.get("annotation") or {}
                label = next((tag for tag in noted.get("tags") or []
                              if key(tag.strip()) in voice_keys), None)
            if media_type == "video" and not label:
                voice_missing += 1
            rows.append({"source": relative, "kind": media_type, "fingerprint": stamp,
                         "issues": reasons, "standard_name": standard_name,
                         "annotation": current.get("annotation"), "action": "skip",
                         "companions": current.get("companions") or archive_sidecars(path),
                         "voice": label})
    return {"root": str(root), "generation": index["generation"], "pattern": pattern,
            "rows": rows, "skipped": skipped, "voice_unchecked": voice_missing}


def standard_stem(name, media_type, pattern):
    stem = Path(name).stem
    if not re.fullmatch(pattern, stem):
        return False
    if pattern != DEFAULT_PATTERN:
        return True
    captured_date, label, _ = stem.split("_", 2)
    if label != ("视频" if media_type == "video" else "图片"):
        return False
    if captured_date != "日期未知":
        try:
            date(int(captured_date[:4]), int(captured_date[4:6]), int(captured_date[6:]))
        except ValueError:
            return False
    return True


def check_name(name, source, pattern):
    stem = Path(name).stem
    require(len(name.encode()) <= 240 and not BAD_NAME.search(name) and not name.startswith(".")
            and not stem.endswith((" ", ".")) and not name.endswith((" ", "."))
            and not DOS_NAME.fullmatch(stem.split(".")[0])
            and Path(name).suffix == source.suffix and standard_stem(name, kind(source), pattern),
            f"Invalid standard name: {name}")


def capture_day(row):
    annotation = row.get("annotation") or {}
    captured = annotation.get("capture_time") or row.get("capture_time")
    if isinstance(captured, str) and len(captured) >= 10:
        return date.fromisoformat(captured[:10])
    stem = Path(row.get("standard_name", "")).stem
    if len(stem) >= 8 and stem[:8].isdigit():
        try:
            return date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))
        except ValueError:
            return None
    return None


def folder_for(row):
    category = row["archive_type"]
    require(isinstance(category, str) and 0 < len(category.encode()) <= 100
            and not BAD_NAME.search(category) and not category.startswith(".")
            and category == category.strip() and not category.endswith(".")
            and not DOS_NAME.fullmatch(category.split(".")[0]),
            f"Invalid archive category: {category}")
    media_folder = Path("视频" if row["kind"] == "video" else "图片")
    day = capture_day(row)
    if day is None:
        return media_folder / "日期未知" / category
    return media_folder / day.strftime("%Y-%m") / day.strftime("%d") / category


def align_name(name, media_type, category):
    """Keep the name's scene segment equal to the archive category."""
    if not standard_stem(name, media_type, DEFAULT_PATTERN):
        return name
    captured, label, scene, action, number = Path(name).stem.split("_")
    require("_" not in category, f"Archive category cannot contain '_': {category}")
    action = action if scene == category else scene + action
    return f"{captured}_{label}_{category}_{action}_{number}{Path(name).suffix}"


def check_annotation(note):
    require(isinstance(note, dict) and all(isinstance(note.get(field), str)
            and note[field].strip() for field in ("description", "evidence")),
            "Annotation requires description and evidence")
    if "full_description" in note:
        require(isinstance(note["full_description"], str) and note["full_description"].strip(),
                "Full description must be nonempty text")
    tags = note.get("tags")
    require(isinstance(tags, list) and tags and all(isinstance(tag, str) and tag.strip()
            for tag in tags), "Annotation requires a nonempty tags list")


def annotation_with_voice(note, path, state=None):
    label = voice_label(state, path)
    if not label:
        return note
    keys = {key(tag) for tag in VOICE_TAGS}
    tags = [tag for tag in note.get("tags") or [] if key(tag.strip()) not in keys]
    return {**note, "tags": [*tags, label]}


def require_voice(note, path):
    if kind(path) != "video":
        return
    keys = {key(tag) for tag in VOICE_TAGS}
    require(any(key(tag.strip()) in keys for tag in note.get("tags") or []),
            f"Video requires a 有人声/无人声 tag. Run voice first: {path}")


def annotation_with_folder(note, path, state=None):
    check_annotation(note)
    folder = re.sub(r"[\x00-\x1f\x7f]", " ", path.parent.name).strip()
    tags = note["tags"]
    if folder and not any(key(tag.strip()) == key(folder) for tag in tags):
        tags = [*tags, folder]
    return annotation_with_voice({**note, "tags": tags}, path, state)


def xattr(*args, path):
    require(sys.platform == "darwin", "File comments require macOS extended attributes.")
    result = subprocess.run(["/usr/bin/xattr", *args, str(path)], capture_output=True, text=True)
    require(result.returncode == 0, f"Cannot access file comment: {path}: {result.stderr.strip()}")
    return result.stdout


ENOATTR = 93
LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True) if sys.platform == "darwin" else None
if LIBC is not None:
    LIBC.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                              ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    LIBC.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                              ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    LIBC.removexattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]


def get_attribute(path, attribute):
    """Read an extended attribute as hex; None when absent. libc keeps this out of subprocess overhead."""
    if LIBC is not None:
        name, encoded = attribute.encode(), os.fsencode(path)
        size = LIBC.getxattr(encoded, name, None, 0, 0, 0)
        if size < 0:
            error = ctypes.get_errno()
            if error == ENOATTR:
                return None
            raise OSError(error, os.strerror(error), str(path))
        buffer = ctypes.create_string_buffer(size)
        read = LIBC.getxattr(encoded, name, buffer, size, 0, 0)
        if read < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
        return buffer.raw[:read].hex()
    if attribute not in xattr(path=path).splitlines():
        return None
    return bytes.fromhex(xattr("-px", attribute, path=path)).hex()


def write_attribute(path, attribute, value):
    data = bytes.fromhex(value)
    if LIBC is not None:
        if LIBC.setxattr(os.fsencode(path), attribute.encode(), data, len(data), 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
        return
    xattr("-wx", attribute, value, path=path)


def remove_attribute(path, attribute):
    if LIBC is not None:
        if LIBC.removexattr(os.fsencode(path), attribute.encode(), 0) != 0:
            error = ctypes.get_errno()
            if error != ENOATTR:
                raise OSError(error, os.strerror(error), str(path))
        return
    if get_attribute(path, attribute) is not None:
        xattr("-d", attribute, path=path)


def file_attribute(path, attribute):
    return get_attribute(path, attribute)


def set_attribute(path, attribute, value):
    if value is None:
        remove_attribute(path, attribute)
    else:
        write_attribute(path, attribute, value)
    require(file_attribute(path, attribute) == value, f"File metadata verification failed: {path}")


def file_comment(path):
    return file_attribute(path, DESCRIPTION_ATTR)


def set_comment(path, value):
    set_attribute(path, DESCRIPTION_ATTR, value)


def planned_tags(row):
    old = row["prior_file_tags"]
    tags = plistlib.loads(bytes.fromhex(old)) if old else []
    require(isinstance(tags, list) and all(isinstance(tag, str) for tag in tags),
            f"Unsupported Finder tags: {row['source']}")
    voice_keys = {key(tag) for tag in VOICE_TAGS}
    if any(key(tag.strip()) in voice_keys for tag in row["annotation"]["tags"]):
        # The annotation overrides any earlier voice verdict, so drop both labels first.
        tags = [tag for tag in tags if key(tag.split("\n", 1)[0].strip()) not in voice_keys]
    seen = {key(tag.split("\n", 1)[0]) for tag in tags}
    for tag in row["annotation"]["tags"]:
        label = tag.strip()
        require(not any(ord(ch) < 32 for ch in label), f"Invalid Finder tag: {tag!r}")
        if key(label) not in seen:
            tags.append(label + "\n0")
            seen.add(key(label))
    return plistlib.dumps(tags, fmt=plistlib.FMT_BINARY).hex()


def build(root, state, review):
    index = index_at(state)
    require(review["root"] == str(root) and review["generation"] == index["generation"],
            "Directory or catalog changed. Scan and review again.")
    pattern = review["pattern"]
    re.compile(pattern)
    sources, targets, rows = set(), set(), []
    occupied_by_parent = {}
    for row in review["rows"]:
        action = row["action"]
        if action == "skip":
            continue
        require(action in {"alias", "rename"}, f"Invalid action: {action}")
        path = source_path(root, row["source"])
        stamp = fingerprint(path)
        require(kind(path) and stamp and stamp[2] > 0 and stamp == row["fingerprint"],
                f"Missing, empty or changed source: {path}")
        require(path not in sources, f"Repeated source: {path}")
        sources.add(path)
        name = row["standard_name"]
        check_name(name, path, pattern)
        annotation = annotation_with_folder(row["annotation"], path, state)
        require_voice(annotation, path)
        target = path.with_name(name) if action == "rename" else path
        if target != path:
            require(not sidecars(path), f"Paired files require alias mode: {path}")
        if name != path.name:
            if path.parent not in occupied_by_parent:
                occupied_by_parent[path.parent] = {key(peer.name) for peer in path.parent.iterdir()}
            require(key(name) not in occupied_by_parent[path.parent], f"Name occupied: {path.with_name(name)}")
        standard_key = (path.parent, key(name))
        require(standard_key not in targets, f"Duplicate standard name: {name}")
        targets.add(standard_key)
        previous = index["assets"].get(row["source"], {})
        original = previous.get("original_path", row["source"]) if previous.get("fingerprint") == stamp else row["source"]
        prepared = {"source": row["source"], "target": str(target.relative_to(root)),
                    "action": action, "fingerprint": stamp, "kind": kind(path),
                    "standard_name": name, "annotation": annotation, "original_path": original}
        if row.get("archive_type"):
            require(isinstance(row["archive_type"], str) and row["archive_type"].strip(),
                    f"Invalid archive type: {path}")
            prepared["archive_type"] = row["archive_type"].strip()
        if row.get("write_file_description"):
            require(action == "rename", f"File comments require direct edit mode: {path}")
            prepared.update(write_file_description=True, prior_file_comment=file_comment(path))
        rows.append(prepared)
    for relative, entry in index["assets"].items():
        path = source_path(root, relative)
        if path not in sources and fingerprint(path) == entry["fingerprint"]:
            require((path.parent, key(entry["standard_name"])) not in targets,
                    f"Standard name already indexed: {entry['standard_name']}")
    require(rows, "No selected rows")
    return {"root": str(root), "generation": index["generation"], "pattern": pattern, "rows": rows}


def move_exclusive(source, target, sync=True):
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        result = library.renamex_np(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        result = library.renameat2(-100, os.fsencode(source), os.fsencode(target), 1)
    else:
        raise ValueError("Exclusive rename unavailable on this platform. Use alias mode.")
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))
    if sync:
        sync_dir(source.parent)
        if target.parent != source.parent:
            sync_dir(target.parent)


def planned_comment(row):
    annotation = row["annotation"]
    note = annotation["description"]
    if annotation.get("full_description"):
        note += "\n\n完整画面描述\n" + annotation["full_description"]
    return plistlib.dumps(note, fmt=plistlib.FMT_BINARY).hex()


def restore(root, rows):
    operations = []
    for row in reversed(rows):
        source = source_path(root, row["source"])
        target = source_path(root, row["target"])
        stamp = row["fingerprint"]
        current = source
        if source != target and fingerprint(source) != stamp:
            require(fingerprint(target) == stamp and not os.path.lexists(source),
                    f"Rollback blocked by a missing, changed or occupied path: {source} / {target}")
            current = target
        if row.get("write_file_description"):
            require(fingerprint(current) == stamp, f"Changed file: {current}")
            existing = file_comment(current)
            previous = row["prior_file_comment"]
            require(existing in (previous, planned_comment(row)), f"Changed file comment: {current}")
            if existing != previous:
                operations.append(("comment", current, previous))
        if "prior_file_tags" in row:
            existing = file_attribute(current, TAGS_ATTR)
            previous = row["prior_file_tags"]
            require(existing in (previous, planned_tags(row)), f"Changed file tags: {current}")
            if existing != previous:
                operations.append(("tags", current, previous))
        if current != source:
            operations.append(("rename", current, source))
    for operation, path, value in operations:
        if operation == "comment":
            set_comment(path, value)
        elif operation == "tags":
            set_attribute(path, TAGS_ATTR, value)
        else:
            move_exclusive(path, value)


def apply(root, state, plan):
    require(build(root, state, plan) == plan, "Plan differs from current validation.")
    index = index_at(state)
    assets = dict(index["assets"])
    for row in plan["rows"]:
        previous = assets.pop(row["source"], None)
        entry = {field: row[field] for field in
                 ("kind", "fingerprint", "standard_name", "annotation", "original_path")}
        if (previous and row["source"] == row["target"] and
                previous.get("fingerprint") == row["fingerprint"] and previous.get("companions")):
            entry["companions"] = previous["companions"]
        assets[row["target"]] = entry
    updated = {"version": 1, "generation": uuid.uuid4().hex, "pattern": plan["pattern"], "assets": assets}
    journal = state / "transaction.json"
    batch = {"before": index, "after_generation": updated["generation"], "rows": plan["rows"]}
    save(journal, batch)
    try:
        for row in plan["rows"]:
            source = source_path(root, row["source"])
            target = source_path(root, row["target"])
            require(fingerprint(source) == row["fingerprint"], f"Source changed: {source}")
            if source != target:
                require(not sidecars(source), f"New paired files: {source}")
                move_exclusive(source, target)
            if row.get("write_file_description"):
                require(file_comment(target) == row["prior_file_comment"], f"File comment changed: {target}")
                set_comment(target, planned_comment(row))
        save(state / "index.json", updated)
    except (OSError, ValueError, KeyboardInterrupt):
        if index_at(state)["generation"] != updated["generation"]:
            restore(root, plan["rows"])
            journal.unlink()
        raise
    os.replace(journal, state / "last-batch.json")
    sync_dir(state)
    return {"applied": len(plan["rows"]), "renamed": sum(r["source"] != r["target"] for r in plan["rows"]),
            "file_descriptions": sum(bool(r.get("write_file_description")) for r in plan["rows"]),
            "index": str(state / "index.json")}


def rollback(root, state, args):
    journal = state / "transaction.json"
    last = state / "last-batch.json"
    path = journal if journal.exists() else last
    batch = read(path)
    generation = index_at(state)["generation"]
    require(generation in (batch["before"]["generation"], batch["after_generation"]),
            "Catalog changed since this batch.")
    if not args.confirm:
        return batch
    if path == last:
        os.replace(last, journal)
        sync_dir(state)
        path = journal
    restore(root, batch["rows"])
    save(state / "index.json", batch["before"])
    path.unlink()
    sync_dir(state)
    return {"rolled_back": len(batch["rows"])}


def search(root, state, args):
    terms = [key(term) for value in args.terms for term in value.split()]
    results = []
    document = document_at(root)
    catalog = index_at(state)
    if document and document.get("catalog_generation") == catalog["generation"]:
        fields = {"path", "filename", "standard_name", "original_path", "original_filename",
                  "source_path", "archive_type", "archive_target", "kind",
                  "status", "issues", "companions", "proposal", "size_bytes", "mtime_ns"}
        entries = ((row["path"], {
            "standard_name": row["standard_name"], "kind": row["kind"],
            "original_path": row.get("original_path", row["path"]),
            "annotation": {k: v for k, v in row.items() if k not in fields},
            "companions": row["companions"],
            "file_stamp": (row.get("size_bytes"), row.get("mtime_ns")),
        }) for row in document["assets"])
    else:
        entries = sorted(catalog["assets"].items())
    for relative, entry in entries:
        path = source_path(root, relative)
        stamp = fingerprint(path)
        if not stamp or stamp != entry.get("fingerprint", stamp) or (args.kind and entry["kind"] != args.kind):
            continue
        if "file_stamp" in entry and entry["file_stamp"] != tuple(stamp[2:]):
            continue
        haystack = key(" ".join((relative, entry["standard_name"], entry["original_path"],
                               json.dumps(entry["annotation"], ensure_ascii=False))))
        if all(term in haystack for term in terms):
            results.append({"path": str(path), "fingerprint": stamp,
                            **{k: v for k, v in entry.items() if k != "file_stamp"}})
    return results


def document_at(root):
    path = root / INDEX_DOCUMENT
    if not os.path.lexists(path):
        return None
    data = read(path)
    require(isinstance(data, dict) and data.get("generated_by") == GENERATED_BY
            and data.get("schema_version") == 1 and isinstance(data.get("assets"), list),
            f"Existing JSON index is not managed by this skill: {path}")
    return data


def legacy_document(root):
    path = root / LEGACY_MARKDOWN
    if path.is_file() and not path.is_symlink():
        with path.open("rb") as file:
            if file.read(len(LEGACY_HEADER)) == LEGACY_HEADER:
                return path
    return None


def finder_metadata(path):
    if sys.platform != "darwin":
        return {}
    metadata = {}
    for attribute in (DESCRIPTION_ATTR, TAGS_ATTR):
        try:
            value = file_attribute(path, attribute)
            decoded = plistlib.loads(bytes.fromhex(value)) if value else None
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        if attribute == DESCRIPTION_ATTR and isinstance(decoded, str) and decoded.strip():
            summary, marker, full = decoded.partition("\n\n完整画面描述\n")
            metadata.update(description=summary.strip(), finder_comment=decoded)
            if marker and full.strip():
                metadata["full_description"] = full
        elif attribute == TAGS_ATTR and isinstance(decoded, list) and all(isinstance(tag, str) for tag in decoded):
            tags = {}
            for value in decoded:
                label = value.split("\n", 1)[0].strip()
                if label:
                    tags.setdefault(key(label), label)
            metadata["tags"] = list(tags.values())
    return metadata


def export_catalog_entry(root, path, stamp, catalogs):
    parent = path.parent
    while parent.is_relative_to(root):
        if parent not in catalogs:
            state = parent / STATE
            catalogs[parent] = index_at(state) if not state.is_symlink() else {}
        catalog = catalogs[parent]
        entry = catalog.get("assets", {}).get(str(path.relative_to(parent)))
        if entry and entry["fingerprint"] == stamp:
            return parent, catalog, entry
        if parent == root:
            break
        parent = parent.parent
    return root, {}, {}


def render_index(root, state, args, archive_preview=None, metadata_cache=None):
    document_at(root)
    proposed, plan_digest = {}, None
    if archive_preview:
        proposed, plan_digest = archive_preview
    elif args.plan:
        raw = args.plan.read_bytes()
        plan = json.loads(raw)
        require(build(root, state, plan) == plan, "Plan differs from current validation.")
        proposed = {r["source"]: r for r in plan["rows"]}
        plan_digest = hashlib.sha256(raw).hexdigest()
    report = scan(root, state, argparse.Namespace(pattern=None))
    assets, children, catalogs = [], set(), {root: index_at(state)}
    if metadata_cache is None:
        metadata_cache = {}
    for scanned in report["rows"]:
        relative = scanned["source"]
        path = root / relative
        catalog_root, catalog, entry = export_catalog_entry(root, path, scanned["fingerprint"], catalogs)
        annotation = dict(entry.get("annotation") or scanned.get("annotation") or {})
        if path not in metadata_cache:
            metadata_cache[path] = finder_metadata(path)
        annotation.update(metadata_cache[path])
        annotation = annotation_with_voice(annotation, path, state)
        proposal = proposed.get(relative)
        issues = list(scanned["issues"])
        standard_name = entry.get("standard_name", scanned["standard_name"])
        if entry:
            pattern = catalog.get("pattern", report["pattern"])
            issues = [issue for issue in issues if issue not in {"nonstandard_name", "missing_annotation", "stale_annotation"}]
            if not standard_stem(standard_name, scanned["kind"], pattern):
                issues.append("nonstandard_name")
        if annotation.get("description") and annotation.get("tags"):
            issues = [issue for issue in issues if issue != "missing_annotation"]
        companions = [str(Path(relative).parent / name) for name in entry.get("companions", scanned["companions"])]
        status = "needs_review" if issues else ("indexed" if entry else "metadata_only")
        if proposal:
            annotation.update(proposal["annotation"])
            standard_name = proposal["standard_name"]
            status = "proposed"
        asset = {**annotation, "path": relative, "filename": path.name, "standard_name": standard_name,
                 "kind": scanned["kind"], "status": status, "issues": issues,
                 "tags": annotation.get("tags") or [], "description": annotation.get("description"),
                 "companions": companions, "size_bytes": scanned["fingerprint"][2],
                 "mtime_ns": scanned["fingerprint"][3]}
        if entry.get("original_path"):
            asset["original_path"] = str(catalog_root.relative_to(root) / entry["original_path"])
        asset["original_filename"] = entry.get("original_filename") or (
            Path(entry["original_path"]).name if entry.get("original_path") else path.name)
        if entry.get("source_path"):
            asset["source_path"] = entry["source_path"]
        elif not entry:
            asset["source_path"] = str(path.resolve())
        if entry.get("archive_type"):
            asset["archive_type"] = entry["archive_type"]
        if proposal:
            asset["proposal"] = {"action": proposal["action"],
                                 "target": proposal.get("archive_destination", proposal["target"])}
            if proposal.get("companion_destinations"):
                asset["proposal"]["companions"] = [{"path": origin, "target": destination}
                                                  for origin, destination in proposal["companion_destinations"]]
            if proposal.get("archive_type"):
                asset["archive_type"] = proposal["archive_type"]
        if asset.get("archive_type"):
            try:
                asset["archive_target"] = str(folder_for(asset) / align_name(
                    standard_name, scanned["kind"], asset["archive_type"]))
            except (ValueError, KeyError, TypeError):
                pass
        if len(Path(relative).parts) > 1:
            child = Path(relative).parts[0]
            child_index = root / child / INDEX_DOCUMENT
            if child_index.is_file() and not child_index.is_symlink():
                children.add(str(child_index.relative_to(root)))
        assets.append(asset)
    assets.sort(key=lambda row: (row.get("capture_time") or "", row["path"]))
    videos = sum(row["kind"] == "video" for row in assets)
    voice_keys = {key(tag) for tag in VOICE_TAGS}
    voice_unchecked = sum(row["kind"] == "video" and not any(
        key(tag.strip()) in voice_keys for tag in row.get("tags") or []) for row in assets)
    output = {"schema_version": 1, "generated_by": GENERATED_BY,
              "mode": "preview" if plan_digest else "current",
              "pattern": report["pattern"],
              "catalog_generation": catalogs[root]["generation"],
              "counts": {"assets": len(assets), "videos": videos, "images": len(assets) - videos,
                         "skipped": len(report["skipped"]),
                         "voice_unchecked": voice_unchecked},
              "child_indexes": sorted(children), "assets": assets, "skipped": report["skipped"]}
    if plan_digest:
        output["plan_sha256"] = plan_digest
    target = root / INDEX_DOCUMENT
    save(target, output)
    legacy = legacy_document(root)
    if legacy:
        legacy.unlink()
        sync_dir(root)
    return {"index": str(target), "assets": len(assets), "skipped": len(report["skipped"]),
            "proposed": len(proposed)}


def render_tree(root, state, args):
    require(args.plan is None, "--tree cannot be combined with --plan.")
    report = scan(root, state, argparse.Namespace(pattern=None))
    folders = {root}
    for row in report["rows"]:
        parent = (root / row["source"]).parent
        while parent not in folders:
            folders.add(parent)
            parent = parent.parent
    ordered = sorted(folders, key=lambda p: (-len(p.parts), str(p)))
    for folder in ordered:
        document_at(folder)
        state = folder / STATE
        require(not state.is_symlink(), f"Invalid catalog directory: {state}")
        require(not any(os.path.lexists(state / name) for name in ("transaction.json", "archive-transaction.json")),
                f"Interrupted batch in {folder}. Recover before rendering.")
    metadata_cache = {}
    results = [render_index(folder, folder / STATE, args, metadata_cache=metadata_cache)
               for folder in ordered]
    return {**results[-1], "updated_indexes": [result["index"] for result in results]}


def videos_at(root):
    for directory, folders, files in os.walk(root):
        base = Path(directory)
        folders[:] = sorted(folder for folder in folders
                            if folder != STATE and not (base / folder).is_symlink())
        for name in sorted(files):
            path = base / name
            if kind(path) != "video":
                continue
            stamp = fingerprint(path)
            if stamp and stamp[2] > 0:
                yield path, stamp


def audio_track(path):
    """True/False for an audio stream, None when the file cannot be probed."""
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                                 "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                                capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def speak_seconds(path, threshold):
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    audio = decode_audio(str(path), sampling_rate=16000)
    chunks = get_speech_timestamps(audio, VadOptions(
        threshold=threshold, min_speech_duration_ms=250,
        min_silence_duration_ms=700, speech_pad_ms=500))
    return round(sum(chunk["end"] - chunk["start"] for chunk in chunks) / 16000, 3), len(chunks)


def import_batch(path, root, records):
    """Reuse statuses from a video_speech_batch doubao_vad_batch.json."""
    statuses = {"prepared": "has_voice", "no_speech_detected": "no_voice",
                "no_audio": "no_audio", "unreadable": "unreadable"}
    labels = {"has_voice": "有人声", "no_voice": "无人声", "no_audio": "无人声"}
    imported = 0
    for manifest in read(path).get("manifests") or []:
        source, status = manifest.get("path"), statuses.get(manifest.get("status"))
        if not source or not status:
            continue
        media_path = Path(source)
        stamp, old = fingerprint(media_path), manifest.get("fingerprint") or {}
        if not stamp or old.get("size") != stamp[2] or old.get("mtime_ns") != stamp[3]:
            continue
        try:
            relative = str(media_path.resolve().relative_to(root))
        except ValueError:
            relative = str(media_path)
        records[f"{stamp[0]}:{stamp[1]}"] = {
            "fingerprint": stamp, "path": relative, "settings": None,
            "source": "doubao_vad_batch", "status": status, "label": labels.get(status),
            "speech_seconds": round(manifest.get("selected_duration") or 0, 3),
            "ranges": len(manifest.get("detected_ranges") or [])}
        imported += 1
    return imported


def voice_batch(root, state, args):
    if importlib.util.find_spec("faster_whisper") is None:
        raise ValueError("voice needs faster-whisper; pass --python with an interpreter that has it")
    settings = {"version": 1, "threshold": args.threshold, "min_speech_ms": 250,
                "min_silence_ms": 700, "speech_pad_ms": 500, "sample_rate": 16000}
    cache = read(state / VOICE_STATE, {"version": 1, "records": {}})
    records = cache.setdefault("records", {})
    imported = import_batch(Path(args.from_batch).expanduser(), root, records) if args.from_batch else 0
    targets = list(videos_at(root))

    def detect(item):
        path, stamp = item
        key_text = f"{stamp[0]}:{stamp[1]}"
        old = records.get(key_text)
        if not args.force and isinstance(old, dict) and old.get("fingerprint") == stamp \
                and old.get("status") != "error" \
                and (old.get("settings") == settings or old.get("source") == "doubao_vad_batch"):
            return path, old, True
        record = {"fingerprint": stamp, "path": str(path.relative_to(root)),
                  "settings": settings,
                  "detected_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        present = audio_track(path)
        if present is False:
            record.update(status="no_audio", label="无人声", speech_seconds=0.0, ranges=0)
        elif present is None:
            record.update(status="unreadable", label=None)
        else:
            try:
                seconds, ranges = speak_seconds(path, args.threshold)
                record.update(status="has_voice" if ranges else "no_voice",
                              label="有人声" if ranges else "无人声",
                              speech_seconds=seconds, ranges=ranges)
            except Exception as error:
                record.update(status="error", label=None, error=str(error)[:300])
        return path, record, False

    results, reused = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(detect, item) for item in targets]
        for number, future in enumerate(as_completed(futures), 1):
            path, record, cached = future.result()
            records[f"{record['fingerprint'][0]}:{record['fingerprint'][1]}"] = record
            reused += cached
            results.append(record)
            print(f"voice {number}/{len(targets)} {path.name}: {record['status']}",
                  file=sys.stderr, flush=True)
    save(state / VOICE_STATE, {"version": 1,
                               "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                               "settings": settings, "records": records})
    results.sort(key=lambda record: record["path"])
    counts = {}
    for record in results:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    return {"root": str(root), "settings": settings, "imported": imported, "cached": reused,
            "checked": len(results) - reused, "status_counts": counts,
            "records": [{"path": record["path"], "status": record["status"], "label": record["label"],
                         "speech_seconds": record.get("speech_seconds")} for record in results],
            "cache": str(state / VOICE_STATE)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "plan", "apply", "rollback", "search", "render", "voice"):
        commands.add_parser(name).add_argument("directory", type=Path)
    commands.choices["scan"].add_argument("--pattern", help="Full-match regex for the filename stem")
    commands.choices["plan"].add_argument("review", type=Path)
    commands.choices["apply"].add_argument("plan", type=Path)
    commands.choices["apply"].add_argument("--sha256", required=True)
    commands.choices["rollback"].add_argument("--confirm", action="store_true")
    commands.choices["render"].add_argument("--plan", type=Path)
    commands.choices["render"].add_argument("--tree", action="store_true",
                                          help="Refresh indexes from the leaves to the selected root")
    commands.choices["search"].add_argument("terms", nargs="*")
    commands.choices["search"].add_argument("--kind", choices=("image", "video"))
    commands.choices["voice"].add_argument("--python",
                                           help="Interpreter with faster-whisper; re-executes this script there")
    commands.choices["voice"].add_argument("--from-batch", type=Path,
                                           help="Reuse doubao_vad_batch.json from video_speech_batch.py")
    commands.choices["voice"].add_argument("--threshold", type=float, default=0.5)
    commands.choices["voice"].add_argument("--workers", type=int, default=2)
    commands.choices["voice"].add_argument("--force", action="store_true",
                                           help="Detect again even when the cached fingerprint matches")
    args = parser.parse_args()
    try:
        if args.command == "voice":
            require(0 < args.threshold < 1 and args.workers > 0, "Invalid voice threshold or workers")
            if args.python and importlib.util.find_spec("faster_whisper") is None:
                interpreter = str(Path(args.python).expanduser())
                require(os.environ.get("MEDIA_PREFLIGHT_REEXEC") != "1",
                        f"faster-whisper is not importable with {interpreter}")
                os.environ["MEDIA_PREFLIGHT_REEXEC"] = "1"
                os.execv(interpreter, [interpreter, str(Path(__file__).resolve()), *sys.argv[1:]])
        root = args.directory.resolve(strict=True)
        require(root.is_dir(), f"Not a directory: {root}")
        state = root / STATE
        require(not state.is_symlink() and (not state.exists() or state.is_dir()), f"Invalid state: {state}")
        if args.command == "apply":
            raw = args.plan.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == args.sha256, "Plan SHA-256 does not match reviewed bytes.")
            args.plan = json.loads(raw)
        mutation = args.command in {"apply", "voice"} or (args.command == "rollback" and args.confirm)
        if mutation:
            state.mkdir(mode=0o700, exist_ok=True)
        if state.exists():
            # Lock the directory itself, including read-only operations, without creating a lock file.
            lock = os.open(state, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX if mutation or args.command == "render" else fcntl.LOCK_SH)
                result = run(root, state, args)
            finally:
                os.close(lock)
        else:
            result = run(root, state, args)
        sys.stdout.buffer.write(encode(result))
    except (OSError, ValueError, KeyError, TypeError, re.error) as error:
        parser.exit(2, f"{error}\n")


def run(root, state, args):
    if args.command != "rollback":
        require(not os.path.lexists(state / "transaction.json"),
                "Interrupted batch. Inspect rollback before proceeding.")
    if args.command == "scan":
        return scan(root, state, args)
    if args.command == "voice":
        return voice_batch(root, state, args)
    if args.command == "plan":
        return build(root, state, read(args.review))
    if args.command == "apply":
        return apply(root, state, args.plan)
    if args.command == "rollback":
        return rollback(root, state, args)
    if args.command == "render":
        return render_tree(root, state, args) if args.tree else render_index(root, state, args)
    return search(root, state, args)


if __name__ == "__main__":
    main()
