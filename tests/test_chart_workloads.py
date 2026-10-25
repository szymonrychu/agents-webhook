"""Chart workloads: three Deployments, the secrets split and per-component annotations."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "chart" / "agents-webhook"

DB_PASS = "".join(["db", "-", "fixture", "-", "value"])
TOKEN = "".join(["orch", "-", "fixture", "-", "value"])

BASE = {
    "image": {"tag": "1.2.3"},
    "postgres": {"url": "postgresql://" + "u:" + DB_PASS + "@db:5432/w"},
    "orchestrator": {"url": "http://orch.agents.svc", "token": TOKEN},
}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _run(values: dict | None) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    assert helm, "helm binary not found on PATH; the chart tests require it"
    with tempfile.TemporaryDirectory() as tmp:
        vf = Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values or {}))
        return subprocess.run(
            [helm, "template", "wh", str(CHART), "-n", "agents", "-f", str(vf)],
            capture_output=True,
            text=True,
        )


def render(overrides: dict | None = None, *, base: dict | None = None) -> list[dict]:
    proc = _run(_merge(BASE if base is None else base, overrides or {}))
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def render_error(values: dict) -> str:
    proc = _run(values)
    assert proc.returncode != 0, "helm template succeeded but was expected to fail"
    return proc.stderr


def deployments(docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d
        for d in docs
        if d["kind"] == "Deployment"
    }


def main_container(dep: dict) -> dict:
    return dep["spec"]["template"]["spec"]["containers"][0]


def env_names(container: dict) -> set[str]:
    return {e["name"] for e in container.get("env", [])}


def test_deployments_run_subcommands_after_migrate():
    deps = deployments(render())
    assert set(deps) == {"ingest", "api", "worker"}
    for name, dep in deps.items():
        spec = dep["spec"]["template"]["spec"]
        assert main_container(dep)["args"] == [name]
        assert main_container(dep)["command"] == ["agents-webhook"]
        init = spec["initContainers"][0]
        assert init["name"] == "migrate"
        assert init["args"] == ["migrate"]
        assert main_container(dep)["image"].endswith("/agents-webhook:1.2.3")
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert main_container(dep)["securityContext"]["readOnlyRootFilesystem"] is True
    assert deps["ingest"]["spec"]["replicas"] == 2
    assert deps["api"]["spec"]["replicas"] == 1


def test_deployments_http_ports_and_probes():
    deps = deployments(render())
    for name in ("ingest", "api"):
        c = main_container(deps[name])
        assert c["ports"][0]["name"] == "http" and c["ports"][0]["containerPort"] == 8000
        assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"
        assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    worker = main_container(deps["worker"])
    assert "ports" not in worker and "livenessProbe" not in worker


def test_deployments_require_image_tag_and_postgres():
    assert "image.tag is required" in render_error({**BASE, "image": {"tag": ""}})
    no_pg = {k: v for k, v in BASE.items() if k != "postgres"}
    assert "Postgres connection is required" in render_error(no_pg)


def test_deployments_dedupe_image_pull_secrets():
    docs = render({"imagePullSecrets": [{"name": "a"}, {"name": "a"}, {"name": "b"}]})
    for dep in deployments(docs).values():
        assert dep["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "a"}, {"name": "b"}]


def test_secrets_token_only_on_api_and_worker():
    deps = deployments(render())
    assert "WEBHOOK_ORCHESTRATOR_TOKEN" not in env_names(main_container(deps["ingest"]))
    for name in ("api", "worker"):
        assert "WEBHOOK_ORCHESTRATOR_TOKEN" in env_names(main_container(deps[name]))
    for dep in deps.values():
        assert "WEBHOOK_DATABASE_URL" in env_names(main_container(dep))
        init = dep["spec"]["template"]["spec"]["initContainers"][0]
        assert "WEBHOOK_DATABASE_URL" in env_names(init)


def test_secrets_existing_secrets_are_referenced():
    values = {
        "postgres": {"url": "", "existingSecret": "pg", "existingSecretKey": "dsn"},
        "orchestrator": {"token": "", "tokenSecret": {"name": "tk", "key": "t"}},
    }
    docs = render(values)
    assert not [d for d in docs if d["kind"] == "Secret"]
    api = main_container(deployments(docs)["api"])
    refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in api["env"] if "valueFrom" in e}
    assert refs["WEBHOOK_DATABASE_URL"] == {"name": "pg", "key": "dsn"}
    assert refs["WEBHOOK_ORCHESTRATOR_TOKEN"] == {"name": "tk", "key": "t"}


def test_secrets_not_in_configmap_and_no_source_secrets():
    docs = render()
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    blob = yaml.safe_dump(cm)
    assert TOKEN not in blob and DB_PASS not in blob
    assert "WEBHOOK_DATABASE_URL" not in cm["data"]
    stored = {k: v for d in docs if d["kind"] == "Secret" for k, v in d["stringData"].items()}
    assert set(stored) == {"uri", "token"}
    everything = yaml.safe_dump_all(docs)
    assert "sources" not in everything.lower().replace("resources", "")
    for name in env_names_all(docs):
        assert not (name.endswith(("_SECRET", "_TOKEN")) and name != "WEBHOOK_ORCHESTRATOR_TOKEN")


def env_names_all(docs: list[dict]) -> set[str]:
    names: set[str] = set()
    for d in docs:
        if d["kind"] == "ConfigMap":
            names |= set(d["data"])
        if d["kind"] == "Deployment":
            spec = d["spec"]["template"]["spec"]
            for c in spec["containers"] + spec["initContainers"]:
                names |= env_names(c)
    return names


def test_secrets_missing_token_fails_clearly():
    values = {**BASE, "orchestrator": {"url": "http://o"}}
    assert "orchestrator token is required" in render_error(values)


def test_config_limits_and_settings_rendered():
    docs = render(
        {
            "publicBaseUrl": "https://hooks.example",
            "trustedProxies": ["10.0.0.0/8", "192.168.0.0/16"],
            "limits": {"maxInProgress": 7, "queueCap": 9},
            "env": {"WEBHOOK_LOG_LEVEL": "DEBUG"},
        }
    )
    data = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert data["WEBHOOK_ORCHESTRATOR_URL"] == "http://orch.agents.svc"
    assert data["WEBHOOK_PUBLIC_BASE_URL"] == "https://hooks.example"
    assert data["WEBHOOK_TRUSTED_PROXIES"] == "10.0.0.0/8,192.168.0.0/16"
    assert data["WEBHOOK_MAX_IN_PROGRESS"] == "7"
    assert data["WEBHOOK_MAX_IN_PROGRESS_PER_ENDPOINT"] == "1"
    assert data["WEBHOOK_MAX_QUEUE"] == "9"
    assert data["WEBHOOK_MAX_BODY_BYTES"] == "5242880"
    assert data["WEBHOOK_LOG_LEVEL"] == "DEBUG"
    for dep in deployments(docs).values():
        refs = main_container(dep)["envFrom"]
        assert refs == [{"configMapRef": {"name": "wh-agents-webhook-settings"}}]


def test_config_rejects_foreign_env_key():
    assert "must start with WEBHOOK_" in render_error({**BASE, "env": {"FOO": "1"}})


@pytest.mark.parametrize("bad", ["ingest", "api", "worker"])
def test_annotations_are_per_component(bad):
    docs = render({"components": {bad: {"podAnnotations": {"only/here": "1"}}}})
    for name, dep in deployments(docs).items():
        ann = dep["spec"]["template"]["metadata"]["annotations"]
        assert ("only/here" in ann) == (name == bad)


def test_annotations_api_only_keeps_ingest_out_of_sso():
    sso = {"oauth2-proxy.example/inject": "true"}
    deps = deployments(render({"components": {"api": {"podAnnotations": sso}}}))
    assert sso.items() <= deps["api"]["spec"]["template"]["metadata"]["annotations"].items()
    for name in ("ingest", "worker"):
        assert (
            "oauth2-proxy.example/inject"
            not in (deps[name]["spec"]["template"]["metadata"]["annotations"])
        )


def test_lint_with_ci_values():
    helm = shutil.which("helm")
    assert helm
    cmd = [helm, "lint", str(CHART), "-f", str(CHART / "ci" / "lint-values.yaml")]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_render_without_required_values_fails():
    assert "is required" in render_error({})


def test_probes_use_numeric_port():
    deps = deployments(render())
    for name in ("ingest", "api"):
        c = main_container(deps[name])
        assert c["livenessProbe"]["httpGet"]["port"] == 8000
        assert c["readinessProbe"]["httpGet"]["port"] == 8000


def env_values(container: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}


def test_api_allowed_cidrs_rendered():
    deps = deployments(render())
    assert env_values(main_container(deps["api"]))["WEBHOOK_API_ALLOWED_CIDRS"] == "127.0.0.1/32"
    for name in ("ingest", "worker"):
        assert "WEBHOOK_API_ALLOWED_CIDRS" not in env_names(main_container(deps[name]))
    for dep in deps.values():
        init = dep["spec"]["template"]["spec"]["initContainers"][0]
        assert "WEBHOOK_API_ALLOWED_CIDRS" not in env_names(init)
    cm = next(d for d in render() if d["kind"] == "ConfigMap")
    assert "WEBHOOK_API_ALLOWED_CIDRS" not in cm["data"]
    custom = deployments(render({"api": {"allowedCidrs": ["127.0.0.1/32", "10.0.0.0/8"]}}))
    assert (
        env_values(main_container(custom["api"]))["WEBHOOK_API_ALLOWED_CIDRS"]
        == "127.0.0.1/32,10.0.0.0/8"
    )


def test_default_resources_and_template_memory():
    docs = render()
    for name, dep in deployments(docs).items():
        res = main_container(dep)["resources"]
        assert res["requests"]["memory"], name
        assert res["limits"]["memory"], name
    data = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert data["WEBHOOK_TEMPLATE_MEMORY_MB"] == "256"
    docs = render({"templateMemoryMb": 128})
    data = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert data["WEBHOOK_TEMPLATE_MEMORY_MB"] == "128"
