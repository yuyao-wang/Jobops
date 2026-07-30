"""
JobOps Dashboard — FastAPI server with REST API and WebSocket support.
Serves a local web dashboard at http://localhost:8080.

Architecture notes:
- REST endpoints handle all CRUD operations against the SQLite tracker.
- A single /ws WebSocket endpoint fans out EventBus events to every browser tab
  that is currently open, enabling live updates without polling.
- Background tasks (discover, rescore, score-all) are run via asyncio.create_task
  so the HTTP response returns immediately while work continues.
"""

import asyncio
import base64
import copy
import inspect
import json
import ipaddress
import logging
import shutil
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Optional

logger = logging.getLogger("dashboard.server")

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from utils.tracker import (
    get_all_jobs,
    get_job_by_id,
    update_job_status,
    update_job_notes,
    delete_job,
    get_stats,
    get_timeline_stats,
    get_score_distribution,
    get_companies,
    log_matched,
    log_skipped,
    get_unscored_jobs,
    VALID_STATUSES,
    ignore_jobs,
    purge_all,
    purge_everything,
    get_ignored_count,
)
from utils.events import EventBus
from core.authenticated_subject import AuthenticatedSubjectContext
from dashboard.authentication import AuthenticatedSubjectDependency
from dashboard.automation_cycle import (
    ContinueAutomationUICommand,
    ContinueAutomationUIController,
)
from dashboard.job_library_refresh import (
    RefreshJobLibraryUICommand,
    RefreshJobLibraryUIController,
)
from dashboard.human_attention_inbox import (
    HumanAttentionInboxUIController,
)
from dashboard.read_models import (
    DashboardApplicationsController,
    DashboardJobsController,
    DashboardOverviewController,
    DashboardProfileController,
)
from dashboard.material_correction_target import (
    MaterialCorrectionTargetUIController,
)
from dashboard.input_replacement_target import (
    InputReplacementTargetUIController,
)
from dashboard.input_replacement_resolution import (
    InputReplacementResolutionUIController,
)
from dashboard.new_resume_candidate_replacement import (
    NewResumeCandidateReplacementUIController,
)
from dashboard.new_base_latex_version_replacement import (
    NewBaseLatexVersionReplacementUIController,
)
from dashboard.resume_layout_correction_preview import (
    ResumeLayoutCorrectionPreviewUIController,
)
from dashboard.cover_letter_overflow_preview import (
    CoverLetterOverflowPreviewUIController,
)
from dashboard.cover_letter_overflow_correction import (
    CoverLetterOverflowCorrectionUICommand,
    CoverLetterOverflowCorrectionUIController,
)
from dashboard.resume_layout_correction import (
    ResumeLayoutCorrectionUIController,
)
from dashboard.unsupported_claim_correction import (
    UnsupportedClaimCorrectionUIController,
)
from dashboard.latex_compilation_correction import (
    LatexCompilationCorrectionUIController,
)
from core.unsupported_claim_correction import (
    UnsupportedClaimCorrectionAction,
)
from core.latex_compilation_correction import (
    LatexCompilationCorrectionAction,
)
from core.resume_layout_correction import (
    ResumeLayoutCorrectionAction,
    ResumeLayoutVisualIssue,
)
from core.cover_letter_overflow_correction import (
    CoverLetterOverflowCorrectionAction,
)
from core.input_replacement_resolution import InputReplacementAction
from core.new_resume_candidate_replacement import (
    NEW_RESUME_UPLOAD_MAX_BYTES,
)
from core.new_base_latex_version_replacement import (
    NEW_BASE_LATEX_UPLOAD_MAX_BYTES,
)
from dashboard.application_answer_resolution import (
    ApplicationAnswerResolutionUIController,
)
from dashboard.version_choice_resolution import (
    VersionChoiceResolutionUIController,
)
from dashboard.candidate_fact_reviews import CandidateFactReviewUIController
from core.candidate_fact_reviews import CandidateFactReviewAction

BASE_DIR = Path(__file__).parent
RESUMES_DIR = BASE_DIR.parent / "resumes"
RESUMES_META = RESUMES_DIR / "meta.json"


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """Own explicitly injected production resources; never start a Scheduler."""

    resources = tuple(
        getattr(app.state, "production_owned_resources", ())
    )
    try:
        for resource in resources:
            start = getattr(resource, "start", None)
            if start is None:
                continue
            result = start()
            if inspect.isawaitable(result):
                await result
        yield
    finally:
        for resource in reversed(resources):
            close = getattr(resource, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("production resource cleanup failed")

app = FastAPI(title="JobOps", version="1.0.0", lifespan=lifespan)


def configure_job_library_refresh_ui(
    *,
    controller: RefreshJobLibraryUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3b UI boundary from the composition root."""

    if not isinstance(controller, RefreshJobLibraryUIController):
        raise TypeError("controller must be a RefreshJobLibraryUIController")
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.job_library_refresh_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_automation_cycle_ui(
    *,
    controller: ContinueAutomationUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated P2c10a UI boundary."""

    if not isinstance(controller, ContinueAutomationUIController):
        raise TypeError("controller must be a ContinueAutomationUIController")
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.automation_cycle_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_production_automation_ui(
    *,
    application: FastAPI,
    refresh_controller: RefreshJobLibraryUIController,
    automation_controller: ContinueAutomationUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
    owned_resources: tuple[object, ...],
    composition_diagnostics: Mapping[str, Any],
    human_attention_controller: HumanAttentionInboxUIController | None = None,
    dashboard_profile_controller: DashboardProfileController | None = None,
    dashboard_jobs_controller: DashboardJobsController | None = None,
    dashboard_applications_controller: (
        DashboardApplicationsController | None
    ) = None,
    dashboard_overview_controller: DashboardOverviewController | None = None,
) -> None:
    """Atomically install the complete P2c10c production UI boundary."""

    if not isinstance(application, FastAPI):
        raise TypeError("application must be FastAPI")
    if not isinstance(refresh_controller, RefreshJobLibraryUIController):
        raise TypeError("refresh controller is invalid")
    if not isinstance(automation_controller, ContinueAutomationUIController):
        raise TypeError("automation controller is invalid")
    if not callable(authenticated_subject):
        raise TypeError("authenticated subject dependency is invalid")
    if not isinstance(owned_resources, tuple) or any(
        resource is None for resource in owned_resources
    ):
        raise TypeError("owned resources are invalid")
    if not isinstance(composition_diagnostics, Mapping):
        raise TypeError("composition diagnostics are invalid")
    optional_controllers = (
        (human_attention_controller, HumanAttentionInboxUIController),
        (dashboard_profile_controller, DashboardProfileController),
        (dashboard_jobs_controller, DashboardJobsController),
        (dashboard_applications_controller, DashboardApplicationsController),
        (dashboard_overview_controller, DashboardOverviewController),
    )
    if any(
        controller is not None and not isinstance(controller, expected)
        for controller, expected in optional_controllers
    ):
        raise TypeError("dashboard read controller is invalid")
    application.state.job_library_refresh_controller = refresh_controller
    application.state.automation_cycle_controller = automation_controller
    application.state.authenticated_subject_dependency = authenticated_subject
    application.state.human_attention_inbox_controller = (
        human_attention_controller
    )
    application.state.dashboard_profile_controller = (
        dashboard_profile_controller
    )
    application.state.dashboard_jobs_controller = dashboard_jobs_controller
    application.state.dashboard_applications_controller = (
        dashboard_applications_controller
    )
    application.state.dashboard_overview_controller = (
        dashboard_overview_controller
    )
    application.state.production_owned_resources = owned_resources
    application.state.production_composition_diagnostics = dict(
        composition_diagnostics
    )


def configure_human_attention_inbox_ui(
    *,
    controller: HumanAttentionInboxUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated, read-only P2b5 Inbox boundary."""

    if not isinstance(controller, HumanAttentionInboxUIController):
        raise TypeError(
            "controller must be a HumanAttentionInboxUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.human_attention_inbox_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_candidate_fact_review_ui(
    *,
    controller: CandidateFactReviewUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated C1d Candidate Fact review boundary."""

    if not isinstance(controller, CandidateFactReviewUIController):
        raise TypeError(
            "controller must be a CandidateFactReviewUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.candidate_fact_review_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_material_correction_target_ui(
    *,
    controller: MaterialCorrectionTargetUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated, read-only P2b5c target boundary."""

    if not isinstance(controller, MaterialCorrectionTargetUIController):
        raise TypeError(
            "controller must be a MaterialCorrectionTargetUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.material_correction_target_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_input_replacement_target_ui(
    *,
    controller: InputReplacementTargetUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated, read-only P2b5f target boundary."""

    if not isinstance(controller, InputReplacementTargetUIController):
        raise TypeError(
            "controller must be an InputReplacementTargetUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.input_replacement_target_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_input_replacement_resolution_ui(
    *,
    controller: InputReplacementResolutionUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3g5 existing-input replacement boundary."""

    if not isinstance(controller, InputReplacementResolutionUIController):
        raise TypeError(
            "controller must be an InputReplacementResolutionUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.input_replacement_resolution_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_new_resume_candidate_replacement_ui(
    *,
    controller: NewResumeCandidateReplacementUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3g5b1 upload-and-replace boundary."""

    if not isinstance(
        controller, NewResumeCandidateReplacementUIController
    ):
        raise TypeError(
            "controller must be a NewResumeCandidateReplacementUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.new_resume_candidate_replacement_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_new_base_latex_version_replacement_ui(
    *,
    controller: NewBaseLatexVersionReplacementUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3g5b2 upload-and-replace boundary."""

    if not isinstance(
        controller, NewBaseLatexVersionReplacementUIController
    ):
        raise TypeError(
            "controller must be a "
            "NewBaseLatexVersionReplacementUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.new_base_latex_version_replacement_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_resume_layout_correction_preview_ui(
    *,
    controller: ResumeLayoutCorrectionPreviewUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated, read-only P2b5e1 preview boundary."""

    if not isinstance(
        controller, ResumeLayoutCorrectionPreviewUIController
    ):
        raise TypeError(
            "controller must be a ResumeLayoutCorrectionPreviewUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.resume_layout_correction_preview_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_cover_letter_overflow_preview_ui(
    *,
    controller: CoverLetterOverflowPreviewUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated, read-only P2b5e2 preview boundary."""

    if not isinstance(controller, CoverLetterOverflowPreviewUIController):
        raise TypeError(
            "controller must be a CoverLetterOverflowPreviewUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.cover_letter_overflow_preview_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_cover_letter_overflow_correction_ui(
    *,
    controller: CoverLetterOverflowCorrectionUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3g4d correction boundary."""

    if not isinstance(controller, CoverLetterOverflowCorrectionUIController):
        raise TypeError(
            "controller must be a CoverLetterOverflowCorrectionUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.cover_letter_overflow_correction_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_resume_layout_correction_ui(
    *,
    controller: ResumeLayoutCorrectionUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3g4c correction boundary."""

    if not isinstance(controller, ResumeLayoutCorrectionUIController):
        raise TypeError(
            "controller must be a ResumeLayoutCorrectionUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.resume_layout_correction_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_unsupported_claim_correction_ui(
    *,
    controller: UnsupportedClaimCorrectionUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3g4a typed correction boundary."""

    if not isinstance(controller, UnsupportedClaimCorrectionUIController):
        raise TypeError(
            "controller must be an UnsupportedClaimCorrectionUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.unsupported_claim_correction_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_latex_compilation_correction_ui(
    *,
    controller: LatexCompilationCorrectionUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3g4b correction boundary."""

    if not isinstance(controller, LatexCompilationCorrectionUIController):
        raise TypeError(
            "controller must be a LatexCompilationCorrectionUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.latex_compilation_correction_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_application_answer_resolution_ui(
    *,
    controller: ApplicationAnswerResolutionUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3g1 conversational boundary."""

    if not isinstance(controller, ApplicationAnswerResolutionUIController):
        raise TypeError(
            "controller must be an ApplicationAnswerResolutionUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.application_answer_resolution_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


def configure_version_choice_resolution_ui(
    *,
    controller: VersionChoiceResolutionUIController,
    authenticated_subject: AuthenticatedSubjectDependency,
) -> None:
    """Inject the authenticated S3g2 choice-resolution boundary."""

    if not isinstance(controller, VersionChoiceResolutionUIController):
        raise TypeError(
            "controller must be a VersionChoiceResolutionUIController"
        )
    if not callable(authenticated_subject):
        raise TypeError("authenticated_subject must be callable")
    app.state.version_choice_resolution_controller = controller
    app.state.authenticated_subject_dependency = authenticated_subject


async def _authenticated_dashboard_subject(
    request: Request,
) -> AuthenticatedSubjectContext:
    dependency = getattr(
        request.app.state, "authenticated_subject_dependency", None
    )
    if dependency is None:
        raise HTTPException(
            status_code=503, detail="Authenticated session is unavailable."
        )
    return await dependency(request)


@app.get("/api/health")
async def health() -> dict:
    """Health check for Docker and monitoring."""
    configured = all(
        getattr(app.state, name, None) is not None
        for name in (
            "job_library_refresh_controller",
            "automation_cycle_controller",
            "authenticated_subject_dependency",
        )
    )
    return {"status": "ok" if configured else "not_ready"}


# ---------------------------------------------------------------------------
# Static files and Jinja2 templates
# ---------------------------------------------------------------------------
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------------------------
# WebSocket connection registry
# ---------------------------------------------------------------------------
ws_clients: list[WebSocket] = []


def _json_safe(obj):
    """Make an object JSON-serializable (datetimes, sets, etc.)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if hasattr(obj, "__dict__"):
        return str(obj)
    return str(obj)


async def broadcast_event(event: dict) -> None:
    """
    Send a JSON event to every connected WebSocket client.

    Dead connections are collected and removed after each broadcast
    to avoid growing the registry with stale entries.
    """
    # Pre-serialize to catch any JSON issues before sending
    try:
        payload = json.dumps(event, default=_json_safe)
    except (TypeError, ValueError) as exc:
        logger.error("broadcast_event: JSON serialization failed: %s", exc)
        # Send a simplified error event instead
        payload = json.dumps({
            "type": event.get("type", "unknown"),
            "data": {"error": f"Serialization error: {exc}"},
        })

    dead: list[WebSocket] = []
    for ws in ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_clients:
            ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# Bridge EventBus (sync) -> WebSocket broadcast (async)
# ---------------------------------------------------------------------------
def _on_event(event: dict) -> None:
    """
    Callback registered with EventBus.

    EventBus.emit() is called from synchronous tracker functions, so we must
    schedule the coroutine on whatever event loop is currently running.
    asyncio.ensure_future is safe to call from sync code that runs within an
    async context (i.e. while uvicorn's loop is active).
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(broadcast_event(event))
    except RuntimeError:
        pass  # No event loop — running outside server context, skip


EventBus.subscribe(_on_event)


# ===========================================================================
# HTML Route
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Serve the single-page dashboard."""
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/job-library/refresh")
async def refresh_job_library_ui(
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Run exactly one authenticated S3b refresh invocation."""

    controller = getattr(
        request.app.state, "job_library_refresh_controller", None
    )
    if not isinstance(controller, RefreshJobLibraryUIController):
        raise HTTPException(
            status_code=503, detail="Job library refresh is unavailable."
        )
    try:
        command = RefreshJobLibraryUICommand(
            invocation_id=body.get("invocation_id", ""),
            max_reprioritizations=body.get("max_reprioritizations"),
        )
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid refresh request."
        ) from None
    result = await controller.refresh(context=context, command=command)
    return result.to_dict()


@app.post("/api/automation-cycle/run")
async def continue_automatic_application_ui(
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Run exactly one authenticated P2c10a invocation."""

    controller = getattr(
        request.app.state, "automation_cycle_controller", None
    )
    if not isinstance(controller, ContinueAutomationUIController):
        raise HTTPException(
            status_code=503, detail="Automatic application is unavailable."
        )
    try:
        command = ContinueAutomationUICommand(
            invocation_id=body.get("invocation_id", "")
        )
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid automation request."
        ) from None
    result = await controller.run(context=context, command=command)
    return result.to_dict()


@app.get("/api/human-attention-inbox")
async def human_attention_inbox_ui(
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Read one current authenticated P2b5 snapshot without writes."""

    controller = getattr(
        request.app.state, "human_attention_inbox_controller", None
    )
    if not isinstance(controller, HumanAttentionInboxUIController):
        raise HTTPException(
            status_code=503, detail="Human attention inbox is unavailable."
        )
    result = await controller.load(context=context)
    return result.to_dict()


async def _load_dashboard_read(
    request: Request,
    context: AuthenticatedSubjectContext,
    *,
    state_name: str,
    controller_type: type,
    unavailable_message: str,
) -> dict:
    controller = getattr(request.app.state, state_name, None)
    if not isinstance(controller, controller_type):
        raise HTTPException(status_code=503, detail=unavailable_message)
    result = await controller.load(context=context)
    return result.to_public_dict()


@app.get("/api/dashboard/profile")
async def dashboard_profile_read_ui(
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    return await _load_dashboard_read(
        request,
        context,
        state_name="dashboard_profile_controller",
        controller_type=DashboardProfileController,
        unavailable_message="Dashboard profile is unavailable.",
    )


@app.get("/api/dashboard/jobs")
async def dashboard_jobs_read_ui(
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    return await _load_dashboard_read(
        request,
        context,
        state_name="dashboard_jobs_controller",
        controller_type=DashboardJobsController,
        unavailable_message="Dashboard jobs are unavailable.",
    )


@app.get("/api/dashboard/applications")
@app.get("/api/applications")
async def dashboard_applications_read_ui(
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    return await _load_dashboard_read(
        request,
        context,
        state_name="dashboard_applications_controller",
        controller_type=DashboardApplicationsController,
        unavailable_message="Dashboard applications are unavailable.",
    )


@app.get("/api/dashboard/overview")
async def dashboard_overview_read_ui(
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    return await _load_dashboard_read(
        request,
        context,
        state_name="dashboard_overview_controller",
        controller_type=DashboardOverviewController,
        unavailable_message="Dashboard overview is unavailable.",
    )


@app.get("/api/candidate-facts/review")
async def candidate_fact_review_queue_ui(
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Read the authenticated subject's bounded Candidate Fact review queue."""

    controller = getattr(
        request.app.state, "candidate_fact_review_controller", None
    )
    if not isinstance(controller, CandidateFactReviewUIController):
        raise HTTPException(
            status_code=503, detail="Candidate fact review is unavailable."
        )
    return controller.load(context=context).to_dict()


@app.get("/api/candidate-facts/review/{review_item_id}")
async def candidate_fact_review_item_ui(
    review_item_id: str,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Read one exact authenticated Candidate Fact review item."""

    controller = getattr(
        request.app.state, "candidate_fact_review_controller", None
    )
    if not isinstance(controller, CandidateFactReviewUIController):
        raise HTTPException(
            status_code=503, detail="Candidate fact review is unavailable."
        )
    result = controller.load(context=context).to_dict()
    item = next(
        (
            value
            for value in result["items"]
            if value["review_item_id"] == review_item_id
        ),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Review item not found.")
    return {
        "item": item,
        "queue_snapshot_hash": result["queue_snapshot_hash"],
        "status": result["status"],
    }


@app.post("/api/candidate-facts/review/{review_item_id}/resolve")
async def resolve_candidate_fact_review_ui(
    review_item_id: str,
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Resolve one Candidate Fact review with an explicit typed action."""

    controller = getattr(
        request.app.state, "candidate_fact_review_controller", None
    )
    if not isinstance(controller, CandidateFactReviewUIController):
        raise HTTPException(
            status_code=503, detail="Candidate fact review is unavailable."
        )
    if "subject_id" in body or any(
        key in body
        for key in (
            "proposal_hash",
            "current_fact_id",
            "source_hash",
            "verification_status",
        )
    ):
        raise HTTPException(status_code=422, detail="Invalid review request.")
    try:
        action = CandidateFactReviewAction(body.get("action", ""))
        result = controller.resolve(
            context=context,
            review_item_id=review_item_id,
            queue_snapshot_hash=body.get("queue_snapshot_hash", ""),
            action=action,
            invocation_id=body.get("invocation_id", ""),
            submitted_value=body.get("value"),
        )
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid review request."
        ) from None
    return result.to_dict()


@app.get(
    "/api/candidate-facts/review/{review_item_id}/assets/{evidence_id}"
)
async def candidate_fact_review_asset_ui(
    review_item_id: str,
    evidence_id: str,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> Response:
    """Return one bounded managed preview asset without revealing its path."""

    controller = getattr(
        request.app.state, "candidate_fact_review_controller", None
    )
    if not isinstance(controller, CandidateFactReviewUIController):
        raise HTTPException(
            status_code=503, detail="Candidate fact review is unavailable."
        )
    payload = controller.read_asset(
        context=context,
        review_item_id=review_item_id,
        evidence_id=evidence_id,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="Preview not found.")
    return Response(
        content=payload.content,
        media_type=payload.asset.media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/human-attention-inbox/{attention_item_id}/resolve")
async def resolve_application_answer_ui(
    attention_item_id: str,
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Resolve one current USER application-answer item."""

    controller = getattr(
        request.app.state,
        "application_answer_resolution_controller",
        None,
    )
    if not isinstance(
        controller, ApplicationAnswerResolutionUIController
    ):
        raise HTTPException(
            status_code=503,
            detail="Application answer resolution is unavailable.",
        )
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(
            status_code=422, detail="A clear response is required."
        )
    result = await controller.resolve(
        context=context,
        attention_item_id=attention_item_id,
        user_message=message,
    )
    return result.to_dict()


@app.get(
    "/api/human-attention-inbox/{attention_item_id}/correction-target"
)
async def material_correction_target_ui(
    attention_item_id: str,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Read one current correction target without exposing private identity."""

    controller = getattr(
        request.app.state, "material_correction_target_controller", None
    )
    if not isinstance(controller, MaterialCorrectionTargetUIController):
        raise HTTPException(
            status_code=503,
            detail="Material correction target is unavailable.",
        )
    try:
        result = await controller.get(
            context=context, attention_item_id=attention_item_id
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid correction target request."
        ) from None
    return result.to_dict()


@app.get(
    "/api/human-attention-inbox/{attention_item_id}/replacement-target"
)
async def input_replacement_target_ui(
    attention_item_id: str,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Read one current replacement target without exposing private identity."""

    controller = getattr(
        request.app.state, "input_replacement_target_controller", None
    )
    if not isinstance(controller, InputReplacementTargetUIController):
        raise HTTPException(
            status_code=503,
            detail="Input replacement target is unavailable.",
        )
    try:
        result = await controller.get(
            context=context, attention_item_id=attention_item_id
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid replacement target request."
        ) from None
    return result.to_dict()


@app.get(
    "/api/human-attention-inbox/{attention_item_id}/replacement-options"
)
async def input_replacement_options_ui(
    attention_item_id: str,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """List exact registered alternatives for one current replacement item."""

    controller = getattr(
        request.app.state, "input_replacement_resolution_controller", None
    )
    if not isinstance(controller, InputReplacementResolutionUIController):
        raise HTTPException(
            status_code=503,
            detail="Input replacement resolution is unavailable.",
        )
    try:
        return await controller.options(
            context=context, attention_item_id=attention_item_id
        )
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid replacement options request."
        ) from None


@app.post(
    "/api/human-attention-inbox/{attention_item_id}/replace-input"
)
async def resolve_input_replacement_ui(
    attention_item_id: str,
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Select one exact registered replacement and rerun preparation once."""

    controller = getattr(
        request.app.state, "input_replacement_resolution_controller", None
    )
    if not isinstance(controller, InputReplacementResolutionUIController):
        raise HTTPException(
            status_code=503,
            detail="Input replacement resolution is unavailable.",
        )
    try:
        result = await controller.resolve(
            context=context,
            attention_item_id=attention_item_id,
            action=InputReplacementAction(body.get("action", "")),
            replacement_option_id=str(body.get("replacement_option_id", "")),
        )
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid input replacement."
        ) from None
    return {
        "message": result.message,
        "reason_code": result.reason_code,
        "status": result.status.value,
    }


@app.post(
    "/api/human-attention-inbox/{attention_item_id}/"
    "register-and-replace-resume"
)
async def register_and_replace_resume_ui(
    attention_item_id: str,
    request: Request,
    file: UploadFile = File(...),
    invocation_id: str = Form(...),
    display_name: str = Form(...),
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Register one bounded uploaded resume and delegate replacement."""

    controller = getattr(
        request.app.state,
        "new_resume_candidate_replacement_controller",
        None,
    )
    if not isinstance(
        controller, NewResumeCandidateReplacementUIController
    ):
        raise HTTPException(
            status_code=503,
            detail="New ResumeCandidate replacement is unavailable.",
        )
    try:
        content = await file.read(NEW_RESUME_UPLOAD_MAX_BYTES + 1)
        await file.close()
        result = await controller.replace(
            context=context,
            attention_item_id=attention_item_id,
            invocation_id=invocation_id,
            uploaded_content=content,
            display_name=display_name,
        )
    except (AttributeError, OSError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid resume replacement upload."
        ) from None
    return {
        "message": result.message,
        "reason_code": result.reason_code,
        "status": result.status.value,
    }


@app.post(
    "/api/human-attention-inbox/{attention_item_id}/"
    "register-and-replace-base-latex"
)
async def register_and_replace_base_latex_ui(
    attention_item_id: str,
    request: Request,
    file: UploadFile = File(...),
    invocation_id: str = Form(...),
    display_label: str = Form(...),
    version_note: str | None = Form(None),
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Register one bounded strict Base LaTeX source and delegate replacement."""

    controller = getattr(
        request.app.state,
        "new_base_latex_version_replacement_controller",
        None,
    )
    if not isinstance(
        controller, NewBaseLatexVersionReplacementUIController
    ):
        raise HTTPException(
            status_code=503,
            detail="New Base LaTeX replacement is unavailable.",
        )
    try:
        content = await file.read(NEW_BASE_LATEX_UPLOAD_MAX_BYTES + 1)
        await file.close()
        result = await controller.replace(
            context=context,
            attention_item_id=attention_item_id,
            invocation_id=invocation_id,
            uploaded_content=content,
            display_label=display_label,
            version_note=version_note,
        )
    except (AttributeError, OSError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid Base LaTeX replacement upload."
        ) from None
    return {
        "message": result.message,
        "reason_code": result.reason_code,
        "status": result.status.value,
    }


@app.get("/api/resume-layout-correction-previews/targets/{target_id}")
async def resume_layout_correction_preview_ui(
    target_id: str,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Create or reuse one current, immutable Resume preview."""

    controller = getattr(
        request.app.state,
        "resume_layout_correction_preview_controller",
        None,
    )
    if not isinstance(
        controller, ResumeLayoutCorrectionPreviewUIController
    ):
        raise HTTPException(
            status_code=503, detail="Resume preview is unavailable."
        )
    try:
        result = await controller.get_or_create(
            context=context, target_id=target_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid Resume preview request."
        ) from None
    return result.to_dict()


@app.get(
    "/api/resume-layout-correction-previews/{opaque_reference}/"
    "pages/{page_number}"
)
async def resume_layout_correction_preview_page_ui(
    opaque_reference: str,
    page_number: int,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> Response:
    """Return one authenticated PNG page without revealing storage paths."""

    controller = getattr(
        request.app.state,
        "resume_layout_correction_preview_controller",
        None,
    )
    if not isinstance(
        controller, ResumeLayoutCorrectionPreviewUIController
    ):
        raise HTTPException(
            status_code=503, detail="Resume preview is unavailable."
        )
    try:
        content = controller.read_page(
            context=context,
            opaque_preview_reference=opaque_reference,
            page_number=page_number,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        content = None
    if content is None:
        raise HTTPException(status_code=404, detail="Preview unavailable.")
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get(
    "/api/cover-letter-overflow-previews/targets/{target_id}"
)
async def cover_letter_overflow_preview_ui(
    target_id: str,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Create or reuse one current, immutable Cover Letter preview."""

    controller = getattr(
        request.app.state,
        "cover_letter_overflow_preview_controller",
        None,
    )
    if not isinstance(controller, CoverLetterOverflowPreviewUIController):
        raise HTTPException(
            status_code=503, detail="Cover Letter preview is unavailable."
        )
    try:
        result = await controller.get_or_create(
            context=context, target_id=target_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid Cover Letter preview request."
        ) from None
    return result.to_dict()


@app.get(
    "/api/cover-letter-overflow-previews/{opaque_reference}/"
    "pages/{page_number}"
)
async def cover_letter_overflow_preview_page_ui(
    opaque_reference: str,
    page_number: int,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> Response:
    """Return one authenticated PNG page without exposing private paths."""

    controller = getattr(
        request.app.state,
        "cover_letter_overflow_preview_controller",
        None,
    )
    if not isinstance(controller, CoverLetterOverflowPreviewUIController):
        raise HTTPException(
            status_code=503, detail="Cover Letter preview is unavailable."
        )
    try:
        content = controller.read_page(
            context=context,
            opaque_preview_reference=opaque_reference,
            page_number=page_number,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        content = None
    if content is None:
        raise HTTPException(status_code=404, detail="Preview unavailable.")
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post(
    "/api/human-attention-inbox/{attention_item_id}/"
    "correct-cover-letter-overflow"
)
async def correct_cover_letter_overflow_ui(
    attention_item_id: str,
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Record one explicit format-only Cover Letter correction."""

    controller = getattr(
        request.app.state,
        "cover_letter_overflow_correction_controller",
        None,
    )
    if not isinstance(
        controller, CoverLetterOverflowCorrectionUIController
    ):
        raise HTTPException(
            status_code=503,
            detail="Cover Letter correction is unavailable.",
        )
    try:
        if set(body) != {"action"}:
            raise ValueError("only the typed action is accepted")
        command = CoverLetterOverflowCorrectionUICommand(
            attention_item_id=attention_item_id,
            action=CoverLetterOverflowCorrectionAction(body["action"]),
        )
        result = await controller.resolve(
            context=context, command=command
        )
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid Cover Letter correction."
        ) from None
    return {
        "message": result.message,
        "reason_code": result.reason_code,
        "status": result.status.value,
    }


@app.post(
    "/api/human-attention-inbox/{attention_item_id}/"
    "correct-resume-layout"
)
async def correct_resume_layout_ui(
    attention_item_id: str,
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Record one explicit bounded Resume layout revision directive."""

    controller = getattr(
        request.app.state, "resume_layout_correction_controller", None
    )
    if not isinstance(controller, ResumeLayoutCorrectionUIController):
        raise HTTPException(
            status_code=503,
            detail="Resume layout correction is unavailable.",
        )
    try:
        action = ResumeLayoutCorrectionAction(body.get("action", ""))
        raw_issues = body.get("visual_issues", [])
        if not isinstance(raw_issues, list) or len(raw_issues) > 5:
            raise ValueError("visual issues are invalid")
        issues = tuple(ResumeLayoutVisualIssue(item) for item in raw_issues)
        result = await controller.correct(
            context=context,
            attention_item_id=attention_item_id,
            action=action,
            visual_issues=issues,
        )
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid Resume layout correction."
        ) from None
    return {
        "message": result.message,
        "reason_code": result.reason_code,
        "status": result.status.value,
    }


@app.post(
    "/api/human-attention-inbox/{attention_item_id}/"
    "correct-unsupported-claim"
)
async def correct_unsupported_claim_ui(
    attention_item_id: str,
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Record one explicit REMOVE or REWRITE directive."""

    controller = getattr(
        request.app.state,
        "unsupported_claim_correction_controller",
        None,
    )
    if not isinstance(controller, UnsupportedClaimCorrectionUIController):
        raise HTTPException(
            status_code=503,
            detail="Unsupported claim correction is unavailable.",
        )
    try:
        action = UnsupportedClaimCorrectionAction(body.get("action", ""))
        instruction = body.get("instruction")
        if instruction is not None and not isinstance(instruction, str):
            raise ValueError("instruction must be text")
        result = await controller.correct(
            context=context,
            attention_item_id=attention_item_id,
            action=action,
            instruction=instruction,
        )
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="Invalid unsupported claim correction."
        ) from None
    return {
        "message": result.message,
        "reason_code": result.reason_code,
        "status": result.status.value,
    }


@app.post(
    "/api/human-attention-inbox/{attention_item_id}/"
    "correct-latex-compilation"
)
async def correct_latex_compilation_ui(
    attention_item_id: str,
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Record one explicit regenerate-and-retry directive."""

    controller = getattr(
        request.app.state,
        "latex_compilation_correction_controller",
        None,
    )
    if not isinstance(controller, LatexCompilationCorrectionUIController):
        raise HTTPException(
            status_code=503,
            detail="LaTeX Compilation correction is unavailable.",
        )
    try:
        action = LatexCompilationCorrectionAction(body.get("action", ""))
        result = await controller.correct(
            context=context,
            attention_item_id=attention_item_id,
            action=action,
        )
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Invalid LaTeX Compilation correction.",
        ) from None
    return {
        "message": result.message,
        "reason_code": result.reason_code,
        "status": result.status.value,
    }


@app.post(
    "/api/human-attention-inbox/{attention_item_id}/resolve-version-choice"
)
async def resolve_version_choice_ui(
    attention_item_id: str,
    body: dict,
    request: Request,
    context: AuthenticatedSubjectContext = Depends(
        _authenticated_dashboard_subject
    ),
) -> dict:
    """Resolve one current ResumeCandidate or LaTeX choice."""

    controller = getattr(
        request.app.state, "version_choice_resolution_controller", None
    )
    if not isinstance(controller, VersionChoiceResolutionUIController):
        raise HTTPException(
            status_code=503,
            detail="Version choice resolution is unavailable.",
        )
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(
            status_code=422, detail="A clear version choice is required."
        )
    result = await controller.resolve(
        context=context,
        attention_item_id=attention_item_id,
        user_message=message,
    )
    return result.to_dict()


# ===========================================================================
# REST API — Jobs
# ===========================================================================

@app.get("/api/jobs")
async def list_jobs(
    status: Optional[str] = None,
    company: Optional[str] = None,
    min_score: Optional[int] = None,
    search: Optional[str] = None,
    sort_by: str = "discovered_at",
    sort_order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    Return a paginated, filtered list of jobs.

    Query parameters:
        status      — filter by exact status string
        company     — partial match against company name
        min_score   — only jobs with match_score >= this value
        search      — full-text search across title, company, location
        sort_by     — column name (discovered_at, match_score, company, title,
                      status, applied_at)
        sort_order  — asc | desc
        limit       — page size (default 50)
        offset      — pagination offset (default 0)
    """
    jobs, total = get_all_jobs(
        status=status,
        company=company,
        min_score=min_score,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    return {"jobs": jobs, "total": total, "limit": limit, "offset": offset}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    """Return a single job record by ID."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.patch("/api/jobs/{job_id}")
async def update_job(job_id: str, body: dict) -> dict:
    """
    Partially update a job.

    Accepted body keys:
        status  — must be a member of VALID_STATUSES
        notes   — free-text string
    """
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if "status" in body:
        if not update_job_status(job_id, body["status"]):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Valid values: {VALID_STATUSES}",
            )

    if "notes" in body:
        update_job_notes(job_id, body["notes"])

    return get_job_by_id(job_id)


@app.delete("/api/jobs/{job_id}")
async def remove_job(job_id: str) -> dict:
    """Permanently delete a job record."""
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


# ===========================================================================
# REST API — Ignore & Purge
# ===========================================================================

@app.post("/api/jobs/ignore")
async def ignore_selected(body: dict) -> dict:
    """Mark selected jobs as ignored. They won't reappear on future discovery runs.
    Body: {"job_ids": ["id1", "id2", ...]}
    """
    job_ids = body.get("job_ids", [])
    if not job_ids:
        raise HTTPException(status_code=400, detail="No job_ids provided")
    count = ignore_jobs(job_ids)
    return {"ok": True, "ignored": count}


@app.post("/api/purge")
async def purge(body: dict = {}) -> dict:
    """Nuke discovery data.
    Body: {"keep_ignore_list": true} — preserves ignore list (default).
          {"keep_ignore_list": false} — full factory reset.
    """
    keep = body.get("keep_ignore_list", True)
    if keep:
        result = purge_all()
    else:
        result = purge_everything()
    return {"ok": True, **result}


@app.get("/api/ignored/count")
async def ignored_count() -> dict:
    """Return the number of ignored job hashes."""
    return {"count": get_ignored_count()}


# ===========================================================================
# REST API — Stats and metadata
# ===========================================================================

@app.get("/api/stats")
async def stats() -> dict:
    """Return aggregate counts and average score."""
    return get_stats()


@app.get("/api/stats/timeline")
async def timeline() -> list:
    """Return per-day discovery and application counts (last 30 days)."""
    return get_timeline_stats()


@app.get("/api/stats/scores")
async def scores() -> list:
    """Return match-score distribution bucketed into score brackets."""
    return get_score_distribution()


@app.get("/api/companies")
async def companies() -> list:
    """Return distinct company names for the filter dropdown."""
    return get_companies()


@app.get("/api/statuses")
async def statuses() -> list:
    """Return all valid status strings."""
    return VALID_STATUSES


# ===========================================================================
# REST API — Follow-ups & Ghost Detection
# ===========================================================================

@app.get("/api/follow-ups")
async def get_follow_ups() -> dict:
    """Get overdue follow-ups and ghost alerts."""
    from utils.tracker import get_overdue_follow_ups, get_ghost_alerts
    return {
        "overdue": get_overdue_follow_ups(),
        "ghosts": get_ghost_alerts(days=14),
    }


@app.post("/api/jobs/{job_id}/follow-up")
async def mark_follow_up(job_id: str, body: dict = {}) -> dict:
    """Mark follow-up done and reschedule."""
    from utils.tracker import increment_follow_up
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    days = body.get("next_days", 7)
    increment_follow_up(job_id, days=days)
    return {"ok": True, "job_id": job_id, "next_days": days}


@app.post("/api/jobs/{job_id}/dismiss-follow-up")
async def dismiss_follow_up_endpoint(job_id: str) -> dict:
    """Clear follow-up reminder for a job."""
    from utils.tracker import dismiss_follow_up
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    dismiss_follow_up(job_id)
    return {"ok": True, "job_id": job_id}


# ===========================================================================
# REST API — Profile
# ===========================================================================

@app.get("/api/profile")
async def get_profile() -> dict:
    """Return the current profile.yaml as JSON, or signal setup needed."""
    import yaml
    profile_path = BASE_DIR.parent / "profile.yaml"
    if not profile_path.exists():
        return {"needs_setup": True}
    with open(profile_path) as f:
        return yaml.safe_load(f)


@app.post("/api/setup")
async def run_setup(body: dict) -> dict:
    """First-run wizard: create profile.yaml from wizard data."""
    import yaml
    profile_path = BASE_DIR.parent / "profile.yaml"
    example_path = BASE_DIR.parent / "profile.yaml.example"

    # Load defaults from example
    defaults = {}
    if example_path.exists():
        with open(example_path) as f:
            defaults = yaml.safe_load(f) or {}

    # Deep merge wizard data over defaults
    def deep_merge(base, updates):
        for key, value in updates.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                deep_merge(base[key], value)
            else:
                base[key] = value

    deep_merge(defaults, body)

    with open(profile_path, "w") as f:
        yaml.dump(defaults, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # Try to start scheduler now that profile exists
    try:
        from scheduler import setup_scheduler, start_scheduler
        setup_scheduler()
        start_scheduler()
    except Exception:
        pass

    return defaults


@app.patch("/api/profile")
async def update_profile(body: dict) -> dict:
    """Partially update profile.yaml with recursive deep merge."""
    import yaml
    profile_path = BASE_DIR.parent / "profile.yaml"
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    def deep_merge(base, updates):
        for key, value in updates.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                deep_merge(base[key], value)
            else:
                base[key] = value

    deep_merge(profile, body)

    with open(profile_path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # Clear LLM backend cache so new ai config takes effect
    try:
        from utils.llm import clear_backend_cache
        clear_backend_cache()
    except ImportError:
        pass

    return profile


@app.post("/api/profile/score")
async def score_profile_endpoint() -> dict:
    """
    AI-powered profile and resume analysis.
    Reads profile.yaml + resume PDF, sends to Claude for scoring.
    Returns strengths, gaps, resume suggestions, role fit rankings.
    """
    import yaml

    profile_path = BASE_DIR.parent / "profile.yaml"
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    async def _do_score():
        try:
            from utils.brain import ClaudeBrain
            from utils.resume_parser import extract_resume_text

            await broadcast_event({"type": "profile_score_started", "data": {}})

            brain = ClaudeBrain(verbose=False, profile=profile)
            resume_path = profile.get("resume_path", "")
            resume_text = extract_resume_text(resume_path) if resume_path else ""

            result = brain.score_profile(profile, resume_text)

            await broadcast_event({
                "type": "profile_score_complete",
                "data": result,
            })
        except Exception as exc:
            await broadcast_event({
                "type": "profile_score_error",
                "data": {"error": str(exc)},
            })

    asyncio.create_task(_do_score())
    return {"status": "started"}


# ===========================================================================
# REST API — Resume Management
# ===========================================================================

def _load_resume_meta() -> dict:
    RESUMES_DIR.mkdir(exist_ok=True)
    if RESUMES_META.exists():
        return json.loads(RESUMES_META.read_text())
    return {}

def _save_resume_meta(meta: dict):
    RESUMES_DIR.mkdir(exist_ok=True)
    RESUMES_META.write_text(json.dumps(meta, indent=2))

def _update_profile_resume_path(path: str):
    import yaml
    profile_path = BASE_DIR.parent / "profile.yaml"
    with open(profile_path) as f:
        profile = yaml.safe_load(f)
    profile["resume_path"] = path
    with open(profile_path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


@app.get("/api/resumes")
async def list_resumes() -> list:
    """List all stored resumes with metadata."""
    meta = _load_resume_meta()
    return [
        {"name": name, **info, "exists": (RESUMES_DIR / info["filename"]).exists()}
        for name, info in meta.items()
    ]


@app.post("/api/resumes")
async def upload_resume(file: UploadFile = File(...), name: str = Form(...)) -> dict:
    """Upload a resume PDF with a display name."""
    RESUMES_DIR.mkdir(exist_ok=True)
    meta = _load_resume_meta()

    safe_name = "".join(c for c in name if c.isalnum() or c in " -_").strip()
    if not safe_name:
        raise HTTPException(400, "Invalid resume name")

    filename = f"{safe_name.replace(' ', '_')}_{file.filename}"
    dest = RESUMES_DIR / filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    is_first = len(meta) == 0
    meta[safe_name] = {
        "filename": filename,
        "original_name": file.filename,
        "is_default": is_first,
    }
    _save_resume_meta(meta)

    if is_first:
        _update_profile_resume_path(str(dest))

    return {"name": safe_name, "filename": filename, "is_default": is_first}


@app.delete("/api/resumes/{name}")
async def delete_resume(name: str) -> dict:
    """Remove a resume by display name."""
    meta = _load_resume_meta()
    if name not in meta:
        raise HTTPException(404, "Resume not found")
    filepath = RESUMES_DIR / meta[name]["filename"]
    was_default = meta[name].get("is_default", False)
    if filepath.exists():
        filepath.unlink()
    del meta[name]
    if was_default and meta:
        first_key = next(iter(meta))
        meta[first_key]["is_default"] = True
        _update_profile_resume_path(str(RESUMES_DIR / meta[first_key]["filename"]))
    _save_resume_meta(meta)
    return {"ok": True}


@app.patch("/api/resumes/{name}/default")
async def set_default_resume(name: str) -> dict:
    """Set a resume as the default for applications."""
    meta = _load_resume_meta()
    if name not in meta:
        raise HTTPException(404, "Resume not found")
    for key in meta:
        meta[key]["is_default"] = (key == name)
    _save_resume_meta(meta)
    _update_profile_resume_path(str(RESUMES_DIR / meta[name]["filename"]))
    return {"ok": True, "default": name}


@app.get("/api/resumes/{name}/download")
async def download_resume(name: str):
    """Download a resume file."""
    meta = _load_resume_meta()
    if name not in meta:
        raise HTTPException(404, "Resume not found")
    filepath = RESUMES_DIR / meta[name]["filename"]
    if not filepath.exists():
        raise HTTPException(404, "File not found on disk")
    return FileResponse(filepath, filename=meta[name].get("original_name", meta[name]["filename"]))


# ===========================================================================
# REST API — Background actions
# ===========================================================================

@app.post("/api/discover")
async def trigger_discover() -> dict:
    """
    Launch a full discovery run in the background.

    The endpoint returns immediately with {"status": "started"}.
    Progress events (discovery_started, discovery_complete, discovery_error)
    are broadcast over the WebSocket.
    """
    import yaml

    profile_path = BASE_DIR.parent / "profile.yaml"
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    async def _run_discovery() -> None:
        try:
            await broadcast_event({"type": "discovery_started", "data": {}})

            from utils.discovery import discover_all_jobs
            from utils.tracker import is_already_seen, log_discovered

            jobs = await discover_all_jobs(profile)
            new_count = 0
            for job in jobs:
                if not is_already_seen(job.id):
                    log_discovered(job)
                    new_count += 1

            await broadcast_event(
                {
                    "type": "discovery_complete",
                    "data": {"total": len(jobs), "new": new_count},
                }
            )
        except Exception as exc:
            await broadcast_event(
                {"type": "discovery_error", "data": {"error": str(exc)}}
            )

    asyncio.create_task(_run_discovery())
    return {"status": "started"}


@app.post("/api/rescore/{job_id}")
async def rescore_job(job_id: str) -> dict:
    """
    Re-score a single job with the Claude brain.

    The endpoint returns immediately. A rescore_complete or rescore_error
    WebSocket event is emitted when finished.
    """
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def _do_rescore() -> None:
        try:
            import yaml
            from utils.brain import ClaudeBrain

            profile_path = BASE_DIR.parent / "profile.yaml"
            with open(profile_path) as f:
                profile = yaml.safe_load(f)

            brain = ClaudeBrain(verbose=False, profile=profile)
            from utils.resume_parser import extract_resume_text
            resume_text = extract_resume_text(profile.get("resume_path", ""))
            desc = (
                job.get("description", "")
                or f"Job: {job['title']} at {job['company']}. Location: {job['location']}"
            )
            result = brain.match_job(desc, profile, resume_text=resume_text)
            score: int = result.get("score", 0)
            reasoning: str = result.get("reasoning", "")
            cover_letter: str = result.get("cover_letter", "")

            log_matched(job_id, score, reasoning, cover_letter)

            min_score: int = profile["preferences"].get("min_match_score", 65)
            if score < min_score:
                log_skipped(job_id, f"Score {score} < {min_score}")

            await broadcast_event(
                {"type": "rescore_complete", "data": {"id": job_id, "score": score}}
            )
        except Exception as exc:
            await broadcast_event(
                {
                    "type": "rescore_error",
                    "data": {"id": job_id, "error": str(exc)},
                }
            )

    asyncio.create_task(_do_rescore())
    return {"status": "started"}


@app.post("/api/jobs/{job_id}/tailor")
async def tailor_job(job_id: str) -> dict:
    """Generate tailored resume content for a specific job."""
    from utils.tracker import get_tailored_resume, update_tailored_resume

    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def _do_tailor():
        try:
            import yaml
            from utils.resume_tailor import tailor_resume
            from utils.resume_parser import extract_resume_text
            from utils.brain import ClaudeBrain

            profile_path = BASE_DIR.parent / "profile.yaml"
            with open(profile_path) as f:
                profile = yaml.safe_load(f)

            resume_text = extract_resume_text(profile.get("resume_path", ""))
            desc = job.get("description", "") or f"Job: {job['title']} at {job['company']}"

            brain = ClaudeBrain(verbose=False, profile=profile)
            result = tailor_resume(desc, resume_text, profile, brain=brain)

            update_tailored_resume(job_id, result)

            await broadcast_event({
                "type": "tailor_complete",
                "data": {"id": job_id, "has_content": bool(result.get("tailored_summary"))}
            })
        except Exception as exc:
            await broadcast_event({
                "type": "tailor_error",
                "data": {"id": job_id, "error": str(exc)}
            })

    asyncio.create_task(_do_tailor())
    return {"status": "started", "job_id": job_id}


@app.get("/api/jobs/{job_id}/tailor")
async def get_tailor(job_id: str) -> dict:
    """Get tailored resume content for a job."""
    from utils.tracker import get_tailored_resume
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return get_tailored_resume(job_id)


@app.post("/api/score-all")
async def score_all_unscored() -> dict:
    """
    Score every job that currently has no match_score.

    Returns {"status": "started", "count": N} immediately.
    A score_all_complete or score_all_error WebSocket event follows.
    """
    unscored = get_unscored_jobs()

    async def _do_score_all() -> None:
        try:
            import yaml
            from utils.brain import ClaudeBrain

            profile_path = BASE_DIR.parent / "profile.yaml"
            with open(profile_path) as f:
                profile = yaml.safe_load(f)

            brain = ClaudeBrain(verbose=False, profile=profile)
            from utils.resume_parser import extract_resume_text
            resume_text = extract_resume_text(profile.get("resume_path", ""))
            min_score: int = profile["preferences"].get("min_match_score", 65)

            for job_row in unscored:
                try:
                    desc = (
                        job_row.get("description", "")
                        or (
                            f"Job: {job_row['title']} at {job_row['company']}. "
                            f"Location: {job_row['location']}"
                        )
                    )
                    result = brain.match_job(desc, profile, resume_text=resume_text)
                    score: int = result.get("score", 0)
                    log_matched(
                        job_row["id"],
                        score,
                        result.get("reasoning", ""),
                        result.get("cover_letter", ""),
                    )
                    if score < min_score:
                        log_skipped(job_row["id"], f"Score {score} < {min_score}")
                except Exception:
                    # Skip individual failures so the batch continues
                    pass

            await broadcast_event(
                {"type": "score_all_complete", "data": {"count": len(unscored)}}
            )
        except Exception as exc:
            await broadcast_event(
                {"type": "score_all_error", "data": {"error": str(exc)}}
            )

    asyncio.create_task(_do_score_all())
    return {"status": "started", "count": len(unscored)}


# ===========================================================================
# REST API — Apply (Form Filling)
# ===========================================================================

# Track active apply sessions so we can report status / cancel
_apply_state = {
    "running": False,
    "job_id": None,
    "progress": [],  # list of {job_id, status, message}
    "cancel_requested": False,
}


@app.post("/api/apply/{job_id}")
async def apply_single_job(job_id: str, body: dict = {}) -> dict:
    """
    Apply to a single job by ID. Launches Playwright, fills the form, submits.

    Body options:
        dry_run  — bool, default True. If True, fills form but doesn't submit.
    """
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("apply_url"):
        raise HTTPException(status_code=400, detail="Job has no apply URL")
    if _apply_state["running"]:
        raise HTTPException(status_code=409, detail="An apply session is already running")

    dry_run = body.get("dry_run", True)

    async def _do_apply():
        _apply_state["running"] = True
        _apply_state["job_id"] = job_id
        _apply_state["cancel_requested"] = False

        try:
            import yaml
            from playwright.async_api import async_playwright
            from utils.brain import ClaudeBrain
            from utils.browser_session import launch_browser_session
            from adapters.stagehand_adapter import apply_smart
            from utils.tracker import log_applied

            profile_path = BASE_DIR.parent / "profile.yaml"
            with open(profile_path) as f:
                profile = yaml.safe_load(f)

            brain = ClaudeBrain(verbose=False, profile=profile)
            cover_letter = job.get("cover_letter", "")

            await broadcast_event({
                "type": "apply_started",
                "data": {
                    "job_id": job_id,
                    "title": job["title"],
                    "company": job["company"],
                    "dry_run": dry_run,
                }
            })

            async with async_playwright() as p:
                session = await launch_browser_session(p, profile, headless=False)
                page = session.page

                platform = job.get("platform", "")
                apply_url = job["apply_url"]

                success = await apply_smart(
                    page, apply_url, profile, brain,
                    cover_letter=cover_letter, dry_run=dry_run,
                    platform=platform,
                    company=job.get("company", ""),
                    title=job.get("title", ""),
                    description=job.get("description", ""),
                )

                if not dry_run:
                    log_applied(job_id, success)

                await broadcast_event({
                    "type": "apply_complete",
                    "data": {
                        "job_id": job_id,
                        "success": success,
                        "dry_run": dry_run,
                        "title": job["title"],
                        "company": job["company"],
                    }
                })

                # Keep browser open for a bit so user can review
                await asyncio.sleep(5)
                await session.close()

        except Exception as exc:
            await broadcast_event({
                "type": "apply_error",
                "data": {"job_id": job_id, "error": str(exc)}
            })
        finally:
            _apply_state["running"] = False
            _apply_state["job_id"] = None

    asyncio.create_task(_do_apply())
    return {"status": "started", "job_id": job_id, "dry_run": dry_run}


@app.post("/api/apply-batch")
async def apply_batch(body: dict = {}) -> dict:
    """
    Apply to all matched jobs above the score threshold.

    Body options:
        dry_run   — bool, default True
        max_count — int, max applications this batch (default 10)
        min_score — int, override minimum score (default from profile)
    """
    if _apply_state["running"]:
        raise HTTPException(status_code=409, detail="An apply session is already running")

    dry_run = body.get("dry_run", True)
    max_count = body.get("max_count", 10)
    min_score_override = body.get("min_score")

    # Get matched jobs that haven't been applied to yet
    from utils.tracker import get_all_jobs as get_jobs_filtered, get_today_count
    matched_jobs, _ = get_jobs_filtered(status="matched", sort_by="match_score", sort_order="desc", limit=500)

    import yaml
    profile_path = BASE_DIR.parent / "profile.yaml"
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    min_score = min_score_override or profile["preferences"].get("min_match_score", 65)
    rate_limits = profile.get("rate_limits", {})
    max_per_day = rate_limits.get("max_applications_per_day", 25)

    # Filter to only jobs with apply URLs and above score threshold
    eligible = [
        j for j in matched_jobs
        if j.get("apply_url")
        and (j.get("match_score") or 0) >= min_score
    ][:max_count]

    if not eligible:
        return {"status": "no_eligible_jobs", "count": 0}

    today_count = get_today_count()
    remaining_today = max(0, max_per_day - today_count)
    eligible = eligible[:remaining_today]

    if not eligible:
        return {"status": "daily_limit_reached", "today": today_count, "max": max_per_day}

    async def _do_batch():
        import random
        _apply_state["running"] = True
        _apply_state["progress"] = []
        _apply_state["cancel_requested"] = False

        try:
            from playwright.async_api import async_playwright
            from utils.brain import ClaudeBrain
            from utils.browser_session import launch_browser_session
            from adapters.stagehand_adapter import apply_smart
            from utils.tracker import log_applied

            brain = ClaudeBrain(verbose=False, profile=profile)
            min_delay = rate_limits.get("min_delay_seconds", 60)
            max_delay = rate_limits.get("max_delay_seconds", 180)

            await broadcast_event({
                "type": "apply_batch_started",
                "data": {"count": len(eligible), "dry_run": dry_run}
            })

            async with async_playwright() as p:
                session = await launch_browser_session(p, profile, headless=False)
                page = session.page

                applied_count = 0
                for i, job in enumerate(eligible):
                    if _apply_state["cancel_requested"]:
                        await broadcast_event({
                            "type": "apply_batch_cancelled",
                            "data": {"applied": applied_count, "cancelled_at": i}
                        })
                        break

                    job_id = job["id"]
                    _apply_state["job_id"] = job_id

                    await broadcast_event({
                        "type": "apply_progress",
                        "data": {
                            "current": i + 1,
                            "total": len(eligible),
                            "job_id": job_id,
                            "title": job["title"],
                            "company": job["company"],
                            "dry_run": dry_run,
                        }
                    })

                    try:
                        cover_letter = job.get("cover_letter", "")
                        platform = job.get("platform", "")
                        apply_url = job["apply_url"]

                        success = await apply_smart(
                            page, apply_url, profile, brain,
                            cover_letter=cover_letter, dry_run=dry_run,
                            platform=platform,
                            company=job.get("company", ""),
                            title=job.get("title", ""),
                            description=job.get("description", ""),
                        )

                        if not dry_run:
                            log_applied(job_id, success)

                        applied_count += 1
                        _apply_state["progress"].append({
                            "job_id": job_id, "status": "success" if success else "failed",
                            "title": job["title"], "company": job["company"],
                        })

                    except Exception as exc:
                        if not dry_run:
                            log_applied(job_id, False)
                        _apply_state["progress"].append({
                            "job_id": job_id, "status": "error",
                            "error": str(exc), "title": job["title"],
                            "company": job["company"],
                        })

                    # Rate limiting between applications
                    if i < len(eligible) - 1:
                        delay = random.randint(min_delay, max_delay)
                        await broadcast_event({
                            "type": "apply_waiting",
                            "data": {"seconds": delay, "next_index": i + 2}
                        })
                        await asyncio.sleep(delay)

                await session.close()

            await broadcast_event({
                "type": "apply_batch_complete",
                "data": {
                    "applied": applied_count,
                    "total": len(eligible),
                    "dry_run": dry_run,
                    "results": _apply_state["progress"],
                }
            })

        except Exception as exc:
            await broadcast_event({
                "type": "apply_batch_error",
                "data": {"error": str(exc)}
            })
        finally:
            _apply_state["running"] = False
            _apply_state["job_id"] = None

    asyncio.create_task(_do_batch())
    return {
        "status": "started",
        "count": len(eligible),
        "dry_run": dry_run,
        "daily_remaining": remaining_today,
    }


@app.get("/api/apply/status")
async def apply_status() -> dict:
    """Get the current apply session status."""
    return {
        "running": _apply_state["running"],
        "current_job_id": _apply_state["job_id"],
        "progress": _apply_state["progress"],
    }


@app.post("/api/apply/cancel")
async def cancel_apply() -> dict:
    """Request cancellation of the current batch apply."""
    if not _apply_state["running"]:
        return {"status": "not_running"}
    _apply_state["cancel_requested"] = True
    return {"status": "cancel_requested"}


@app.post("/api/resolve-url/{job_id}")
async def resolve_url(job_id: str) -> dict:
    """
    Resolve a job's aggregator URL to a real ATS application form URL.
    Updates the job's apply_url in the database if resolved.
    """
    from utils.url_resolver import resolve_apply_url, is_ats_url
    from utils.tracker import get_job_by_id, update_apply_url

    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    current_url = job.get("apply_url") or job.get("url", "")
    if is_ats_url(current_url):
        return {"status": "already_ats", "url": current_url}

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            result = await resolve_apply_url(
                page,
                job_url=current_url,
                company=job.get("company", ""),
                title=job.get("title", ""),
                description=job.get("description", ""),
                platform=job.get("platform", ""),
            )

            await browser.close()

        if result["resolved_url"] != current_url:
            update_apply_url(job_id, result["resolved_url"])

        return {
            "status": result["resolution"],
            "original_url": result["original_url"],
            "resolved_url": result["resolved_url"],
            "apply_email": result.get("apply_email"),
            "company_careers": result.get("company_careers"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ===========================================================================
# REST API — YOLO Mode (Fully Autonomous Pipeline)
# ===========================================================================

_yolo_state = {
    "running": False,
    "phase": None,       # "discover" | "score" | "apply" | "waiting"
    "cancel_requested": False,
    "log": [],           # Full action log
    "cycle": 0,          # Which cycle we're on
    "continuous": False,  # Keep looping?
}


@app.post("/api/yolo")
async def start_yolo(body: dict = {}) -> dict:
    """
    YOLO Mode — Fully autonomous: discover → score → apply → repeat.

    Body options:
        dry_run      — bool, default True. Safety net.
        continuous   — bool, default False. If True, loops forever with interval.
        interval_min — int, minutes between cycles (default 360 = 6 hours).
        max_apply    — int, max applications per cycle (default 10).
        min_score    — int, override minimum score threshold.
    """
    if _yolo_state["running"]:
        raise HTTPException(status_code=409, detail="YOLO mode already running")
    if _apply_state["running"]:
        raise HTTPException(status_code=409, detail="An apply session is already running")

    dry_run = body.get("dry_run", True)
    continuous = body.get("continuous", False)
    interval_min = body.get("interval_min", 360)
    max_apply = body.get("max_apply", 10)
    min_score_override = body.get("min_score")

    async def _yolo_pipeline():
        import random
        import yaml

        _yolo_state["running"] = True
        _yolo_state["cancel_requested"] = False
        _yolo_state["continuous"] = continuous
        _yolo_state["log"] = []
        _yolo_state["cycle"] = 0

        def ylog(msg, level="info"):
            """Append to YOLO action log and broadcast."""
            entry = {
                "time": __import__("datetime").datetime.now().isoformat(),
                "msg": msg,
                "level": level,
                "cycle": _yolo_state["cycle"],
            }
            _yolo_state["log"].append(entry)
            # Keep log bounded
            if len(_yolo_state["log"]) > 500:
                _yolo_state["log"] = _yolo_state["log"][-500:]

        try:
            while True:
                if _yolo_state["cancel_requested"]:
                    ylog("YOLO cancelled by user.", "warn")
                    await broadcast_event({"type": "yolo_cancelled", "data": {}})
                    break

                _yolo_state["cycle"] += 1
                cycle = _yolo_state["cycle"]

                await broadcast_event({
                    "type": "yolo_cycle_start",
                    "data": {"cycle": cycle, "dry_run": dry_run, "continuous": continuous}
                })
                ylog(f"=== CYCLE {cycle} START {'[DRY RUN]' if dry_run else '[LIVE]'} ===")

                # --- PHASE 1: DISCOVER ---
                _yolo_state["phase"] = "discover"
                ylog("Phase 1: Discovering jobs...")
                await broadcast_event({"type": "yolo_phase", "data": {"phase": "discover", "cycle": cycle}})

                profile_path = BASE_DIR.parent / "profile.yaml"
                with open(profile_path) as f:
                    profile = yaml.safe_load(f)

                try:
                    from utils.discovery import discover_all_jobs
                    from utils.tracker import is_already_seen, log_discovered

                    jobs = await discover_all_jobs(profile)
                    new_count = 0
                    for job in jobs:
                        if not is_already_seen(job.id):
                            log_discovered(job)
                            new_count += 1
                    ylog(f"Discovered {new_count} new jobs from {len(jobs)} total.")
                    await broadcast_event({
                        "type": "yolo_discover_done",
                        "data": {"total": len(jobs), "new": new_count, "cycle": cycle}
                    })
                except Exception as e:
                    ylog(f"Discovery error: {e}", "error")
                    await broadcast_event({"type": "yolo_error", "data": {"phase": "discover", "error": str(e)}})

                if _yolo_state["cancel_requested"]:
                    break

                # --- PHASE 2: SCORE ---
                _yolo_state["phase"] = "score"
                ylog("Phase 2: Scoring unscored jobs...")
                await broadcast_event({"type": "yolo_phase", "data": {"phase": "score", "cycle": cycle}})

                try:
                    from utils.brain import ClaudeBrain
                    from utils.resume_parser import extract_resume_text

                    brain = ClaudeBrain(verbose=False, profile=profile)
                    resume_text = extract_resume_text(profile.get("resume_path", ""))
                    unscored = get_unscored_jobs()
                    min_score = min_score_override or profile["preferences"].get("min_match_score", 65)
                    scored_count = 0

                    for job_row in unscored:
                        if _yolo_state["cancel_requested"]:
                            break
                        try:
                            desc = (
                                job_row.get("description", "")
                                or f"Job: {job_row['title']} at {job_row['company']}. Location: {job_row['location']}"
                            )
                            result = brain.match_job(desc, profile, resume_text=resume_text)
                            score = result.get("score", 0)
                            log_matched(job_row["id"], score, result.get("reasoning", ""), result.get("cover_letter", ""))
                            if score < min_score:
                                log_skipped(job_row["id"], f"Score {score} < {min_score}")
                            scored_count += 1
                            ylog(f"  Scored: {job_row['title']} @ {job_row['company']} = {score}")
                        except Exception as e:
                            ylog(f"  Score failed: {job_row['title']}: {e}", "error")

                    ylog(f"Scored {scored_count} jobs.")
                    await broadcast_event({
                        "type": "yolo_score_done",
                        "data": {"scored": scored_count, "cycle": cycle}
                    })
                except Exception as e:
                    ylog(f"Scoring error: {e}", "error")
                    await broadcast_event({"type": "yolo_error", "data": {"phase": "score", "error": str(e)}})

                if _yolo_state["cancel_requested"]:
                    break

                # --- PHASE 3: APPLY ---
                _yolo_state["phase"] = "apply"
                ylog("Phase 3: Applying to matched jobs...")
                await broadcast_event({"type": "yolo_phase", "data": {"phase": "apply", "cycle": cycle}})

                try:
                    from playwright.async_api import async_playwright
                    from adapters.stagehand_adapter import apply_smart
                    from utils.tracker import log_applied, get_today_count

                    rate_limits = profile.get("rate_limits", {})
                    max_per_day = rate_limits.get("max_applications_per_day", 25)
                    min_delay = rate_limits.get("min_delay_seconds", 60)
                    max_delay = rate_limits.get("max_delay_seconds", 180)
                    min_score = min_score_override or profile["preferences"].get("min_match_score", 65)

                    # Get matched jobs not yet applied
                    matched, _ = get_all_jobs(status="matched", sort_by="match_score", sort_order="desc", limit=500)
                    eligible = [
                        j for j in matched
                        if j.get("apply_url") and (j.get("match_score") or 0) >= min_score
                    ][:max_apply]

                    today_count = get_today_count()
                    remaining = max(0, max_per_day - today_count)
                    eligible = eligible[:remaining]

                    if not eligible:
                        ylog(f"No eligible jobs to apply to (today: {today_count}/{max_per_day}).")
                    else:
                        ylog(f"Applying to {len(eligible)} jobs...")
                        _apply_state["running"] = True

                        async with async_playwright() as p:
                            from utils.browser_session import launch_browser_session
                            session = await launch_browser_session(p, profile, headless=False)
                            page = session.page
                            applied_count = 0

                            for i, job in enumerate(eligible):
                                if _yolo_state["cancel_requested"]:
                                    break

                                job_id = job["id"]
                                _apply_state["job_id"] = job_id
                                ylog(f"  [{i+1}/{len(eligible)}] {job['title']} @ {job['company']}")

                                await broadcast_event({
                                    "type": "yolo_applying",
                                    "data": {
                                        "current": i + 1, "total": len(eligible),
                                        "title": job["title"], "company": job["company"],
                                        "dry_run": dry_run,
                                    }
                                })

                                try:
                                    cover_letter = job.get("cover_letter", "")
                                    platform = job.get("platform", "")
                                    apply_url = job["apply_url"]

                                    success = await apply_smart(
                                        page, apply_url, profile, brain,
                                        cover_letter=cover_letter, dry_run=dry_run,
                                        platform=platform,
                                        company=job.get("company", ""),
                                        title=job.get("title", ""),
                                        description=job.get("description", ""),
                                    )

                                    if not dry_run:
                                        log_applied(job_id, success)

                                    status_str = "OK" if success else "FAIL"
                                    ylog(f"    -> {status_str}")
                                    applied_count += 1

                                except Exception as e:
                                    ylog(f"    -> ERROR: {e}", "error")
                                    if not dry_run:
                                        log_applied(job_id, False)

                                # Rate limit
                                if i < len(eligible) - 1:
                                    delay = random.randint(min_delay, max_delay)
                                    ylog(f"    Waiting {delay}s...")
                                    await asyncio.sleep(delay)

                            await session.close()

                        _apply_state["running"] = False
                        _apply_state["job_id"] = None
                        ylog(f"Applied to {applied_count}/{len(eligible)} jobs.")

                    await broadcast_event({
                        "type": "yolo_apply_done",
                        "data": {"applied": applied_count if eligible else 0, "cycle": cycle, "dry_run": dry_run}
                    })

                except Exception as e:
                    ylog(f"Apply error: {e}", "error")
                    _apply_state["running"] = False
                    _apply_state["job_id"] = None
                    await broadcast_event({"type": "yolo_error", "data": {"phase": "apply", "error": str(e)}})

                # --- CYCLE COMPLETE ---
                ylog(f"=== CYCLE {cycle} COMPLETE ===")
                await broadcast_event({
                    "type": "yolo_cycle_complete",
                    "data": {"cycle": cycle, "log_size": len(_yolo_state["log"])}
                })

                if not continuous:
                    break

                # Wait for next cycle
                _yolo_state["phase"] = "waiting"
                ylog(f"Next cycle in {interval_min} minutes...")
                await broadcast_event({
                    "type": "yolo_waiting",
                    "data": {"minutes": interval_min, "next_cycle": cycle + 1}
                })

                # Sleep in small increments so cancel is responsive
                for _ in range(interval_min * 6):  # check every 10 seconds
                    if _yolo_state["cancel_requested"]:
                        break
                    await asyncio.sleep(10)

        except Exception as e:
            ylog(f"YOLO fatal error: {e}", "error")
            await broadcast_event({"type": "yolo_error", "data": {"phase": "fatal", "error": str(e)}})
        finally:
            _yolo_state["running"] = False
            _yolo_state["phase"] = None
            _apply_state["running"] = False

    asyncio.create_task(_yolo_pipeline())
    return {
        "status": "started",
        "dry_run": dry_run,
        "continuous": continuous,
        "interval_min": interval_min,
        "max_apply": max_apply,
    }


@app.get("/api/yolo/status")
async def yolo_status() -> dict:
    """Get current YOLO mode status and action log."""
    return {
        "running": _yolo_state["running"],
        "phase": _yolo_state["phase"],
        "cycle": _yolo_state["cycle"],
        "continuous": _yolo_state["continuous"],
        "log_count": len(_yolo_state["log"]),
        "recent_log": _yolo_state["log"][-20:],  # Last 20 entries
    }


@app.get("/api/yolo/log")
async def yolo_log(offset: int = 0, limit: int = 100) -> dict:
    """Get full YOLO action log with pagination."""
    log = _yolo_state["log"]
    return {
        "total": len(log),
        "entries": log[offset:offset + limit],
    }


@app.post("/api/yolo/cancel")
async def cancel_yolo() -> dict:
    """Cancel YOLO mode after current action completes."""
    if not _yolo_state["running"]:
        return {"status": "not_running"}
    _yolo_state["cancel_requested"] = True
    _apply_state["cancel_requested"] = True  # Also cancel any active apply
    return {"status": "cancel_requested"}


# ===========================================================================
# REST API — MCP-powered discovery (ingestion endpoint)
# ===========================================================================

@app.post("/api/ingest")
async def ingest_mcp_jobs(body: dict) -> dict:
    """
    Ingest job results discovered via MCP tools (WebSearch, Playwright, etc.).

    Accepts: {"jobs": [{"title": "...", "company": "...", "url": "...", ...}]}
    This is called by Claude Code agents after running MCP-powered searches.
    """
    from utils.mcp_source import ingest_jobs
    job_dicts = body.get("jobs", [])
    if not job_dicts:
        raise HTTPException(status_code=400, detail="No jobs provided")
    result = ingest_jobs(job_dicts)
    await broadcast_event({
        "type": "mcp_ingest_complete",
        "data": result
    })
    return result


# ===========================================================================
# REST API — Scheduler
# ===========================================================================

@app.get("/api/scheduler/status")
async def scheduler_status() -> dict:
    """Return scheduler state: running flag, job next-run times, last results."""
    try:
        from scheduler import get_scheduler_status
        return get_scheduler_status()
    except Exception:
        return {"running": False, "jobs": [], "last_results": {}}


@app.post("/api/scheduler/trigger/{job_name}")
async def trigger_scheduler_job(job_name: str) -> dict:
    """Manually trigger a scheduler job (discover, score, email)."""
    try:
        from scheduler import scheduler as sched
        job = sched.get_job(job_name)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_name}' not found")
        job.modify(next_run_time=__import__("datetime").datetime.now())
        return {"status": "triggered", "job": job_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================================================
# REST API — Email checking
# ===========================================================================

@app.post("/api/check-email")
async def check_email_now() -> dict:
    """Trigger an email check for application status updates."""
    import yaml
    profile_path = BASE_DIR.parent / "profile.yaml"
    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    async def _do_check():
        try:
            from utils.email_checker import check_emails
            results = check_emails(profile)
            await broadcast_event({
                "type": "email_check_complete",
                "data": {"results": results, "count": len(results)}
            })
        except Exception as exc:
            await broadcast_event({
                "type": "email_check_error",
                "data": {"error": str(exc)}
            })

    asyncio.create_task(_do_check())
    return {"status": "started"}


# ===========================================================================
# WebSocket endpoint
# ===========================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    Maintain a persistent connection with each browser tab.

    The client may send "ping" to keep the connection alive through
    proxies or firewalls that enforce idle timeouts. All other inbound
    messages are silently ignored — this is a server-push channel.
    """
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


# ===========================================================================
# Entry point
# ===========================================================================

def run_server(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Start the uvicorn server.  Called from main.py or directly."""
    normalized_host = str(host or "").strip().casefold()
    loopback = normalized_host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(normalized_host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise RuntimeError(
            "The production dashboard currently permits only a literal "
            "loopback bind address"
        )
    if not all(
        getattr(app.state, name, None) is not None
        for name in (
            "job_library_refresh_controller",
            "automation_cycle_controller",
            "authenticated_subject_dependency",
        )
    ):
        raise RuntimeError(
            "production automation composition is not installed"
        )
    import uvicorn

    print(f"\n  Dashboard running at: http://localhost:{port}")
    print(f"  Press Ctrl+C to stop\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
