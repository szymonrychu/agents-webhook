"""The CI workflow publishes only from main and never hands secrets to pull requests."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github/workflows/ci.yaml").read_text()
WF = yaml.safe_load(TEXT)
JOBS = WF["jobs"]
MAIN = "github.event_name == 'push' && github.ref == 'refs/heads/main'"


def steps_text(job: str) -> str:
    return yaml.safe_dump(JOBS[job]["steps"])


def test_jobs_exist():
    for job in ("lint", "test", "chart", "e2e", "images", "chart-publish", "release"):
        assert job in JOBS


def test_triggers_include_pull_requests_and_main():
    on = WF.get("on", WF.get(True))
    assert "pull_request" in on
    assert on["push"]["branches"] == ["main"]


def test_job_commands():
    assert "mise run lint" in steps_text("lint")
    assert "mise run test" in steps_text("test")
    chart = steps_text("chart")
    assert "mise run test:chart" in chart and "mise run lint:chart" in chart
    assert "mise run test:e2e" in steps_text("e2e")


def test_lint_values_file_is_linted():
    mise = (ROOT / "mise.toml").read_text()
    assert "helm lint chart/agents-webhook -f chart/agents-webhook/ci/lint-values.yaml" in mise


def test_images_name_and_main_only_push():
    assert "harbor.szymonrichert.pl/containers/agents-webhook" in TEXT
    assert "docker build" in steps_text("images")
    assert "--help" in steps_text("images")
    for step in JOBS["images"]["steps"]:
        pushes = "regctl" in str(step) or "HARBOR" in str(step)
        if pushes and "uses" not in step or "regctl-installer" in str(step.get("uses", "")):
            assert step.get("if") == MAIN, step


def test_chart_publish_conditions_and_repo():
    assert JOBS["chart-publish"]["if"] == MAIN
    assert JOBS["chart-publish"]["env"]["CHART_REPO"] == "oci://harbor.szymonrichert.pl/charts"
    assert "charts/agents-webhook" in TEXT or "$CHART_REPO/agents-webhook" in TEXT
    assert "chart-version.sh branch" in steps_text("chart-publish")


def test_secrets_only_in_publishing_steps():
    for name, job in JOBS.items():
        for step in job["steps"]:
            if "secrets." not in str(step):
                continue
            job_main = job.get("if") == MAIN
            step_main = step.get("if") == MAIN
            assert job_main or step_main, (name, step)
    assert "secrets." not in str({k: v for k, v in WF.items() if k != "jobs"})
    for name in ("lint", "test", "chart", "e2e"):
        assert "secrets." not in steps_text(name)


def test_regctl_installer_pinned_by_sha():
    uses = re.findall(r"regclient/actions/regctl-installer@(\S+)", TEXT)
    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", u) for u in uses)


@pytest.mark.parametrize(
    ("args", "code", "out"),
    [
        (["branch", "main", "abcdef1234567"], 0, "0.1.0-main.abcdef1"),
        (["tag", "v0.1.0", "abcdef1234567"], 2, ""),
        (["tag", "v9.9.9", "abcdef1234567"], 2, ""),
        (["branch", "feature", "abcdef1234567"], 2, ""),
    ],
)
def test_chart_version_script(args, code, out):
    r = subprocess.run(
        [str(ROOT / "scripts/chart-version.sh"), *args], capture_output=True, text=True
    )
    assert r.returncode == code
    assert r.stdout.strip() == out
    if code == 2:
        assert r.stdout == ""


RELEASED = "steps.version.outputs.version != ''"
RELEASE_IMAGE = "harbor.szymonrichert.pl/containers/agents-webhook"


def _copy_steps(steps: list[dict]) -> list[int]:
    return [i for i, s in enumerate(steps) if "regctl image copy" in s.get("run", "")]


def test_release_job_runs_last_on_main_push():
    job = JOBS["release"]
    assert job["if"] == MAIN
    assert set(job["needs"]) == set(JOBS) - {"release"}
    assert set(job["needs"]) == {"lint", "test", "chart", "e2e", "images", "chart-publish"}
    assert job["permissions"] == {"contents": "write"}
    assert job["concurrency"] == {"group": "release", "cancel-in-progress": False}


def test_release_job_fetches_full_history_and_tags():
    checkout = JOBS["release"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["fetch-tags"] is True


def test_release_job_computes_version_with_script():
    steps = JOBS["release"]["steps"]
    version = next(i for i, s in enumerate(steps) if s.get("id") == "version")
    # A bare assignment fails the step (bash -e) when the script fails; inside echo it would not.
    assert "shell" not in steps[version]
    assert steps[version]["run"].strip().splitlines() == [
        'version="$(python3 scripts/next_version.py)"',
        'echo "version=$version" >> "$GITHUB_OUTPUT"',
    ]
    later = steps[version + 1 :]
    assert later
    assert all(s.get("if") == RELEASED for s in later)


def test_release_job_retags_manifest_only():
    job = JOBS["release"]
    steps = job["steps"]
    assert job["env"]["IMAGE"] == RELEASE_IMAGE
    assert job["env"]["HARBOR_URL"] == "harbor.szymonrichert.pl"
    copies = [
        line.strip()
        for s in steps
        for line in s.get("run", "").splitlines()
        if "regctl image copy" in line
    ]
    assert copies == [
        'regctl image copy "$IMAGE:$SHORTSHA" "$IMAGE:$VERSION"',
        'regctl image copy "$IMAGE:$SHORTSHA" "$IMAGE:$VERSION_MINOR"',
        'regctl image copy "$IMAGE:$SHORTSHA" "$IMAGE:$VERSION_MAJOR"',
    ]
    tag = steps[_copy_steps(steps)[0]]["run"]
    assert 'SHORTSHA="${GITHUB_SHA::7}"' in tag
    assert 'VERSION_MINOR="${VERSION%.*}"' in tag
    assert 'VERSION_MAJOR="${VERSION%%.*}"' in tag
    installers = [s["uses"] for s in steps if "regctl-installer" in s.get("uses", "")]
    assert len(installers) == 1
    assert re.fullmatch(r"regclient/actions/regctl-installer@[0-9a-f]{40}", installers[0])
    logins = [s for s in steps if "regctl registry login" in s.get("run", "")]
    assert len(logins) == 1
    assert logins[0]["env"] == {
        "HARBOR_USERNAME": "${{ secrets.HARBOR_USERNAME }}",
        "HARBOR_PASSWORD": "${{ secrets.HARBOR_PASSWORD }}",
    }
    text = yaml.safe_dump(job)
    for banned in ("docker", "regctl image import", "--blob-chunk"):
        assert banned not in text, banned


def test_release_job_pushes_git_tag_last():
    steps = JOBS["release"]["steps"]
    last = steps[-1]["run"].strip().splitlines()
    assert [line.strip() for line in last] == [
        'git tag "v$VERSION" "$GITHUB_SHA"',
        'git push origin "refs/tags/v$VERSION"',
    ]
    assert max(_copy_steps(steps)) < len(steps) - 1
    assert steps[-1]["env"]["VERSION"] == "${{ steps.version.outputs.version }}"
    for s in steps:
        assert not re.search(r"(^|\s)(--force|-f)(\s|$)", s.get("run", "")), s
    assert "secrets.GITHUB_TOKEN" not in TEXT


def test_no_tag_trigger_or_tag_publish_job():
    on = WF.get("on", WF.get(True))
    assert "tags" not in on["push"]
    assert "chart-publish-tag" not in JOBS
    assert "startsWith(github.ref, 'refs/tags" not in TEXT
