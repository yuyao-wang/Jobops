"""Explicit CLI for creating one formal real-application control-plane task.

This command never clicks Submit.  It only transfers a bounded review
projection and hashes from an existing formal ApplicationBundle assembly.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
from pathlib import Path

from core.private_home import PrivateHome
from core.production_application_bootstrap import (
    load_production_application_config,
    resolve_production_config_path,
)
from jobops.control_client import RealApplicationControlClient
from jobops.control_client import ControlPlaneClientError
from jobops.real_application import (
    RealApplicationPreparationError,
    load_formal_real_application,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jobops.live_apply",
        description="Prepare one formal Workday attempt for explicit Dashboard review.",
    )
    parser.add_argument(
        "--server", default="http://127.0.0.1:9000", help="Control-plane origin."
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Production application config; defaults to JOBOPS_CONFIG_FILE/platform location.",
    )
    parser.add_argument(
        "--assembly-record",
        required=True,
        help="Existing formal ApplicationBundle assembly record ID.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    config_path = resolve_production_config_path(cli_path=args.config)
    config = load_production_application_config(config_path)
    subject_id = config.authentication.local_subject_id
    preparation, _bundle = load_formal_real_application(
        subject_id=subject_id,
        assembly_record_id=args.assembly_record,
        home=PrivateHome(config.private_home.root),
    )
    client = RealApplicationControlClient(args.server)
    if not client.has_session():
        enrollment = getpass.getpass(
            "Paste the one-time worker enrollment token (input hidden): "
        )
        await client.enroll(enrollment)
    result = await client.prepare(preparation.to_dict())
    print(
        "Real application task "
        f"{result.get('status', 'UNKNOWN')}: attempt={preparation.attempt_id} "
        f"provider={preparation.provider} external_job_id={preparation.external_job_id} "
        f"bundle_sha256={preparation.bundle_canonical_hash}"
    )
    print(
        "No external submission occurred. Start the local browser executor, "
        "then review and explicitly approve this attempt in Dashboard."
    )
    return 0


def main() -> int:
    try:
        return asyncio.run(_run(_parser().parse_args()))
    except (ControlPlaneClientError, RealApplicationPreparationError) as exc:
        print(f"Real application preparation stopped safely: {exc}")
        return 10
    except Exception as exc:
        print(
            "Real application preparation stopped safely with an internal failure: "
            f"{type(exc).__name__}"
        )
        return 50


if __name__ == "__main__":
    raise SystemExit(main())
