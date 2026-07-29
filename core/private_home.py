"""Private, repository-external storage for Jobops user data.

The public repository contains schemas and synthetic fixtures only.  Real profile
facts, application materials, browser state, queues, logs, and evidence live below
``JOBOPS_HOME``.  Passwords and access tokens do not belong here; they are stored
through the platform credential provider (macOS Keychain by default).
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


JOBOPS_HOME_ENV = "JOBOPS_HOME"
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
PRIVATE_HOME_MARKER = ".jobops-private-home"
PRIVATE_HOME_MARKER_CONTENT = "jobops-private-home-v1\n"

_LEGACY_PRIVATE_HOME_ENTRIES = frozenset(
    {
        "profile",
        "queue",
        "documents",
        "state",
        "browser",
        "cache",
        "evidence",
        "logs",
    }
)


class PrivateHomeError(RuntimeError):
    """Raised when a requested private path is unsafe."""


def default_private_home(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Resolve the configured private home without creating it."""

    env = os.environ if environ is None else environ
    override = env.get(JOBOPS_HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()

    user_home = (home or Path.home()).expanduser()
    active_platform = platform or sys.platform
    if active_platform == "darwin":
        return (user_home / "Library" / "Application Support" / "Jobops").resolve()

    xdg_data = env.get("XDG_DATA_HOME")
    if xdg_data:
        return (Path(xdg_data).expanduser() / "jobops").resolve()
    return (user_home / ".local" / "share" / "jobops").resolve()


def _ensure_private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise PrivateHomeError(f"private directory cannot be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if not path.is_dir():
        raise PrivateHomeError(f"private path is not a directory: {path}")
    path.chmod(PRIVATE_DIRECTORY_MODE)
    return path


def _owned_by_current_user(path: Path) -> bool:
    """Return whether ``path`` is owned by this process' effective user."""

    getuid = getattr(os, "geteuid", None)
    return getuid is None or path.stat(follow_symlinks=False).st_uid == getuid()


def _write_ownership_marker(root: Path) -> None:
    marker = root / PRIVATE_HOME_MARKER
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(marker, flags, PRIVATE_FILE_MODE)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        os.write(descriptor, PRIVATE_HOME_MARKER_CONTENT.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_or_claim_private_root(root: Path) -> None:
    """Claim a new/empty root or validate an explicitly owned Jobops root.

    This prevents a typo such as ``JOBOPS_HOME=~/Documents`` from chmodding and
    populating an unrelated directory.  A secure, recognizable pre-marker
    Jobops layout is adopted once for compatibility with early local builds.
    """

    if root.is_symlink():
        raise PrivateHomeError(f"JOBOPS_HOME cannot be a symlink: {root}")
    if not root.exists():
        root.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE)
    if not root.is_dir():
        raise PrivateHomeError(f"JOBOPS_HOME is not a directory: {root}")
    if not _owned_by_current_user(root):
        raise PrivateHomeError("JOBOPS_HOME must be owned by the current user")

    marker = root / PRIVATE_HOME_MARKER
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_file():
            raise PrivateHomeError("JOBOPS_HOME ownership marker is unsafe")
        if not _owned_by_current_user(marker):
            raise PrivateHomeError("JOBOPS_HOME ownership marker has the wrong owner")
        try:
            content = marker.read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            raise PrivateHomeError("JOBOPS_HOME ownership marker is unreadable") from exc
        if content != PRIVATE_HOME_MARKER_CONTENT:
            raise PrivateHomeError("JOBOPS_HOME ownership marker is invalid")
        marker.chmod(PRIVATE_FILE_MODE)
        root.chmod(PRIVATE_DIRECTORY_MODE)
        return

    entries = tuple(root.iterdir())
    if entries:
        names = {entry.name for entry in entries}
        mode = root.stat(follow_symlinks=False).st_mode & 0o777
        recognized_legacy_layout = (
            names <= _LEGACY_PRIVATE_HOME_ENTRIES
            and {"profile", "state", "browser"} <= names
            and mode & 0o077 == 0
            and all(not entry.is_symlink() for entry in entries)
        )
        if not recognized_legacy_layout:
            raise PrivateHomeError(
                "existing JOBOPS_HOME is not an owned Jobops directory; choose a "
                "new empty directory instead"
            )

    root.chmod(PRIVATE_DIRECTORY_MODE)
    try:
        _write_ownership_marker(root)
    except FileExistsError:
        # A concurrent initializer won the race. Validate its marker rather
        # than overwriting it.
        _validate_or_claim_private_root(root)


def _containing_git_worktree(path: Path) -> Path | None:
    """Return the nearest Git worktree containing ``path``, if any."""

    candidate = path.expanduser().resolve()
    for ancestor in (candidate, *candidate.parents):
        marker = ancestor / ".git"
        if marker.is_dir() or marker.is_file():
            return ancestor
    return None


def containing_git_worktree(path: Path) -> Path | None:
    """Public, read-only check used before importing private source trees."""

    return _containing_git_worktree(path)


@dataclass(frozen=True, slots=True)
class PrivatePaths:
    root: Path
    profile: Path
    queue: Path
    documents: Path
    master_documents: Path
    generated_documents: Path
    state: Path
    browser: Path
    chromium_profile: Path
    cache: Path
    private_recipes: Path
    evidence: Path
    logs: Path
    profile_facts: Path
    verified_answers: Path
    policy: Path
    job_queue: Path
    event_ledger: Path
    intake: Path
    accepted_job_intents: Path
    discovery: Path
    job_postings: Path
    discovery_runs: Path
    prioritization: Path
    prioritization_policies: Path
    priority_decisions: Path
    preparation: Path
    application_plans: Path
    resume_candidates: Path
    resume_candidate_records: Path
    resume_candidate_artifacts: Path
    resume_selection_decisions: Path
    source_resume_projections: Path
    candidate_evidence_snapshots: Path
    tailored_resume_drafts: Path
    resume_fact_qa_results: Path
    resume_latex_versions: Path
    resume_latex_version_records: Path
    resume_latex_version_sources: Path
    base_latex_selections: Path


@dataclass(frozen=True, slots=True)
class PrivateHome:
    """Named paths and secure file helpers rooted outside the Git checkout."""

    root: Path

    @classmethod
    def discover(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
        platform: str | None = None,
    ) -> "PrivateHome":
        return cls(default_private_home(environ=environ, home=home, platform=platform))

    @property
    def paths(self) -> PrivatePaths:
        root = self.root.expanduser().resolve()
        profile = root / "profile"
        queue = root / "queue"
        documents = root / "documents"
        state = root / "state"
        browser = root / "browser"
        cache = root / "cache"
        return PrivatePaths(
            root=root,
            profile=profile,
            queue=queue,
            documents=documents,
            master_documents=documents / "master",
            generated_documents=documents / "generated",
            state=state,
            browser=browser,
            chromium_profile=browser / "chromium",
            cache=cache,
            private_recipes=cache / "private-recipes",
            evidence=root / "evidence",
            logs=root / "logs",
            profile_facts=profile / "facts.json",
            verified_answers=profile / "verified-answers.json",
            policy=profile / "policy.json",
            job_queue=queue / "job_pool.csv",
            event_ledger=state / "events.sqlite3",
            intake=state / "intake",
            accepted_job_intents=state / "intake" / "accepted-job-intents",
            discovery=state / "discovery",
            job_postings=state / "discovery" / "job-postings",
            discovery_runs=state / "discovery" / "runs",
            prioritization=state / "prioritization",
            prioritization_policies=state / "prioritization" / "policies",
            priority_decisions=state / "prioritization" / "decisions",
            preparation=state / "preparation",
            application_plans=state / "preparation" / "application-plans",
            resume_candidates=state / "preparation" / "resume-candidates",
            resume_candidate_records=(
                state / "preparation" / "resume-candidates" / "records"
            ),
            resume_candidate_artifacts=(
                state / "preparation" / "resume-candidates" / "artifacts"
            ),
            resume_selection_decisions=(
                state / "preparation" / "resume-selections"
            ),
            source_resume_projections=(
                state / "preparation" / "source-resume-projections"
            ),
            candidate_evidence_snapshots=(
                state / "preparation" / "candidate-evidence-snapshots"
            ),
            tailored_resume_drafts=(
                state / "preparation" / "tailored-resume-drafts"
            ),
            resume_fact_qa_results=(
                state / "preparation" / "resume-fact-qa-results"
            ),
            resume_latex_versions=(
                state / "preparation" / "resume-latex-versions"
            ),
            resume_latex_version_records=(
                state / "preparation" / "resume-latex-versions" / "records"
            ),
            resume_latex_version_sources=(
                state / "preparation" / "resume-latex-versions" / "sources"
            ),
            base_latex_selections=(
                state / "preparation" / "base-latex-selections"
            ),
        )

    def ensure(self) -> PrivatePaths:
        if self.root.expanduser().is_symlink():
            raise PrivateHomeError(
                f"JOBOPS_HOME cannot be a symlink: {self.root.expanduser()}"
            )
        paths = self.paths
        worktree = _containing_git_worktree(paths.root)
        if worktree is not None:
            raise PrivateHomeError(
                "JOBOPS_HOME must be outside every Git worktree; refusing to place "
                f"private candidate data under {worktree}"
            )
        _validate_or_claim_private_root(paths.root)
        for directory in (
            paths.profile,
            paths.queue,
            paths.documents,
            paths.master_documents,
            paths.generated_documents,
            paths.state,
            paths.intake,
            paths.accepted_job_intents,
            paths.discovery,
            paths.job_postings,
            paths.discovery_runs,
            paths.prioritization,
            paths.prioritization_policies,
            paths.priority_decisions,
            paths.preparation,
            paths.application_plans,
            paths.resume_candidates,
            paths.resume_candidate_records,
            paths.resume_candidate_artifacts,
            paths.resume_selection_decisions,
            paths.source_resume_projections,
            paths.candidate_evidence_snapshots,
            paths.tailored_resume_drafts,
            paths.resume_fact_qa_results,
            paths.resume_latex_versions,
            paths.resume_latex_version_records,
            paths.resume_latex_version_sources,
            paths.base_latex_selections,
            paths.browser,
            paths.chromium_profile,
            paths.cache,
            paths.private_recipes,
            paths.evidence,
            paths.logs,
        ):
            _ensure_private_directory(directory)
        return paths

    def contained_path(self, path: str | Path) -> Path:
        """Resolve a path and reject attempts to escape the private home."""

        root = self.paths.root
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = Path(os.path.abspath(candidate))
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise PrivateHomeError(f"path escapes JOBOPS_HOME: {candidate}") from exc
        cursor = root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise PrivateHomeError(
                    f"private path cannot contain a symlink: {cursor}"
                )
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PrivateHomeError(f"path escapes JOBOPS_HOME: {resolved}") from exc
        return resolved

    def ensure_private_file(self, path: str | Path) -> Path:
        """Create a private file when absent and enforce mode 0600."""

        candidate = self.contained_path(path)
        _ensure_private_directory(candidate.parent)
        if candidate.is_symlink():
            raise PrivateHomeError(f"private file cannot be a symlink: {candidate}")
        descriptor = os.open(candidate, os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE)
        try:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)
        return candidate

    def write_bytes(self, path: str | Path, content: bytes) -> Path:
        """Atomically replace a private file with mode 0600."""

        candidate = self.contained_path(path)
        _ensure_private_directory(candidate.parent)
        if candidate.is_symlink():
            raise PrivateHomeError(f"private file cannot be a symlink: {candidate}")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{candidate.name}.", dir=candidate.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, candidate)
            candidate.chmod(PRIVATE_FILE_MODE)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return candidate

    def write_bytes_if_absent(
        self,
        path: str | Path,
        content: bytes,
    ) -> bool:
        """Create one private file atomically, returning false if it exists."""

        candidate = self.contained_path(path)
        _ensure_private_directory(candidate.parent)
        if candidate.is_symlink():
            raise PrivateHomeError(f"private file cannot be a symlink: {candidate}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(candidate, flags, PRIVATE_FILE_MODE)
        except FileExistsError:
            return False
        try:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            candidate.unlink(missing_ok=True)
            raise
        return True

    def write_text(
        self, path: str | Path, content: str, *, encoding: str = "utf-8"
    ) -> Path:
        return self.write_bytes(path, content.encode(encoding))


__all__ = [
    "JOBOPS_HOME_ENV",
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "PRIVATE_HOME_MARKER",
    "PrivateHome",
    "PrivateHomeError",
    "PrivatePaths",
    "containing_git_worktree",
    "default_private_home",
]
