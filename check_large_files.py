"""Walk the repo tree and warn about files over 50MB and 100MB.

Files/folders excluded by any .gitignore encountered along the way
(root's, and any subfolder's own .gitignore — e.g. routing/.gitignore,
ABsurveys/.gitignore) are skipped, mirroring how git itself resolves
nested .gitignore rules.
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MB = 1024 * 1024
WARN_50MB = 50 * MB
WARN_100MB = 100 * MB


def _glob_to_regex(pattern: str) -> str:
    """Translate a single gitignore glob (no leading '!' or trailing '/') into
    a regex body, treating '/' as a real path separator: '**' crosses
    directories, '*' and '?' don't."""
    i, n = 0, len(pattern)
    out = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
            elif pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
                i += 1
            else:
                cls = pattern[i + 1 : j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append("[" + cls + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _parse_gitignore(gitignore_path: Path):
    """Return a list of (regex, negate, dir_only) patterns, each relative to
    gitignore_path's own directory."""
    patterns = []
    try:
        lines = gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return patterns

    for raw in lines:
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue

        negate = line.startswith("!")
        if negate:
            line = line[1:]

        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]

        anchored = line.startswith("/") or "/" in line
        line = line.lstrip("/")

        prefix = "" if anchored else "(?:.*/)?"
        regex = re.compile("^" + prefix + _glob_to_regex(line) + "$")
        patterns.append((regex, negate, dir_only))

    return patterns


def main():
    # (base_dir, patterns) pairs, root-to-leaf order; each pattern list is
    # matched against paths relative to its own base_dir.
    stack = [(ROOT, _parse_gitignore(ROOT / ".gitignore"))]

    def is_ignored(path: Path, is_dir: bool) -> bool:
        ignored = False
        for base, patterns in stack:
            rel = path.relative_to(base).as_posix()
            for regex, negate, dir_only in patterns:
                if dir_only and not is_dir:
                    continue
                if regex.match(rel):
                    ignored = not negate
        return ignored

    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirpath = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d != ".git")

        # Pop stack entries for .gitignore files that no longer apply now
        # that we've walked back out of their directory.
        while stack[-1][0] != ROOT and stack[-1][0] != dirpath and stack[-1][0] not in dirpath.parents:
            stack.pop()

        own_gitignore = dirpath / ".gitignore"
        if dirpath != ROOT and own_gitignore.is_file():
            stack.append((dirpath, _parse_gitignore(own_gitignore)))

        # Prune ignored subdirectories before descending into them.
        dirnames[:] = [d for d in dirnames if not is_ignored(dirpath / d, True)]

        for name in filenames:
            path = dirpath / name
            if is_ignored(path, False):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue

            if size >= WARN_100MB:
                print(f"WARNING [>=100MB] {size / MB:.1f}MB: {path}")
            elif size >= WARN_50MB:
                print(f"WARNING [>=50MB]  {size / MB:.1f}MB: {path}")


if __name__ == "__main__":
    main()
