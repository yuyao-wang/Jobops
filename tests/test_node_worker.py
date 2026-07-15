from __future__ import annotations

import stat
from pathlib import Path

import pytest

from core.node_worker import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    NodeWorkerClient,
    NodeWorkerProtocolError,
    NodeWorkerRemoteError,
    NodeWorkerTimeout,
    NodeWorkerUnavailable,
)


@pytest.fixture(scope="module")
def client() -> NodeWorkerClient:
    try:
        return NodeWorkerClient(timeout_seconds=3)
    except NodeWorkerUnavailable:
        pytest.skip("Node.js is not installed")


def _executable(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "fake-node"
    script.write_text(f"#!/bin/sh\n{source}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _placeholder_worker(tmp_path: Path) -> Path:
    worker = tmp_path / "worker.mjs"
    worker.write_text("// input for fake executables\n", encoding="utf-8")
    return worker


def test_capabilities_are_versioned_and_complete(client: NodeWorkerClient) -> None:
    capabilities = client.capabilities()

    assert capabilities.worker_version == "0.1.0"
    assert capabilities.runtime_name == "node"
    assert set(capabilities.methods) == {"capabilities", "probe_url"}
    assert capabilities.adapters == (
        "greenhouse",
        "lever",
        "ashby",
        "jobvite",
        "workday",
        "generic",
    )


@pytest.mark.parametrize(
    ("url", "adapter", "supported"),
    [
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse", True),
        ("https://job-boards.eu.greenhouse.io/acme/jobs/123", "greenhouse", True),
        ("https://jobs.lever.co/acme/abc", "lever", True),
        ("https://jobs.ashbyhq.com/acme/abc", "ashby", True),
        ("https://apply.jobvite.com/acme/job/abc", "jobvite", True),
        ("https://acme.wd5.myworkdayjobs.com/Careers/job/abc", "workday", True),
        ("https://careers.example.org/jobs/abc", "generic", False),
    ],
)
def test_probe_url_is_deterministic(
    client: NodeWorkerClient, url: str, adapter: str, supported: bool
) -> None:
    first = client.probe_url(url)
    second = client.probe_url(url)

    assert first == second
    assert first.adapter == adapter
    assert first.supported is supported
    assert first.deterministic is True
    assert first.match_basis == "hostname"


def test_probe_does_not_classify_unrelated_greenhouse_host(
    client: NodeWorkerClient,
) -> None:
    assert client.probe_url("https://support.greenhouse.io/article/1").adapter == "generic"


@pytest.mark.parametrize(
    "url",
    [
        "not a url",
        "file:///private/profile.json",
        "https://user:secret@jobs.lever.co/acme/abc",
    ],
)
def test_invalid_url_returns_sanitized_remote_error(
    client: NodeWorkerClient, url: str
) -> None:
    with pytest.raises(NodeWorkerRemoteError) as caught:
        client.probe_url(url)

    assert caught.value.code == "INVALID_URL"
    assert "secret" not in str(caught.value)
    assert url not in str(caught.value)


def test_client_payload_is_stdin_not_argv(tmp_path: Path) -> None:
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.txt"
    script = _executable(
        tmp_path,
        f'printf "%s\\n" "$@" > "{args_file}"\n'
        f'IFS= read -r line\n'
        f'printf "%s\\n" "$line" > "{stdin_file}"\n'
        "id=$(printf '%s' \"$line\" | sed -n 's/.*\"id\":\"\\([^\"]*\\)\".*/\\1/p')\n"
        f'printf \'{{"protocol":"{PROTOCOL_NAME}","version":{PROTOCOL_VERSION},'
        '"id":"%s","ok":true,"result":{}}\\n\' "$id"',
    )
    worker = _placeholder_worker(tmp_path)
    client = NodeWorkerClient(node_executable=script, worker_path=worker)

    secret = "private-answer-9f6a"
    result = client.request("future_method", {"answer": secret})

    assert dict(result) == {}
    assert secret not in args_file.read_text(encoding="utf-8")
    assert secret in stdin_file.read_text(encoding="utf-8")


def test_client_rejects_malformed_json_without_echoing_output(tmp_path: Path) -> None:
    secret = "private-output-774"
    script = _executable(tmp_path, f"printf '%s\\n' '{secret}'")
    client = NodeWorkerClient(
        node_executable=script, worker_path=_placeholder_worker(tmp_path)
    )

    with pytest.raises(NodeWorkerProtocolError) as caught:
        client.request("capabilities")

    assert secret not in str(caught.value)


def test_client_rejects_wrong_version(tmp_path: Path) -> None:
    script = _executable(
        tmp_path,
        "IFS= read -r line\n"
        "id=$(printf '%s' \"$line\" | sed -n 's/.*\"id\":\"\\([^\"]*\\)\".*/\\1/p')\n"
        f'printf \'{{"protocol":"{PROTOCOL_NAME}","version":999,'
        '"id":"%s","ok":true,"result":{}}\\n\' "$id"',
    )
    client = NodeWorkerClient(
        node_executable=script, worker_path=_placeholder_worker(tmp_path)
    )

    with pytest.raises(NodeWorkerProtocolError, match="version mismatch"):
        client.request("capabilities")


def test_client_times_out(tmp_path: Path) -> None:
    script = _executable(tmp_path, "sleep 2")
    client = NodeWorkerClient(
        node_executable=script,
        worker_path=_placeholder_worker(tmp_path),
        timeout_seconds=0.05,
    )

    with pytest.raises(NodeWorkerTimeout, match="timed out"):
        client.request("capabilities")


def test_client_uses_minimal_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "env.txt"
    script = _executable(
        tmp_path,
        f'printf "%s" "${{JOBOPS_TEST_PRIVATE_VALUE-unset}}" > "{output}"\n'
        "IFS= read -r line\n"
        "id=$(printf '%s' \"$line\" | sed -n 's/.*\"id\":\"\\([^\"]*\\)\".*/\\1/p')\n"
        f'printf \'{{"protocol":"{PROTOCOL_NAME}","version":{PROTOCOL_VERSION},'
        '"id":"%s","ok":true,"result":{}}\\n\' "$id"',
    )
    monkeypatch.setenv("JOBOPS_TEST_PRIVATE_VALUE", "must-not-leak")
    client = NodeWorkerClient(
        node_executable=script, worker_path=_placeholder_worker(tmp_path)
    )

    client.request("future_method")

    assert output.read_text(encoding="utf-8") == "unset"


def test_missing_worker_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(NodeWorkerUnavailable):
        NodeWorkerClient(worker_path=tmp_path / "missing.mjs")
