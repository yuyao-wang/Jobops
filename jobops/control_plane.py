"""Kubernetes control-plane entry point for the real Workday Golden Path."""

from __future__ import annotations

import argparse
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from auth.credentials import SQLiteCredentialStore
from core.authenticated_subject import (
    KeychainAuthenticatedSubjectSessionProvider,
    LocalAuthenticatedSubjectSessionIssuer,
)
from core.event_ledger import EventLedger
from core.permits import PermitService
from core.real_application_control_plane import RealApplicationControlPlane
from dashboard.authentication import (
    LocalDashboardSessionController,
    make_authenticated_subject_dependency,
)


DEFAULT_CONTROL_HOME = Path("/var/lib/jobops")


def _owner_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _load_or_create_text(path: Path, factory) -> str:
    try:
        if path.is_symlink():
            raise RuntimeError("control-plane secret path is not a regular file")
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = str(factory())
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return value
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("control-plane secret path is not a regular file")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError("control-plane secret storage is empty")
        return value
    except OSError as exc:
        raise RuntimeError("control-plane secret storage is unavailable") from exc


def _configured_secret(
    *, env_name: str, path: Path, factory
) -> str:
    supplied = os.environ.get(env_name)
    if supplied is not None:
        value = supplied.strip()
        if not value:
            raise RuntimeError(f"{env_name} is empty")
        return value
    return _load_or_create_text(path, factory)


def _control_home(value: str | None = None) -> Path:
    raw = value or os.environ.get("JOBOPS_CONTROL_HOME") or str(DEFAULT_CONTROL_HOME)
    path = Path(raw).expanduser().resolve()
    return _owner_directory(path)


def build_control_plane_application(*, home: Path | None = None):
    """Install the real-application control boundary without starting I/O."""

    root = _control_home(str(home) if home is not None else None)
    secret_root = _owner_directory(root / "secrets")
    try:
        permit_secret = bytes.fromhex(_configured_secret(
            env_name="JOBOPS_PERMIT_HMAC_HEX",
            path=secret_root / "permit-hmac.hex",
            factory=lambda: secrets.token_hex(32),
        ))
    except ValueError as exc:
        raise RuntimeError("JOBOPS_PERMIT_HMAC_HEX is invalid") from exc
    if len(permit_secret) != 32:
        raise RuntimeError("permit HMAC key must contain exactly 32 bytes")
    dashboard_master = _configured_secret(
        env_name="JOBOPS_DASHBOARD_MASTER_SECRET",
        path=secret_root / "dashboard-session.key",
        factory=lambda: secrets.token_urlsafe(48),
    )
    enrollment_secret = _configured_secret(
        env_name="JOBOPS_WORKER_ENROLLMENT_TOKEN",
        path=secret_root / "worker-enrollment.token",
        factory=lambda: secrets.token_urlsafe(48),
    )
    subject_id = str(
        os.environ.get("JOBOPS_SUBJECT_ID") or "subject-local-primary"
    ).strip()
    if not subject_id:
        raise RuntimeError("JOBOPS_SUBJECT_ID is required")

    ledger = EventLedger(root / "event-ledger.sqlite3")
    session_store = SQLiteCredentialStore(root / "dashboard-credentials.sqlite3")
    session_provider = KeychainAuthenticatedSubjectSessionProvider(session_store)
    session_issuer = LocalAuthenticatedSubjectSessionIssuer(
        session_writer=session_provider,
        subject_id=subject_id,
        master_secret=dashboard_master,
        ttl_seconds=3600,
    )
    permit_service = PermitService(
        secret=permit_secret,
        ledger=ledger,
        signer_key_id="kubernetes-pvc:real-application-v1",
    )
    control = RealApplicationControlPlane(
        ledger=ledger,
        permit_service=permit_service,
        subject_id=subject_id,
        enrollment_secret=enrollment_secret,
    )

    from dashboard.server import app, configure_real_application_control_plane

    configure_real_application_control_plane(
        application=app,
        control_plane=control,
        local_session_controller=LocalDashboardSessionController(
            issuer=session_issuer,
            clock=lambda: datetime.now(timezone.utc),
        ),
        authenticated_subject=make_authenticated_subject_dependency(
            session_provider=session_provider,
            clock=lambda: datetime.now(timezone.utc),
        ),
    )
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("serve", "print-enrollment-token"), nargs="?", default="serve"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = _control_home()
    if args.command == "print-enrollment-token":
        configured = os.environ.get("JOBOPS_WORKER_ENROLLMENT_TOKEN")
        if configured:
            print(configured.strip())
            return 0
        token_path = root / "secrets" / "worker-enrollment.token"
        if not token_path.is_file():
            raise SystemExit("control plane has not initialized enrollment")
        # This command is explicit operator output and is never run by the Pod.
        print(token_path.read_text(encoding="utf-8").strip())
        return 0
    build_control_plane_application(home=root)
    from dashboard.server import run_server

    run_server(
        host=args.host,
        port=args.port,
        allow_kubernetes_bind=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
