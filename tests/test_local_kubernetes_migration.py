from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deploy" / "base"
LOCAL = ROOT / "deploy" / "overlays" / "local"
PRODUCTION = ROOT / "deploy" / "overlays" / "production"


def _documents(path: Path) -> list[dict]:
    return [
        item
        for item in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if item is not None
    ]


def _document(path: Path) -> dict:
    return _documents(path)[0]


def test_base_declares_three_dormant_application_workloads():
    kustomization = _document(BASE / "kustomization.yaml")
    assert set(kustomization["resources"]) == {
        "api.yaml",
        "configmap.yaml",
        "dashboard.yaml",
        "serviceaccounts.yaml",
        "services.yaml",
        "worker.yaml",
    }

    expected = {
        "api.yaml": ("jobops-api", "jobops.api", 8080),
        "dashboard.yaml": ("jobops-dashboard", "jobops.dashboard", 3000),
        "worker.yaml": ("jobops-worker", "jobops.worker", 8081),
    }
    for name, (workload, module, port) in expected.items():
        deployment = _document(BASE / name)
        assert deployment["metadata"]["name"] == workload
        assert deployment["spec"]["replicas"] == 0
        assert deployment["metadata"]["annotations"]["jobops.dev/status"] == (
            "pending-business-entrypoint"
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "jobops:demo"
        assert container["command"] == ["python", "-m", module]
        assert container["ports"][0]["containerPort"] == port
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_local_overlay_owns_demo_data_services_only():
    kustomization = _document(LOCAL / "kustomization.yaml")
    assert kustomization["namespace"] == "jobops-demo"
    assert set(kustomization["resources"]) == {
        "../../base",
        "minio.yaml",
        "namespace.yaml",
        "network-policies.yaml",
        "postgres.yaml",
        "redis.yaml",
    }
    assert "secret.example.yaml" not in kustomization["resources"]

    postgres = _documents(LOCAL / "postgres.yaml")
    redis = _documents(LOCAL / "redis.yaml")
    minio = _documents(LOCAL / "minio.yaml")
    assert {item["kind"] for item in postgres} == {"Service", "StatefulSet"}
    assert {item["kind"] for item in redis} == {"Deployment", "Service"}
    assert {item["kind"] for item in minio} == {"Service", "StatefulSet"}
    assert next(item for item in postgres if item["kind"] == "StatefulSet")["spec"][
        "replicas"
    ] == 1
    assert next(item for item in redis if item["kind"] == "Deployment")["spec"][
        "replicas"
    ] == 1
    assert next(item for item in minio if item["kind"] == "StatefulSet")["spec"][
        "replicas"
    ] == 1


def test_local_overlay_defaults_to_deny_and_has_no_internet_egress():
    policies = _documents(LOCAL / "network-policies.yaml")
    deny = next(
        item for item in policies if item["metadata"]["name"] == "default-deny-all"
    )
    assert deny["spec"]["podSelector"] == {}
    assert set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert not any(
        rule.get("to") == [{}]
        for policy in policies
        for rule in policy["spec"].get("egress", [])
    )


def test_production_overlay_has_no_in_cluster_data_workloads():
    kustomization = _document(PRODUCTION / "kustomization.yaml")
    assert kustomization["namespace"] == "jobops-production"
    assert set(kustomization["resources"]) == {"../../base", "namespace.yaml"}
    assert kustomization["patches"] == [
        {"path": "external-services-config.yaml"}
    ]
    assert "secret.example.yaml" not in kustomization["resources"]


def test_secret_examples_are_non_runnable_placeholders():
    for path in (
        LOCAL / "secret.example.yaml",
        PRODUCTION / "secret.example.yaml",
    ):
        secret = _document(path)
        assert secret["kind"] == "Secret"
        assert all(
            str(value).startswith("replace-with-")
            for value in secret["stringData"].values()
        )


def test_local_toolchain_uses_docker_desktop_and_project_scoped_clients():
    environment = (ROOT / "scripts" / "jobops_k8s_env.sh").read_text(
        encoding="utf-8"
    )
    bootstrap = (ROOT / "scripts" / "bootstrap_local_k8s_tools.sh").read_text(
        encoding="utf-8"
    )
    desktop = (ROOT / "scripts" / "download_docker_desktop.sh").read_text(
        encoding="utf-8"
    )
    local = (ROOT / "scripts" / "local_k8s.sh").read_text(encoding="utf-8")
    assert "/.tools" in environment
    assert "DOCKER_CONFIG" in environment
    assert "KUBECONFIG" in environment
    assert "KIND_VERSION=\"0.32.0\"" in bootstrap
    assert "KUBECTL_VERSION=\"1.36.3\"" in bootstrap
    assert "KUBECONFORM_VERSION=\"0.8.0\"" in bootstrap
    assert "colima" not in bootstrap.casefold()
    assert "DOCKER_DESKTOP_VERSION=\"4.85.0\"" in desktop
    assert "Docker Desktop is not running" in local
    assert 'kubectl -n jobops-demo apply --server-side -k' in local
