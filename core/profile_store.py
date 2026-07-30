"""Load the private candidate vault without exposing it to the repository.

The vault deliberately separates facts, verified answers, and policy.  This
module converts that private representation to the legacy profile mapping used
by the existing MR.Jobs adapters while the two runtimes coexist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .job_prioritization import (
    CandidateFact,
    CandidateSummary,
    build_candidate_summary,
)
from .policy import AutonomyMode, PolicyConfig
from .private_home import PrivateHome, PrivatePaths


VAULT_SCHEMA_VERSION = 1


class ProfileStoreError(RuntimeError):
    """Raised when private candidate data is absent or malformed."""


class CandidateSummaryProviderError(ProfileStoreError):
    """Raised when no authoritative typed prioritization summary is available."""


@runtime_checkable
class CandidateSummaryProvider(Protocol):
    def get_current(
        self,
        subject_id: str,
        *,
        now: datetime,
    ) -> CandidateSummary:
        """Return the current trusted CandidateSummary for one subject."""


_KNOWN_SENSITIVITIES = frozenset(
    {
        "personal",
        "legal",
        "compensation",
        "voluntary_self_id",
        "health",
        "demographic",
        "employment",
        "education",
        "BASIC",
        "PERSONAL",
        "LEGAL",
        "COMPENSATION",
        "VOLUNTARY_SELF_ID",
        "APPLICATION_MATERIAL",
        "ATTESTATION",
        "UNSUPPORTED",
    }
)


@dataclass(frozen=True, slots=True)
class AnswerTrustReport:
    """Privacy-safe validation result for the private answer bank."""

    values: Mapping[str, Any]
    accepted_keys: tuple[str, ...]
    rejected_keys: tuple[str, ...]
    invalid_verified_keys: tuple[str, ...]

    @property
    def all_projected_answers_verified(self) -> bool:
        return not self.invalid_verified_keys


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProfileStoreError(f"private profile file is missing: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileStoreError(f"private profile file is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ProfileStoreError(f"private profile file must contain an object: {path.name}")
    version = int(value.get("schema_version", VAULT_SCHEMA_VERSION))
    if version != VAULT_SCHEMA_VERSION:
        raise ProfileStoreError(
            f"unsupported private profile schema {version} in {path.name}"
        )
    return value


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _scope_matches(
    scope: Mapping[str, Any], *, job_id: str = "", tenant: str = ""
) -> bool:
    """Allow global records or records explicitly scoped to this application."""

    if not scope:
        return True
    supported = {"job_id", "job_ids", "tenant", "tenants"}
    if set(scope) - supported:
        return False

    def _matches(single: str, plural: str, actual: str) -> bool:
        expected: list[str] = []
        if scope.get(single) is not None:
            expected.append(str(scope[single]).strip())
        raw_plural = scope.get(plural)
        if isinstance(raw_plural, (list, tuple, set)):
            expected.extend(str(item).strip() for item in raw_plural)
        expected = [item for item in expected if item]
        return not expected or bool(actual) and actual in expected

    return _matches("job_id", "job_ids", job_id) and _matches(
        "tenant", "tenants", tenant
    )


def _answer_report(
    document: Mapping[str, Any],
    *,
    job_id: str = "",
    tenant: str = "",
    now: datetime | None = None,
) -> AnswerTrustReport:
    records = document.get("answers", {})
    if not isinstance(records, Mapping):
        raise ProfileStoreError("verified-answers.json answers must be an object")
    active_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    values: dict[str, Any] = {}
    rejected: list[str] = []
    invalid_verified: list[str] = []
    for key, record in records.items():
        answer_key = str(key)
        if not isinstance(record, Mapping):
            rejected.append(answer_key)
            continue
        if record.get("verified") is not True:
            rejected.append(answer_key)
            continue
        source = record.get("source")
        sensitivity = record.get("sensitivity")
        scope = record.get("scope")
        confirmed_at = _parse_timestamp(record.get("confirmed_at"))
        expires_raw = record.get("expires_at")
        expires_at = _parse_timestamp(expires_raw) if expires_raw is not None else None
        structurally_valid = (
            "value" in record
            and isinstance(source, str)
            and bool(source.strip())
            and sensitivity in _KNOWN_SENSITIVITIES
            and isinstance(scope, Mapping)
            and confirmed_at is not None
            and (expires_raw is None or expires_at is not None)
        )
        if not structurally_valid:
            rejected.append(answer_key)
            invalid_verified.append(answer_key)
            continue
        if confirmed_at > active_now + timedelta(minutes=5):
            rejected.append(answer_key)
            invalid_verified.append(answer_key)
            continue
        if expires_at is not None and expires_at <= active_now:
            rejected.append(answer_key)
            invalid_verified.append(answer_key)
            continue
        if not _scope_matches(scope, job_id=job_id, tenant=tenant):
            rejected.append(answer_key)
            continue
        values[answer_key] = record["value"]
    return AnswerTrustReport(
        values=values,
        accepted_keys=tuple(sorted(values)),
        rejected_keys=tuple(sorted(rejected)),
        invalid_verified_keys=tuple(sorted(invalid_verified)),
    )


@dataclass(frozen=True, slots=True)
class CandidateVault:
    paths: PrivatePaths
    facts: Mapping[str, Any]
    answers: Mapping[str, Any]
    policy: Mapping[str, Any]

    @classmethod
    def load(cls, home: PrivateHome | None = None) -> "CandidateVault":
        private_home = home or PrivateHome.discover()
        paths = private_home.ensure()
        return cls(
            paths=paths,
            facts=_load_json(paths.profile_facts),
            answers=_load_json(paths.verified_answers),
            policy=_load_json(paths.policy),
        )

    @property
    def canonical_answers(self) -> dict[str, Any]:
        return dict(self.answer_trust_report().values)

    def answer_trust_report(
        self, *, job_id: str = "", tenant: str = ""
    ) -> AnswerTrustReport:
        return _answer_report(self.answers, job_id=job_id, tenant=tenant)

    @property
    def policy_config(self) -> PolicyConfig:
        autonomy = self.policy.get("autonomy", {})
        if not isinstance(autonomy, Mapping):
            autonomy = {}
        return PolicyConfig(
            mode=AutonomyMode(
                str(autonomy.get("mode") or AutonomyMode.LOW_RISK_AUTOPILOT.value)
            ),
            email_verification_agent_enabled=bool(
                autonomy.get("email_verification_agent_enabled", False)
            ),
            allow_keychain_login=bool(autonomy.get("allow_keychain_login", True)),
            allow_account_registration=bool(
                autonomy.get("allow_account_registration", True)
            ),
        )

    def application_profile(
        self,
        *,
        resume_path: str | Path | None = None,
        job_id: str = "",
        tenant: str = "",
    ) -> dict[str, Any]:
        """Return a compatibility mapping for deterministic Python adapters.

        No secret is loaded here.  Credentials remain behind the credential
        provider and only verified answer values are projected.
        """

        normalized = self.facts.get("normalized", {})
        if not isinstance(normalized, Mapping):
            raise ProfileStoreError("facts.json normalized must be an object")
        personal = dict(normalized.get("personal") or {})
        required = ("first_name", "last_name", "email")
        missing = [key for key in required if not str(personal.get(key) or "").strip()]
        if missing:
            raise ProfileStoreError(
                "private candidate facts are missing required normalized fields: "
                + ", ".join(missing)
            )

        answer_values = dict(
            self.answer_trust_report(job_id=job_id, tenant=tenant).values
        )
        common_keys = {
            "work_authorization": "authorized_to_work",
            "sponsorship": "require_sponsorship",
            "relocation": "willing_to_relocate",
            "salary": "salary_expectation",
            "start_date": "earliest_start_date",
            "gender": "gender",
            "race_ethnicity": "race_ethnicity",
            "veteran_status": "veteran_status",
            "disability_status": "disability_status",
        }
        common_answers = {
            legacy: answer_values[canonical]
            for canonical, legacy in common_keys.items()
            if canonical in answer_values
        }

        variants = list(normalized.get("resume_variants") or [])
        selected_resume = Path(resume_path).expanduser() if resume_path else None
        if selected_resume is None:
            default_resume = normalized.get("default_resume")
            selected_resume = Path(default_resume).expanduser() if default_resume else None

        browser = dict(normalized.get("browser") or {})
        browser.setdefault("preferred_handoff_browser", "safari")
        # Persisted browser state never follows a profile-provided path. Older
        # profiles may contain repository-local or otherwise shared locations.
        browser["user_data_dir"] = str(self.paths.chromium_profile)
        browser["chromium_user_data_dir"] = str(self.paths.chromium_profile)
        workday = dict(normalized.get("workday") or {})
        # Private profile preferences may further restrict autonomy, but they
        # can never widen the explicit policy boundary.
        workday["auto_login"] = bool(workday.get("auto_login", True)) and bool(
            self.policy_config.allow_keychain_login
        )
        workday["auto_register"] = bool(workday.get("auto_register", True)) and bool(
            self.policy_config.allow_account_registration
        )

        return {
            "personal": personal,
            "canonical_answers": {
                key: {"value": value, "source": "verified_private_vault"}
                for key, value in answer_values.items()
            },
            "common_answers": common_answers,
            "resume_path": str(selected_resume) if selected_resume else "",
            "resume_variants": variants,
            "preferences": dict(normalized.get("preferences") or {}),
            "browser": browser,
            "workday": workday,
            "ai": dict(
                normalized.get("ai")
                or {
                    "default_backend": "codex_cli",
                    "backends": {"codex_cli": {"timeout": 180}},
                    "components": {"form_analysis": "codex_cli"},
                }
            ),
            "auto_submission": {
                "enabled": True,
                "low_risk_only": self.policy_config.mode
                is AutonomyMode.LOW_RISK_AUTOPILOT,
                "allow_ai_custom_answers": False,
                "require_explicit_confirmation_evidence": True,
            },
            "private_home": str(self.paths.root),
        }


def _candidate_fact_timestamp(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise CandidateSummaryProviderError(
            f"prioritization fact {field} must be an aware timestamp"
        )
    return parsed


def _candidate_fact_from_record(value: Any) -> CandidateFact:
    expected = {
        "fact_id",
        "category",
        "statement",
        "source",
        "verified",
        "prioritization_safe",
        "scope",
        "confirmed_at",
        "expires_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise CandidateSummaryProviderError(
            "prioritization fact does not match CandidateFact"
        )
    scope = value["scope"]
    if scope not in {None, "global"}:
        raise CandidateSummaryProviderError(
            "current CandidateSummary accepts only global facts"
        )
    try:
        return CandidateFact(
            fact_id=value["fact_id"],
            category=value["category"],
            statement=value["statement"],
            source=value["source"],
            verified=value["verified"],
            prioritization_safe=value["prioritization_safe"],
            scope=scope,
            confirmed_at=_candidate_fact_timestamp(
                value["confirmed_at"],
                field="confirmed_at",
            ),
            expires_at=_candidate_fact_timestamp(
                value["expires_at"],
                field="expires_at",
            ),
        )
    except (TypeError, ValueError) as exc:
        raise CandidateSummaryProviderError(
            "prioritization fact is invalid"
        ) from exc


class PrivateHomeCandidateSummaryProvider:
    """Project explicitly trusted CandidateFacts from the current CandidateVault."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def get_current(
        self,
        subject_id: str,
        *,
        now: datetime,
    ) -> CandidateSummary:
        if (
            not isinstance(subject_id, str)
            or not subject_id.strip()
            or len(subject_id.strip()) > 160
        ):
            raise CandidateSummaryProviderError(
                "subject_id is outside the CandidateSummary contract"
            )
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise CandidateSummaryProviderError(
                "CandidateSummary projection time must be timezone-aware"
            )
        try:
            vault = CandidateVault.load(self._home)
        except ProfileStoreError as exc:
            raise CandidateSummaryProviderError(
                "CandidateVault is unavailable"
            ) from exc
        if vault.facts.get("subject_id") != subject_id.strip():
            raise CandidateSummaryProviderError(
                "CandidateVault subject does not match the requested subject"
            )
        records = vault.facts.get("prioritization_facts")
        if not isinstance(records, list):
            raise CandidateSummaryProviderError(
                "CandidateVault has no typed prioritization facts"
            )
        facts = tuple(_candidate_fact_from_record(item) for item in records)
        provisional = build_candidate_summary(
            subject_id=subject_id.strip(),
            candidate_summary_version="candidate-summary-current",
            facts=facts,
            created_at=now,
        )
        return CandidateSummary(
            subject_id=provisional.subject_id,
            candidate_summary_version=(
                "candidate-summary-"
                f"{provisional.candidate_summary_content_hash[:24]}"
            ),
            candidate_summary_content_hash=(
                provisional.candidate_summary_content_hash
            ),
            facts=provisional.facts,
            created_at=provisional.created_at,
        )


__all__ = [
    "AnswerTrustReport",
    "CandidateSummaryProvider",
    "CandidateSummaryProviderError",
    "CandidateVault",
    "PrivateHomeCandidateSummaryProvider",
    "ProfileStoreError",
    "VAULT_SCHEMA_VERSION",
]
