from __future__ import annotations

import stat
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from dashboard.server import app as dashboard_app
from jobops.control_plane import build_control_plane_application


ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "k8s" / "real-application"


def _document(name: str):
    return yaml.safe_load((K8S / name).read_text(encoding="utf-8"))


def test_kubernetes_manifest_is_single_replica_nonroot_and_probe_complete():
    kustomization = _document("kustomization.yaml")
    assert set(kustomization["resources"]) == {
        "configmap.yaml",
        "deployment.yaml",
        "pvc.yaml",
        "service.yaml",
    }
    assert "secret.example.yaml" not in kustomization["resources"]

    deployment = _document("deployment.yaml")
    assert deployment["kind"] == "Deployment"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["args"][-2:] == ["--port", "9000"]
    assert container["ports"] == [
        {"name": "http", "containerPort": 9000, "protocol": "TCP"}
    ]
    assert container["readinessProbe"]["httpGet"]["path"] == "/api/health"
    assert container["livenessProbe"]["httpGet"]["path"] == "/api/live"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["resources"]["requests"]
    assert container["resources"]["limits"]
    assert any(
        item["mountPath"] == "/var/lib/jobops"
        for item in container["volumeMounts"]
    )

    service = _document("service.yaml")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {
            "name": "http",
            "port": 9000,
            "targetPort": "http",
            "protocol": "TCP",
        }
    ]
    config = _document("configmap.yaml")
    assert config["data"]["JOBOPS_CONTROL_HOME"] == "/var/lib/jobops/control"
    assert _document("pvc.yaml")["kind"] == "PersistentVolumeClaim"
    assert not any(
        document.get("kind") == "Ingress"
        for document in (
            config,
            deployment,
            _document("service.yaml"),
            _document("pvc.yaml"),
        )
    )


def test_secret_example_is_not_a_real_secret_and_control_bootstrap_is_ready(
    tmp_path: Path,
):
    example = _document("secret.example.yaml")
    assert example["kind"] == "Secret"
    assert all(
        str(value).startswith("replace-with-")
        for value in example["stringData"].values()
    )

    saved_state = dict(dashboard_app.state._state)
    try:
        application = build_control_plane_application(home=tmp_path / "control")
        with TestClient(application) as client:
            health = client.get("/api/health")
            live = client.get("/api/live")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert live.status_code == 200
        assert live.json() == {"status": "alive"}
        secret_root = tmp_path / "control" / "secrets"
        assert {
            item.name for item in secret_root.iterdir() if item.is_file()
        } == {
            "dashboard-session.key",
            "permit-hmac.hex",
            "worker-enrollment.token",
        }
        assert all(
            stat.S_IMODE(item.stat().st_mode) == 0o600
            for item in secret_root.iterdir()
        )
        assert not hasattr(application.state, "browser_runtime")
    finally:
        dashboard_app.state._state.clear()
        dashboard_app.state._state.update(saved_state)
