"""scripts/next_version.py: the next release version from conventional commits.

Every case builds a real temporary git repository; git output is never mocked. This file is
copied byte for byte into every repository that releases with the script, so nothing in it
may be specific to one repository.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "next_version.py"
ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, env=ENV, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    return path


def _commit(repo: Path, message: str, tag: str = "") -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    if tag:
        _git(repo, "tag", tag)
    return _git(repo, "rev-parse", "HEAD")


def _next(repo: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo, env=ENV, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _released(path: Path, base: str, *messages: str) -> str:
    repo = _repo(path)
    _commit(repo, "chore: init", tag=base)
    for message in messages:
        _commit(repo, message)
    return _next(repo)


def test_no_tag_gives_0_1_0(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "feat!: drop the old api")
    _commit(repo, "fix: a bug")
    assert _next(repo) == "0.1.0\n"


def test_fix_chore_docs_bump_patch(tmp_path):
    messages = ("fix: a bug", "chore: deps", "docs: readme", "Merge pull request #5 from x/y")
    assert _released(tmp_path, "v1.2.3", *messages) == "1.2.4\n"


def test_feat_bumps_minor(tmp_path):
    assert _released(tmp_path, "v1.2.3", "fix: a bug", "feat(scope): a thing") == "1.3.0\n"


def test_feat_type_is_case_insensitive(tmp_path):
    for i, subject in enumerate(("Feat: a thing", "FEAT(x): a thing")):
        assert _released(tmp_path / str(i), "v1.2.3", subject) == "1.3.0\n", subject


def test_bang_bumps_major(tmp_path):
    for i, subject in enumerate(("feat!: a", "fix!: b", "refactor(api)!: c")):
        assert _released(tmp_path / str(i), "v1.2.3", "fix: a bug", subject) == "2.0.0\n", subject


def test_breaking_change_footer_bumps_major(tmp_path):
    bodies = ("fix: a\n\nBREAKING CHANGE: the api moved", "chore: b\n\nBREAKING-CHANGE: gone")
    for i, message in enumerate(bodies):
        assert _released(tmp_path / str(i), "v1.2.3", message) == "2.0.0\n", message


def test_breaking_on_0x_bumps_major(tmp_path):
    assert _released(tmp_path, "v0.3.1", "feat!: a") == "1.0.0\n"


def test_merge_commit_branch_commits_counted(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "chore: init", tag="v1.0.0")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "feat: a thing")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #1 from x/y")
    assert _next(repo) == "1.1.0\n"


def test_highest_semver_tag_is_base(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "chore: one", tag="v0.9.0")
    _commit(repo, "chore: two", tag="v0.10.0")
    _commit(repo, "chore: three")
    for name in ("v1.2", "vfoo", "v2.0.0-rc1"):
        _git(repo, "tag", name)
    _commit(repo, "fix: a bug", tag="v3")
    assert _next(repo) == "0.10.1\n"


def test_head_tagged_prints_nothing(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "feat: a thing", tag="v1.0.0")
    assert _next(repo) == ""


def test_head_contained_in_newer_tag_prints_nothing(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "chore: init", tag="v1.0.0")
    c1 = _commit(repo, "fix: a bug")
    _commit(repo, "feat: a thing", tag="v1.1.0")
    _git(repo, "checkout", "-q", "--detach", c1)
    assert _next(repo) == ""


def test_script_is_stdlib_only():
    tree = ast.parse(SCRIPT.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            modules.add(node.module.split(".")[0])
    assert modules
    assert modules <= sys.stdlib_module_names, modules - sys.stdlib_module_names
