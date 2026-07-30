"""Manual, synthetic M1b smoke test. Never run from the automated test suite."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.isolated_model_runner import (
    IsolatedStructuredModelRequest,
    ManagedModelImage,
)
from utils.isolated_subscription_cli import (
    CodexSubscriptionCLIInvocationAdapter,
    IsolatedSubscriptionCLIRunner,
    runtime_model_execution_isolation_profiles,
)


_SYNTHETIC_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def _run() -> int:
    adapter = CodexSubscriptionCLIInvocationAdapter()
    profiles = runtime_model_execution_isolation_profiles(adapter)
    profile = profiles["ISOLATED_SUBSCRIPTION_CLI_V1"]
    if not profile.runner_available:
        print("M1b runtime probe failed; no model invocation was started.")
        return 2
    image = ManagedModelImage(
        media_type="image/png",
        content=_SYNTHETIC_PNG,
        byte_size=len(_SYNTHETIC_PNG),
        sha256=hashlib.sha256(_SYNTHETIC_PNG).hexdigest(),
        order=0,
        role_id="synthetic-page-1",
    )
    request = IsolatedStructuredModelRequest(
        component_id="m1b_manual_smoke",
        invocation_id="m1b-manual-smoke-v1",
        model_id=None,
        system_prompt=(
            "Inspect only the supplied synthetic image and return the schema."
        ),
        input_data={"question": "Is the synthetic image present?"},
        images=(image,),
        output_schema_name="m1b_manual_smoke",
        output_schema={
            "type": "object",
            "properties": {"image_present": {"type": "boolean"}},
            "required": ["image_present"],
            "additionalProperties": False,
        },
        timeout_seconds=120,
        max_input_bytes=100_000,
        max_output_bytes=4_096,
        max_images=1,
        prompt_contract_version="m1b-manual-smoke-prompt-v1",
        schema_contract_version="m1b-manual-smoke-schema-v1",
    )
    result = await IsolatedSubscriptionCLIRunner().execute(
        request,
        backend_adapter=adapter,
        isolation_profile=profile,
    )
    print(
        "status="
        f"{result.status.value} backend={result.backend_id} "
        f"images={result.image_count} duration_ms={result.duration_ms}"
    )
    return 0 if result.status.value == "SUCCEEDED" else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one synthetic isolated Codex subscription generation."
    )
    parser.add_argument(
        "--acknowledge-subscription-usage",
        action="store_true",
        help="Acknowledge that this manual command consumes subscription usage.",
    )
    args = parser.parse_args()
    if not args.acknowledge_subscription_usage:
        parser.error(
            "not run: pass --acknowledge-subscription-usage to explicitly "
            "authorize one synthetic subscription generation"
        )
    print(
        "Starting one synthetic M1b model generation; no repository, Private "
        "Home, or real candidate data is supplied."
    )
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
