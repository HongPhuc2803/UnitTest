#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    ".gradle",
}

MAX_FILE_BYTES = 1_000_000  # avoid scanning huge artifacts

# Broad URI patterns (best-effort; intentionally permissive)
URI_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("mongodb", re.compile(r"mongodb(?:\+srv)?:\/\/[^\s'\"<>]+", re.IGNORECASE)),
    ("redis", re.compile(r"redis:\/\/[^\s'\"<>]+", re.IGNORECASE)),
    ("jdbc", re.compile(r"jdbc:[^\s'\"<>]+", re.IGNORECASE)),
]

# Common config keys that often contain DB endpoints or creds
KEY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("spring.datasource.url", re.compile(r"^\s*spring\.datasource\.url\s*=\s*(.+?)\s*$")),
    ("spring.datasource.username", re.compile(r"^\s*spring\.datasource\.username\s*=\s*(.+?)\s*$")),
    ("spring.datasource.password", re.compile(r"^\s*spring\.datasource\.password\s*=\s*(.+?)\s*$")),
    ("spring.redis.host", re.compile(r"^\s*spring\.redis\.host\s*=\s*(.+?)\s*$")),
    ("spring.redis.port", re.compile(r"^\s*spring\.redis\.port\s*=\s*(.+?)\s*$")),
    ("spring.redis.url", re.compile(r"^\s*spring\.redis\.url\s*=\s*(.+?)\s*$")),
    ("DATABASE_URL", re.compile(r"^\s*DATABASE_URL\s*=\s*(.+?)\s*$")),
    ("DB_URL", re.compile(r"^\s*DB_URL\s*=\s*(.+?)\s*$")),
    ("MONGO_URL", re.compile(r"^\s*MONGO_URL\s*=\s*(.+?)\s*$")),
    ("MONGO_URI", re.compile(r"^\s*MONGO_URI\s*=\s*(.+?)\s*$")),
    ("REDIS_URL", re.compile(r"^\s*REDIS_URL\s*=\s*(.+?)\s*$")),
]


@dataclass(frozen=True)
class Hit:
    kind: str
    file: Path
    line_no: int
    value: str


def _is_probably_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(2048)
        return b"\x00" in chunk
    except OSError:
        return True


def _should_scan_file(path: Path) -> bool:
    # Skip huge files and binaries
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size > MAX_FILE_BYTES:
        return False
    if _is_probably_binary(path):
        return False

    # Skip common lockfiles/artifacts by extension only when huge; otherwise scan.
    return True


def walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            files.append(Path(dirpath) / name)
    return files


def scan_file(path: Path) -> list[Hit]:
    hits: list[Hit] = []
    if not _should_scan_file(path):
        return hits

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return hits

    lines = text.splitlines()
    for idx, line in enumerate(lines, start=1):
        # Key-based matches (more precise)
        for kind, pat in KEY_PATTERNS:
            m = pat.search(line)
            if m:
                value = m.group(1).strip()
                hits.append(Hit(kind=kind, file=path, line_no=idx, value=value))

        # URI matches (broad)
        for kind, pat in URI_PATTERNS:
            for m in pat.finditer(line):
                hits.append(Hit(kind=kind, file=path, line_no=idx, value=m.group(0)))

    return hits


def infer_from_deps(root: Path) -> list[str]:
    notes: list[str] = []

    pom = root / "EngHub" / "pom.xml"
    if pom.exists():
        try:
            pom_text = pom.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pom_text = ""
        if "org.postgresql" in pom_text or ">postgresql<" in pom_text:
            notes.append("EngHub: Maven dependency includes PostgreSQL driver (JDBC).")
        if "spring-boot-starter-data-redis" in pom_text:
            notes.append("EngHub: Maven dependency includes Spring Data Redis (default host usually localhost:6379 if not configured).")

    req = root / "backend-fastapi" / "requirements.txt"
    if req.exists():
        try:
            req_text = req.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            req_text = ""
        if re.search(r"(?m)^\s*pymongo\s*$", req_text):
            notes.append("backend-fastapi: requirements include pymongo (MongoDB).")

    return notes


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd().resolve()
    self_path = Path(__file__).resolve()
    files = walk_files(root)

    all_hits: list[Hit] = []
    for f in files:
        if f.resolve() == self_path:
            continue
        all_hits.extend(scan_file(f))

    # De-dupe same (kind, file, line, value)
    seen = set()
    deduped: list[Hit] = []
    for h in all_hits:
        key = (h.kind, str(h.file), h.line_no, h.value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)

    deduped.sort(key=lambda h: (h.kind, str(h.file), h.line_no))

    print(f"Scanned: {root}")
    print(f"Files scanned: {len(files)}")
    print(f"Hits: {len(deduped)}")
    print()

    notes = infer_from_deps(root)
    if notes:
        print("Dependency hints:")
        for n in notes:
            print(f"- {n}")
        print()

    if not deduped:
        print("No DB/connection-string patterns found (after exclusions).")
        return 0

    print("Matches:")
    for h in deduped:
        rel = h.file.relative_to(root) if h.file.is_relative_to(root) else h.file
        print(f"- {h.kind}: {rel}:{h.line_no} -> {h.value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
