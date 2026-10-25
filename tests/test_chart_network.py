"""Chart network: Services and the two Ingresses (public ingest vs SSO api)."""

from tests.test_chart_workloads import BASE, deployments, main_container, render

ING = {
    "ingress": {
        "enabled": True,
        "className": "nginx",
        "host": "lab.example.test",
        "tls": {"secretName": "lab-tls"},
    }
}


def by_kind(docs, kind):
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def paths(ingress):
    return {
        p["path"]: (p["pathType"], p["backend"]["service"]["name"])
        for r in ingress["spec"]["rules"]
        for p in r["http"]["paths"]
    }


def test_services_map_port_80_to_http():
    svcs = by_kind(render(ING), "Service")
    assert set(svcs) == {"wh-agents-webhook-ingest", "wh-agents-webhook-api"}
    for name, svc in svcs.items():
        port = svc["spec"]["ports"][0]
        assert port["port"] == 80 and port["targetPort"] == "http"
        comp = name.rsplit("-", 1)[1]
        assert svc["spec"]["selector"]["app.kubernetes.io/component"] == comp


def test_paths_route_api_and_ingest_separately():
    ings = by_kind(render(ING), "Ingress")
    assert paths(ings["wh-agents-webhook-api"]) == {
        "/webhooks/api": ("Prefix", "wh-agents-webhook-api")
    }
    assert paths(ings["wh-agents-webhook-ingest"]) == {
        "/webhook": ("Prefix", "wh-agents-webhook-ingest")
    }
    for ing in ings.values():
        assert ing["spec"]["rules"][0]["host"] == "lab.example.test"
        assert ing["spec"]["ingressClassName"] == "nginx"


def test_tls_secret_used_and_no_cert_manager_by_default():
    for ing in by_kind(render(ING), "Ingress").values():
        assert ing["spec"]["tls"] == [{"hosts": ["lab.example.test"], "secretName": "lab-tls"}]
        assert "cert-manager.io/cluster-issuer" not in ing["metadata"].get("annotations", {})


def test_cert_manager_annotation_only_when_set():
    ing = {"ingress": {**ING["ingress"], "annotations": {"cert-manager.io/cluster-issuer": "x"}}}
    ings = by_kind(render(ing), "Ingress")
    assert (
        ings["wh-agents-webhook-api"]["metadata"]["annotations"]["cert-manager.io/cluster-issuer"]
        == "x"
    )


def test_disabled_renders_no_ingress():
    assert not by_kind(render(), "Ingress")
    assert not by_kind(render({"ingress": {"enabled": False}}), "Ingress")


def test_ingest_ingress_body_size_timeouts_and_no_sso():
    ings = by_kind(render({"ingress": {**ING["ingress"], "annotations": {"sso": "on"}}}), "Ingress")
    ingest = ings["wh-agents-webhook-ingest"]["metadata"]["annotations"]
    assert ingest["nginx.ingress.kubernetes.io/proxy-body-size"] == "5m"
    assert ingest["nginx.ingress.kubernetes.io/proxy-read-timeout"] == "60"
    assert ingest["nginx.ingress.kubernetes.io/proxy-send-timeout"] == "60"
    assert "sso" not in ingest
    assert ings["wh-agents-webhook-api"]["metadata"]["annotations"]["sso"] == "on"


def test_ingest_annotations_apply_to_ingest_only():
    ing = {"ingress": {**ING["ingress"], "ingestAnnotations": {"a": "b"}}}
    ings = by_kind(render(ing), "Ingress")
    assert ings["wh-agents-webhook-ingest"]["metadata"]["annotations"]["a"] == "b"
    assert "a" not in ings["wh-agents-webhook-api"]["metadata"].get("annotations", {})


def test_deny_source_ranges_on_api_ingress_only():
    ing = {"ingress": {**ING["ingress"], "denySourceRanges": ["10.0.0.1/32", "10.0.0.2/32"]}}
    ings = by_kind(render(ing), "Ingress")
    key = "nginx.ingress.kubernetes.io/denylist-source-range"
    assert (
        ings["wh-agents-webhook-api"]["metadata"]["annotations"][key] == "10.0.0.1/32,10.0.0.2/32"
    )
    assert key not in ings["wh-agents-webhook-ingest"]["metadata"]["annotations"]


def test_trusted_proxies_reach_ingest_via_settings():
    docs = render({"trustedProxies": ["10.1.0.0/16", "10.2.0.0/16"]})
    cm = by_kind(docs, "ConfigMap")["wh-agents-webhook-settings"]
    assert cm["data"]["WEBHOOK_TRUSTED_PROXIES"] == "10.1.0.0/16,10.2.0.0/16"
    ingest = main_container(deployments(docs)["ingest"])
    assert ingest["envFrom"][0]["configMapRef"]["name"] == "wh-agents-webhook-settings"


def test_base_values_still_render_services():
    assert BASE and len(by_kind(render(), "Service")) == 2


def test_image_pull_secrets_deduped_across_global_and_values():
    docs = render(
        {
            "global": {"imagePullSecrets": [{"name": "reg"}, {"name": "g-only"}]},
            "imagePullSecrets": [{"name": "reg"}, {"name": "v-only"}],
        }
    )
    for dep in deployments(docs).values():
        names = [s["name"] for s in dep["spec"]["template"]["spec"]["imagePullSecrets"]]
        assert sorted(names) == ["g-only", "reg", "v-only"]


def test_every_container_is_hardened_including_migrate_init():
    for dep in deployments(render()).values():
        spec = dep["spec"]["template"]["spec"]
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        for c in [*spec["initContainers"], *spec["containers"]]:
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"]["drop"] == ["ALL"]
            assert sc["readOnlyRootFilesystem"] is True
