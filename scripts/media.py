#!/usr/bin/env python3
"""Inventory, review, apply, and search a directory-local media catalog."""
import argparse
import ctypes
from datetime import date
import fcntl
import hashlib
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
from urllib.parse import quote

IMAGES = set(".jpg .jpeg .png .heic .heif .webp .avif .gif .tif .tiff .bmp .dng .cr2 .cr3 .arw .nef .raf .orf .rw2".split())
VIDEOS = set(".mp4 .mov .m4v .mkv .avi .webm .mts .m2ts .mpg .mpeg .wmv .mxf .3gp .ts .r3d .braw .insv".split())
STATE = ".media-preflight"
MARKDOWN = "素材索引.md"
GENERATED = "<!-- media-preflight -->"
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


def index_at(state):
    return read(state / "index.json", {"version": 1, "generation": None, "assets": {}})


def scan(root, state, args):
    index = index_at(state)
    pattern = args.pattern or index.get("pattern", DEFAULT_PATTERN)
    re.compile(pattern)
    rows, skipped = [], []

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
            standard_name = current.get("standard_name", name)
            reasons = []
            if not standard_stem(standard_name, media_type, pattern):
                reasons.append("nonstandard_name")
            if not current.get("annotation"):
                reasons.append("stale_annotation" if old else "missing_annotation")
            if stamp[2] == 0:
                reasons.append("empty_file")
            rows.append({"source": relative, "kind": media_type, "fingerprint": stamp,
                         "issues": reasons, "standard_name": standard_name,
                         "annotation": current.get("annotation"), "action": "skip"})
    return {"root": str(root), "generation": index["generation"], "pattern": pattern,
            "rows": rows, "skipped": skipped}


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


def check_annotation(note):
    require(isinstance(note, dict) and all(isinstance(note.get(field), str)
            and note[field].strip() for field in ("description", "evidence")),
            "Annotation requires description and evidence")
    tags = note.get("tags")
    require(isinstance(tags, list) and tags and all(isinstance(tag, str) and tag.strip()
            for tag in tags), "Annotation requires a nonempty tags list")


def xattr(*args, path):
    require(sys.platform == "darwin", "File comments require macOS extended attributes.")
    result = subprocess.run(["/usr/bin/xattr", *args, str(path)], capture_output=True, text=True)
    require(result.returncode == 0, f"Cannot access file comment: {path}: {result.stderr.strip()}")
    return result.stdout


def file_attribute(path, attribute):
    if attribute not in xattr(path=path).splitlines():
        return None
    return bytes.fromhex(xattr("-px", attribute, path=path)).hex()


def set_attribute(path, attribute, value):
    if value is None:
        if file_attribute(path, attribute) is not None:
            xattr("-d", attribute, path=path)
    else:
        xattr("-wx", attribute, value, path=path)
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
        check_annotation(row["annotation"])
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
                    "standard_name": name, "annotation": row["annotation"], "original_path": original}
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


def move_exclusive(source, target):
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        result = library.renamex_np(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        result = library.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        raise ValueError("Exclusive rename unavailable on this platform. Use alias mode.")
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))
    sync_dir(source.parent)
    if target.parent != source.parent:
        sync_dir(target.parent)


def planned_comment(row):
    return plistlib.dumps(row["annotation"]["description"], fmt=plistlib.FMT_BINARY).hex()


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
        assets.pop(row["source"], None)
        assets[row["target"]] = {field: row[field] for field in
                                 ("kind", "fingerprint", "standard_name", "annotation", "original_path")}
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
    for relative, entry in sorted(index_at(state)["assets"].items()):
        path = source_path(root, relative)
        if fingerprint(path) != entry["fingerprint"] or (args.kind and entry["kind"] != args.kind):
            continue
        haystack = key(" ".join((relative, entry["standard_name"], entry["original_path"],
                               json.dumps(entry["annotation"], ensure_ascii=False))))
        if all(term in haystack for term in terms):
            results.append({"path": str(path), **entry})
    return results


ISSUE_TEXT = {
    "nonstandard_name": "命名待整理",
    "missing_annotation": "缺少注释",
    "stale_annotation": "注释待复核",
    "empty_file": "空文件",
}


def markdown_text(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\n", " ")


def timestamp(seconds):
    centiseconds = round(float(seconds) * 100)
    minutes, rest = divmod(centiseconds, 6000)
    whole, fraction = divmod(rest, 100)
    return f"{minutes:02d}:{whole:02d}.{fraction:02d}"


def render_markdown(root, state, args, archive_preview=None):
    target = root / MARKDOWN
    if os.path.lexists(target):
        require(target.is_file() and not target.is_symlink() and
                target.read_text(encoding="utf-8").startswith(GENERATED + "\n"),
                f"Existing Markdown is not managed by this skill: {target}")
    proposed, digest = {}, None
    if archive_preview:
        proposed, digest = archive_preview
    elif args.plan:
        raw = args.plan.read_bytes()
        plan = json.loads(raw)
        require(build(root, state, plan) == plan, "Plan differs from current validation.")
        proposed = {r["source"]: r for r in plan["rows"]}
        digest = hashlib.sha256(raw).hexdigest()
    report = scan(root, state, argparse.Namespace(pattern=None))
    rows = []
    for scanned in report["rows"]:
        source = scanned["source"]
        row = proposed.get(source, scanned)
        if source in proposed:
            status = "待确认 · 索引"
            if row["action"] == "archive":
                status = "待确认 · 改名、写备注和标签并归档"
            elif row["action"] == "rename":
                status = "待确认 · 改文件名" if row["target"] != source else "待确认 · 文件名沿用"
                if row.get("write_file_description"):
                    status += "及写入文件备注"
        else:
            status = "、".join(ISSUE_TEXT[i] for i in scanned["issues"]) or "已索引"
        rows.append((row, status))
    rows.sort(key=lambda pair: ((pair[0].get("annotation") or {}).get("capture_time") or "", pair[0]["source"]))
    count = len(rows)
    videos = sum(row["kind"] == "video" for row, _ in rows)
    parts = [GENERATED, "# 素材索引", f"{count} 条素材 · {videos} 条视频 · {count - videos} 张图片"]
    if digest:
        parts.append(f"待确认方案 · SHA-256 `{digest}`。表中的链接指向当前文件。")
    parts.append("时间码对应原片。抽帧时间点用于定位，剪辑入出点和同期声以回看片段为准。")
    table = ["| 当前文件 | 标准名 | 状态 | 画面描述 |",
             "| --- | --- | --- | --- |"]
    if archive_preview:
        table[0] += " 归档目标 |"
        table[1] += " --- |"
    details = []
    for row, status in rows:
        source = row["source"]
        title = markdown_text(row["standard_name"])
        link = f"[{markdown_text(source).replace('[', '&#91;').replace(']', '&#93;')}](./{quote(source)})"
        a = row.get("annotation") or {}
        description = markdown_text(a.get("description", "待核验"))
        table_row = f"| {link} | {title} | {status} | {description} |"
        info = [f"### {title}", f"文件 {link} · {status}", description]
        if archive_preview:
            destination = markdown_text(row.get("archive_destination", ""))
            table_row += f" {destination or '沿用当前路径'} |"
            if destination:
                info.append(f"**归档目标** {destination}")
        table.append(table_row)
        for field, label in (("orientation", "画幅"), ("capture_time", "拍摄时间"),
                             ("shot", "镜头"), ("usage", "用途"), ("evidence", "证据")):
            if a.get(field):
                info.append(f"**{label}** {markdown_text(a[field])}")
        if "duration_s" in a:
            info.append(f"**时长** {timestamp(a['duration_s'])}")
        if a.get("tags"):
            info.append(f"**标签** {markdown_text('、'.join(a['tags']))}")
        if a.get("limitations"):
            limits = a["limitations"]
            info.append(f"**选片注意** {markdown_text('。'.join(limits) if isinstance(limits, list) else limits)}")
        points = []
        for segment in a.get("segments", []):
            if "at_s" in segment:
                time = timestamp(segment["at_s"])
            elif "in_s" in segment and "out_s" in segment:
                time = f"{timestamp(segment['in_s'])}–{timestamp(segment['out_s'])}"
            else:
                continue
            points.append(f"- `{time}` {markdown_text(segment.get('note', ''))}")
        if points:
            info.append("\n".join(points))
        details.append("\n\n".join(info))
    parts.extend(("\n".join(table), "## 逐条注释", *details))
    if report["skipped"]:
        parts.append("## 跳过或无法访问")
        parts.extend(f"- {markdown_text(item['path'])} · {markdown_text(item['reason'])}"
                     for item in report["skipped"])
    save_bytes(target, ("\n\n".join(parts) + "\n").encode())
    return {"markdown": str(target), "assets": count, "skipped": len(report["skipped"]),
            "proposed": len(proposed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "plan", "apply", "rollback", "search", "render"):
        commands.add_parser(name).add_argument("directory", type=Path)
    commands.choices["scan"].add_argument("--pattern", help="Full-match regex for the filename stem")
    commands.choices["plan"].add_argument("review", type=Path)
    commands.choices["apply"].add_argument("plan", type=Path)
    commands.choices["apply"].add_argument("--sha256", required=True)
    commands.choices["rollback"].add_argument("--confirm", action="store_true")
    commands.choices["render"].add_argument("--plan", type=Path)
    commands.choices["search"].add_argument("terms", nargs="*")
    commands.choices["search"].add_argument("--kind", choices=("image", "video"))
    args = parser.parse_args()
    try:
        root = args.directory.resolve(strict=True)
        require(root.is_dir(), f"Not a directory: {root}")
        state = root / STATE
        require(not state.is_symlink() and (not state.exists() or state.is_dir()), f"Invalid state: {state}")
        if args.command == "apply":
            raw = args.plan.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == args.sha256, "Plan SHA-256 does not match reviewed bytes.")
            args.plan = json.loads(raw)
        mutation = args.command == "apply" or (args.command == "rollback" and args.confirm)
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
    if args.command == "plan":
        return build(root, state, read(args.review))
    if args.command == "apply":
        return apply(root, state, args.plan)
    if args.command == "rollback":
        return rollback(root, state, args)
    if args.command == "render":
        return render_markdown(root, state, args)
    return search(root, state, args)


if __name__ == "__main__":
    main()
