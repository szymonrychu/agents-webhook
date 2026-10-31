"""Print the next release version from conventional commits since the last vX.Y.Z tag.

Run as `python3 scripts/next_version.py` from the repository work tree. Stdlib only, and
identical in every repository that releases with it.

- HEAD already in a vX.Y.Z tag (HEAD tagged, or a newer commit released): print nothing.
- No vX.Y.Z tag reachable from HEAD: print 0.1.0, without scanning history.
- Else bump the highest reachable vX.Y.Z by the highest bump of the commits since it (every
  commit, merged branches included): `type!:` or a `BREAKING CHANGE:` footer bumps major
  (also on 0.x), `feat` minor, anything else patch.

A git failure exits non-zero with git's error on stderr.
"""

import re
import subprocess
import sys

TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
BANG = re.compile(r"^[A-Za-z]+(\([^)]*\))?!:")
FEAT = re.compile(r"^feat(\([^)]*\))?:", re.IGNORECASE)
FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
PATCH, MINOR, MAJOR = 0, 1, 2


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode)
    return proc.stdout


def versions(*args: str) -> list[tuple[int, int, int]]:
    found = []
    for name in git("tag", *args).split():
        if m := TAG.match(name):
            found.append((int(m[1]), int(m[2]), int(m[3])))
    return found


def bump(message: str) -> int:
    subject = message.split("\n", 1)[0]
    if BANG.match(subject) or FOOTER.search(message):
        return MAJOR
    if FEAT.match(subject):
        return MINOR
    return PATCH


def next_version() -> str:
    if versions("--contains", "HEAD"):
        return ""
    released = versions("--merged", "HEAD")
    if not released:
        return "0.1.0"
    major, minor, patch = max(released)
    log = git("log", "--format=%B%x00", f"v{major}.{minor}.{patch}..HEAD")
    level = max((bump(m.strip()) for m in log.split("\0") if m.strip()), default=PATCH)
    if level == MAJOR:
        return f"{major + 1}.0.0"
    if level == MINOR:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


if __name__ == "__main__":
    version = next_version()
    if version:
        print(version)
