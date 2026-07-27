"""Contract invariants for the provider-neutral semantic mapper boundary."""

from __future__ import annotations

import json

import pytest

from adapters.generic_ai.adapter import _validated_semantic_mappings
from adapters.generic_ai.models import FormControl, FormOption
from adapters.generic_ai.semantic_mapper import (
    FakeSemanticMapper,
    MappingRequest,
    MappingResponse,
    SemanticMapper,
)


SYNTHETIC_EMAIL = "candidate-4821@example.invalid"
SYNTHETIC_PHONE = "+1 555 010 4821"


def _request(*, index: int = 3) -> MappingRequest:
    return MappingRequest(
        index=index,
        role="textbox",
        tag="input",
        input_type="email",
        label="Primary electronic contact",
    )


def test_mapping_request_is_value_free_and_excludes_browser_authority() -> None:
    control = FormControl(
        index=3,
        role="textbox",
        tag="input",
        input_type="email",
        label=f"Confirm {SYNTHETIC_EMAIL}",
        name="candidate_contact",
        aria_label=f"Telephone {SYNTHETIC_PHONE}",
        required=True,
        selector="#candidate-email",
        element_id="candidate-email",
        options=(FormOption(label=SYNTHETIC_EMAIL, value="private-option-value"),),
    )

    request = MappingRequest.from_control(
        control,
        private_values=(SYNTHETIC_EMAIL, SYNTHETIC_PHONE),
    )
    payload = request.to_dict()
    encoded = json.dumps(payload, sort_keys=True)

    assert set(payload) == {
        "index",
        "role",
        "tag",
        "type",
        "label",
        "name",
        "aria_label",
        "placeholder",
        "autocomplete",
        "required",
        "options",
    }
    assert SYNTHETIC_EMAIL not in encoded
    assert SYNTHETIC_PHONE not in encoded
    assert "#candidate-email" not in encoded
    assert "private-option-value" not in encoded
    assert encoded.count("[PRIVATE]") == 3


def test_mapping_request_redacts_before_field_truncation() -> None:
    long_private_value = "private-" + ("x" * 300)
    control = FormControl(
        index=4,
        role="textbox",
        tag="input",
        # Reproduce the already-bounded value emitted by the DOM observer.
        label=f"Confirm {long_private_value}"[:240],
        required=True,
    )

    request = MappingRequest.from_control(
        control,
        private_values=(long_private_value,),
    )

    assert request.label == "Confirm [PRIVATE]"
    assert long_private_value[:232] not in request.label


@pytest.mark.parametrize(
    ("canonical_key", "status"),
    (
        ("email", "mapped"),
        ("phone_number", "mapped"),
        ("work_authorization", "needs_review"),
        ("unknown", "unsupported"),
    ),
)
def test_response_allows_only_policy_safe_key_status_pairs(
    canonical_key: str,
    status: str,
) -> None:
    response = MappingResponse.for_key(3, canonical_key)  # type: ignore[arg-type]

    assert response.canonical_key == canonical_key
    assert response.status == status


def test_response_rejects_illegal_canonical_key() -> None:
    with pytest.raises(ValueError, match="outside the mapping taxonomy"):
        MappingResponse(  # type: ignore[arg-type]
            index=3,
            canonical_key="salary",
            status="mapped",
        )


@pytest.mark.parametrize(
    ("canonical_key", "status"),
    (
        ("work_authorization", "mapped"),
        ("unknown", "needs_review"),
        ("email", "unsupported"),
        ("phone_number", "retry"),
    ),
)
def test_response_rejects_status_escalation(
    canonical_key: str,
    status: str,
) -> None:
    with pytest.raises(ValueError):
        MappingResponse(  # type: ignore[arg-type]
            index=3,
            canonical_key=canonical_key,
            status=status,
        )


@pytest.mark.asyncio
async def test_fake_mapper_is_controllable_and_records_the_contract_call() -> None:
    response = MappingResponse.for_key(3, "email")
    mapper = FakeSemanticMapper((response,))
    request = _request()

    result = await mapper.map_controls((request,))

    assert isinstance(mapper, SemanticMapper)
    assert result == (response,)
    assert mapper.calls == [(request,)]


@pytest.mark.asyncio
async def test_fake_mapper_rejects_results_outside_the_request_batch() -> None:
    mapper = FakeSemanticMapper((MappingResponse.for_key(99, "email"),))

    with pytest.raises(ValueError, match="unrequested index"):
        await mapper.map_controls((_request(index=3),))


@pytest.mark.asyncio
async def test_fake_mapper_rejects_duplicate_request_indices() -> None:
    mapper = FakeSemanticMapper()

    with pytest.raises(ValueError, match="must be unique"):
        await mapper.map_controls((_request(index=3), _request(index=3)))


def test_consumer_translates_only_mapped_results() -> None:
    requests = tuple(_request(index=index) for index in range(1, 5))
    responses = (
        MappingResponse.for_key(1, "email"),
        MappingResponse.for_key(2, "phone_number"),
        MappingResponse.for_key(3, "work_authorization"),
        MappingResponse.for_key(4, "unknown"),
    )

    mapped, needs_review = _validated_semantic_mappings(requests, responses)

    assert mapped == {1: "email", 2: "phone"}
    assert needs_review == frozenset({3})


def test_consumer_rejects_the_whole_batch_on_an_invalid_result() -> None:
    requests = (_request(index=1), _request(index=2))
    responses = (
        MappingResponse.for_key(1, "email"),
        {"index": 2, "canonical_key": "salary", "status": "mapped"},
    )

    with pytest.raises(TypeError, match="invalid response"):
        _validated_semantic_mappings(requests, responses)  # type: ignore[arg-type]
