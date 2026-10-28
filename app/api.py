"""Local HTTP bridge for the React desktop interface.

The preview endpoint builds a plan exactly once and stores it in memory.  Apply
uses that stored plan after revalidating its file paths; it never calls the LLM
again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from fnmatch import fnmatchcase
import json
import os
import logging
from pathlib import Path
import re
import subprocess
import sys
from threading import Lock
from time import perf_counter
from typing import Dict, List
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.core import history, provider_catalog, provider_store
from app.core import chat as chat_engine
from app.core.chat_resolution import parser_context, resolve_topic
from app.core.semantic_index import SemanticConfig, SemanticIndex
from app.core.content_intelligence import (
    ContentSuggestion,
    plan_content_intelligence,
    sanitize_windows_filename,
)
from app.core.executor import execute_plan
from app.core.planner import Move, sanitize_target_path
from app.core.plan_sanity import evaluate_plan
from app.core.safety import (
    ScanPolicy,
    UnsafePathError,
    assert_safe_root,
    file_identity,
    should_skip_directory,
    validate_move_paths,
    verify_file_snapshot,
)
from app.models.schemas import FileInfo
from app.core.scanner import scan_directory_with_report
from app.main import resolve_models
from app.history_api import router as history_router

load_dotenv()

app = FastAPI(title="Sortly Local API")
logger = logging.getLogger(__name__)
LOCAL_ORIGIN_PATTERN = (
    r"^(https?://(?:127\.0\.0\.1|localhost)(?::\d+)?"
    r"|https?://tauri\.localhost|tauri://localhost)$"
)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, error: RequestValidationError) -> JSONResponse:
    """Never echo request input, which may contain a provider credential."""
    detail = [
        {
            "loc": item.get("loc", ()),
            "msg": item.get("msg", "Invalid request"),
            "type": item.get("type", "validation_error"),
        }
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
    ],
    allow_origin_regex=LOCAL_ORIGIN_PATTERN,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_private_network=True,
    max_age=600,
)
app.include_router(history_router)


@app.middleware("http")
async def unexpected_error(request: Request, call_next):
    """Keep crash responses JSON and CORS-readable while logging diagnostics locally."""
    try:
        return await call_next(request)
    except Exception as error:
        logger.error(
            "Unhandled API error for %s %s",
            request.method,
            request.url.path,
            exc_info=(type(error), error, error.__traceback__),
        )
        headers = {}
        origin = request.headers.get("origin", "")
        if re.fullmatch(LOCAL_ORIGIN_PATTERN, origin):
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    "The local service encountered an unexpected error. "
                    "Check Sortly's local log and History before trying again."
                )
            },
            headers=headers,
        )


class PreviewRequest(BaseModel):
    source: str
    recursive: bool = False
    rename: bool = False
    reorganize: bool = False
    models: List[str] | None = None
    allow_sensitive_cloud: bool = False
    instructions: List[str] = Field(default_factory=list, max_length=20)

    @field_validator("instructions")
    @classmethod
    def bounded_instructions(cls, values: List[str]) -> List[str]:
        if any(not value.strip() or len(value) > 4000 for value in values) or sum(map(len, values)) > 16000:
            raise ValueError("Use nonempty organization instructions totaling at most 16000 characters.")
        return [value.strip() for value in values]


class ApplyFile(BaseModel):
    id: str
    accepted: bool = True
    target_path: str = ""
    destination_name: str = ""


class ApplyRequest(BaseModel):
    plan_id: str
    selected_targets: List[str] = Field(default_factory=list)
    files: List[ApplyFile] | None = None
    risk_acknowledged: bool = False


class ChatRequest(BaseModel):
    plan_id: str
    message: str = Field(min_length=1, max_length=4000)
    files: List[ApplyFile] | None = None


class UndoRequest(BaseModel):
    source: str


class ProviderConnectionRequest(BaseModel):
    model: str = ""
    base_url: str = ""
    api_key: str = Field(default="", repr=False)
    clear_api_key: bool = False


class ProviderConfigRequest(BaseModel):
    primary: str
    fallbacks: List[str] = Field(default_factory=list)
    connections: Dict[str, ProviderConnectionRequest] = Field(default_factory=dict)


class ModelDiscoveryRequest(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    api_key: str = Field(default="", repr=False)


class PlanItem(BaseModel):
    id: str
    name: str
    current_path: str
    destination_name: str
    target_path: str
    reasoning: str
    renamed: bool = False
    selected: bool = True
    category: str = ""
    confidence: str = "insufficient"
    evidence_excerpt: str = ""
    evidence_sources: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    filename_conflict: str = "none"
    evidence_sufficient: bool = False
    detected_type: str = "unknown"
    extraction_status: str = "unavailable"
    rename_reason: str = ""
    sensitive: bool = False


class PreviewResponse(BaseModel):
    plan_id: str
    source: str
    files: List[PlanItem]
    analysis_seconds: float
    already_organized: int = 0
    plan_warnings: List[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    scan_summary: dict = Field(default_factory=dict)
    analysis_stats: dict = Field(default_factory=dict)
    rules_revision: int = 0


@dataclass
class StoredPlan:
    source: Path
    moves: List[Move]
    suggestions: List[ContentSuggestion] = field(default_factory=list)
    applied: bool = False
    attempted: bool = False
    apply_state: str = "preview"
    chat_rules: list[chat_engine.ChatRule] = field(default_factory=list)
    chat_messages: list[dict] = field(default_factory=list)
    chat_decisions: dict = field(default_factory=dict)
    scan_summary: dict = field(default_factory=dict)
    analysis_stats: dict = field(default_factory=dict)
    semantic_index: SemanticIndex | None = field(default=None, repr=False)
    organization_instructions: list[str] = field(default_factory=list)
    chat_topic_cache: dict = field(default_factory=dict, repr=False)
    rules_revision: int = 0
    pending_clarification: str | None = None
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)


plans: Dict[str, StoredPlan] = {}
APPLY_LOCK = Lock()


def _target_path(move: Move) -> str:
    return "/".join(part for part in (move.folder, move.subfolder) if part)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_plan_source(raw_path: str, root: Path) -> Path:
    """Resolve a stored source path against the selected root exactly once."""
    resolved_root = root.expanduser().resolve(strict=False)
    raw = Path(raw_path).expanduser()
    if raw.is_absolute():
        candidate = raw
        root_parts = tuple(part.casefold() for part in resolved_root.parts)
        candidate_parts = tuple(part.casefold() for part in candidate.parts)
        duplicated_prefix = (
            len(candidate_parts) > len(root_parts)
            and candidate_parts[: len(root_parts)] == root_parts
            and candidate_parts[len(root_parts)] == resolved_root.name.casefold()
        )
        if duplicated_prefix:
            candidate = resolved_root.joinpath(*candidate.parts[len(root_parts) + 1 :])
    else:
        parts = raw.parts
        if parts and parts[0].casefold() == resolved_root.name.casefold():
            candidate = resolved_root.joinpath(*parts[1:])
        else:
            candidate = resolved_root / raw
    # Preserve the lexical path so Apply can still detect substituted links.
    return candidate.absolute()


def _validate_for_apply(plan: StoredPlan, allow_stale: bool = False) -> None:
    for move in plan.moves:
        if not move.selected:
            continue
        source = Path(move.src)
        destination = Path(move.dest)
        if allow_stale:
            try:
                if not move.source_snapshot or file_identity(source) != move.source_snapshot.identity:
                    continue  # Let execution report stale operations individually.
            except OSError:
                continue
        if not source.is_file():
            if allow_stale:
                continue  # The executor reports this operation and continues safely.
            raise HTTPException(
                409,
                f"Preview is stale because this source file is missing: {source.name}",
            )
        try:
            validate_move_paths(source, destination, plan.source)
        except UnsafePathError as error:
            logger.warning("Rejected unsafe Apply path: %s", error)
            raise HTTPException(
                400,
                "The selected file or destination did not pass safety checks. Create a new preview.",
            ) from error


def _hierarchy(root: Path, moves: List[dict]) -> dict:
    root = root.resolve()
    tree = {"name": root.name or str(root), "files": [], "folders": {}}
    for move in moves:
        destination = Path(move["dest"]).resolve()
        try:
            parts = destination.relative_to(root).parts
        except ValueError:
            continue
        current = tree
        for folder in parts[:-1]:
            current = current["folders"].setdefault(folder, {"name": folder, "files": [], "folders": {}})
        current["files"].append(parts[-1])

    def freeze(node: dict) -> dict:
        return {
            "name": node["name"],
            "files": sorted(node["files"]),
            "folders": [freeze(folder) for _, folder in sorted(node["folders"].items())],
        }

    return freeze(tree)


def _same_path(first: Path | str, second: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


class NamingPolicyConflict(ValueError):
    pass


def _naming_policy(decisions: dict, index: int) -> dict | None:
    value = decisions.get(f"naming:{index}")
    if not isinstance(value, (list, tuple)) or len(value) != 2 or value[0] != "rename":
        return None
    return json.loads(value[1])


def _unique_destination(destination: Path, taken: set[str], source: Path | str | None = None,
                        *, naming_policy: dict | None = None) -> Path:
    # A source is not a collision, including when a suffix candidate reaches
    # its current name. Repeated analysis must not keep incrementing that suffix.
    candidate = destination
    original = Path(source).name if source is not None else destination.name
    if naming_policy is not None:
        name = chat_engine.collision_filename(destination.name, original, naming_policy)
        if name is None:
            raise NamingPolicyConflict("Filename constraints cannot preserve the extension and existing dates.")
        candidate = destination.with_name(name)
    index = 1
    while (str(candidate).casefold() in taken or candidate.exists()) and not (
        source is not None and _same_path(candidate, source)
    ):
        if naming_policy is None:
            candidate = destination.with_name(f"{destination.stem}-{index}{destination.suffix}")
        else:
            name = chat_engine.collision_filename(destination.name, original, naming_policy, f"-{index}")
            if name is None:
                raise NamingPolicyConflict("A collision suffix cannot fit the filename constraints safely.")
            candidate = destination.with_name(name)
        index += 1
    taken.add(str(candidate).casefold())
    return candidate


def _content_moves(
    suggestions: List[ContentSuggestion],
    source: Path,
) -> List[Move]:
    moves = []
    taken: set[str] = set()
    for suggestion in suggestions:
        destination_folder = source.joinpath(
            suggestion.category,
            suggestion.subcategory,
        )
        current = _resolve_plan_source(suggestion.path, source)
        destination = _unique_destination(destination_folder / suggestion.suggested_name, taken, current)
        moves.append(
            Move(
                str(current),
                str(destination),
                suggestion.category,
                suggestion.reasoning,
                suggestion.subcategory,
                # Already in the right place with the right name: nothing to do.
                suggestion.selected and not _same_path(destination, current),
                suggestion.source_snapshot,
            )
        )
    return moves


def _apply_user_edits(plan: StoredPlan, request: ApplyRequest) -> List[Move]:
    if request.files is None:
        available_targets = {_target_path(move) for move in plan.moves}
        selected_targets = set(request.selected_targets)
        if not selected_targets.issubset(available_targets):
            raise HTTPException(400, "The selected targets do not match this preview.")
        for move in plan.moves:
            move.selected = _target_path(move) in selected_targets and not _same_path(move.src, move.dest)
        return plan.moves

    edits = {edit.id: edit for edit in request.files}
    known_ids = {str(index) for index in range(len(plan.moves))}
    if not set(edits).issubset(known_ids):
        raise HTTPException(400, "The file decisions do not match this preview.")
    moves = []
    taken: set[str] = set()
    for index, original in enumerate(plan.moves):
        edit = edits.get(str(index))
        if edit is None or not edit.accepted:
            moves.append(
                Move(
                    original.src,
                    original.dest,
                    original.folder,
                    original.reason,
                    original.subfolder,
                    False,
                    original.source_snapshot,
                )
            )
            continue
        target_path = edit.target_path or "/".join(
            part for part in (original.folder, original.subfolder) if part
        )
        if target_path == _target_path(original):
            category, subcategory = original.folder, original.subfolder
        else:
            category, subcategory = sanitize_target_path(target_path)
        planned_extension = Path(original.dest).suffix
        source = _resolve_plan_source(original.src, plan.source)
        requested_name = edit.destination_name or source.name
        # An unchanged existing filename is already valid. Do not truncate it
        # while applying a folder move with rename suggestions disabled.
        destination_name = source.name if requested_name == source.name and planned_extension == source.suffix else (
            sanitize_windows_filename(requested_name, source.name, target_extension=planned_extension)
        )
        policy = _naming_policy(plan.chat_decisions, index) if (
            not edit.destination_name or edit.destination_name == Path(original.dest).name
        ) else None  # A newly typed per-file name is an explicit user override.
        try:
            destination = _unique_destination(
                plan.source.joinpath(category, subcategory, destination_name), taken, source, naming_policy=policy,
            )
        except NamingPolicyConflict as error:
            raise HTTPException(409, "Create a new preview and review the filename length/date constraints.") from error
        moves.append(
            Move(
                str(source),
                str(destination),
                category,
                original.reason,
                subcategory,
                not _same_path(destination, source),
                original.source_snapshot,
            )
        )
    return moves


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/providers/config")
def provider_config() -> dict:
    return provider_store.public_config()


@app.post("/api/providers/config")
def save_provider_config(request: ProviderConfigRequest) -> dict:
    try:
        connections = {
            provider: connection.model_dump()
            for provider, connection in request.connections.items()
        }
        return provider_store.save_config(
            request.primary,
            request.fallbacks,
            connections,
        )
    except provider_store.ProviderConfigurationError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/providers/models")
def provider_models(request: ModelDiscoveryRequest) -> dict:
    try:
        return provider_catalog.discover_models(
            request.provider,
            {
                "model": request.model,
                "base_url": request.base_url,
                "api_key": request.api_key,
            },
        )
    except provider_store.ProviderConfigurationError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(502, str(error)) from error


@app.post("/api/providers/test")
def test_provider_config() -> dict:
    try:
        results = provider_catalog.test_active_config()
    except provider_store.ProviderConfigurationError as error:
        raise HTTPException(400, str(error)) from error
    if not any(result["ok"] for result in results):
        return {"ok": False, "results": results}
    return {
        "ok": all(result["ok"] for result in results),
        "results": results,
    }


@app.post("/api/preview", response_model=PreviewResponse)
def preview(request: PreviewRequest) -> PreviewResponse:
    started = perf_counter()
    try:
        source = assert_safe_root(Path(request.source).expanduser())
    except (OSError, UnsafePathError) as error:
        raise HTTPException(400, str(error)) from error
    if not source.is_dir():
        raise HTTPException(400, "Choose an existing folder.")

    scan_started = perf_counter()
    files, scan_summary = scan_directory_with_report(
        str(source),
        recursive=request.recursive,
        policy=ScanPolicy.from_env(),
    )
    scan_seconds = perf_counter() - scan_started
    plan_warnings = []
    requires_confirmation = False
    analysis_stats = {}
    semantic_index = None
    if files:
        existing = sorted(
            item.name
            for item in source.iterdir()
            if item.is_dir()
            and not item.name.startswith(".")
            and not should_skip_directory(item)
        )
        try:
            active_models = provider_store.activate_runtime()
            result = plan_content_intelligence(
                files,
                str(source),
                active_models or resolve_models(request.models),
                known_folders=existing,
                use_cache=not request.reorganize,
                allow_rename=request.rename,
                allow_sensitive_cloud=request.allow_sensitive_cloud,
                instructions=request.instructions,
            )
            suggestions = result.suggestions
            moves = _content_moves(suggestions, source)
            sanity = evaluate_plan(suggestions)
            plan_warnings = list(sanity.warnings)
            requires_confirmation = sanity.requires_confirmation
            analysis_stats = dict(result.stats)
            analysis_stats["scan_seconds"] = round(scan_seconds, 4)
            semantic_index = getattr(result, "semantic_index", None)
            plan_warnings.extend(analysis_stats.get("analysis_warnings", []))
        except (RuntimeError, ValueError) as error:
            # Parsing and validation failures from model replies are provider-facing errors.
            raise HTTPException(502, str(error)[:600]) from error
    else:
        suggestions = []
        moves = []

    plan_id = str(uuid4())
    plans[plan_id] = StoredPlan(source=source, moves=moves, suggestions=suggestions,
                              semantic_index=semantic_index, organization_instructions=list(request.instructions),
                              analysis_stats=dict(analysis_stats), scan_summary={
        "files_seen": scan_summary.files_seen, "skipped": scan_summary.skipped,
        "reasons": scan_summary.reasons, "truncated": scan_summary.truncated,
    })
    return PreviewResponse(
        plan_id=plan_id,
        source=str(source),
        analysis_seconds=round(perf_counter() - started, 2),
        already_organized=sum(not move.selected and _same_path(move.src, move.dest) for move in moves),
        plan_warnings=plan_warnings,
        requires_confirmation=requires_confirmation,
        scan_summary={
            "files_seen": scan_summary.files_seen,
            "skipped": scan_summary.skipped,
            "reasons": scan_summary.reasons,
            "truncated": scan_summary.truncated,
        },
        analysis_stats=analysis_stats,
        files=[
            PlanItem(
                id=str(index),
                name=Path(move.src).name,
                current_path=str(Path(move.src).parent),
                destination_name=Path(move.dest).name,
                target_path=_target_path(move),
                reasoning=move.reason or "No reason provided",
                renamed=Path(move.src).name != Path(move.dest).name,
                selected=move.selected,
                category=suggestions[index].target_path,
                confidence=suggestions[index].confidence,
                evidence_excerpt=suggestions[index].evidence_excerpt,
                evidence_sources=suggestions[index].evidence_sources,
                warnings=suggestions[index].warnings,
                filename_conflict=suggestions[index].filename_conflict,
                evidence_sufficient=suggestions[index].sufficient_for_change,
                detected_type=suggestions[index].detected_type,
                extraction_status=suggestions[index].extraction_status,
                rename_reason=suggestions[index].rename_reason,
                sensitive=suggestions[index].sensitive,
            )
            for index, move in enumerate(moves)
        ],
    )


def _prepare_chat_names(plan: StoredPlan, result: chat_engine.ChatResult, context: dict, models: list[str], resolver) -> None:
    """Generate missing per-file proposals only after an explicit chat opt-in."""
    if not any(rule.action == "rename" and rule.naming.enabled is not None for rule in result.rules):
        return
    requested: set[int] = set()
    scopes = deepcopy(plan.moves)
    for rule in result.rules:
        topic_ids = set(resolver(rule.match.topic, context).ids) if rule.match.topic else set()
        for index, (move, suggestion) in enumerate(zip(scopes, plan.suggestions)):
            if (suggestion.sensitive or suggestion.requires_relationship_review
                    or suggestion.extraction_status == "unavailable"):
                continue
            relative = Path(move.src).relative_to(plan.source).as_posix()
            matched = (index in topic_ids if rule.match.topic else
                       chat_engine.matches_folder(move, rule.match.folder) if rule.match.folder else
                       Path(move.src).suffix.casefold() == rule.match.extension.casefold()
                       if rule.match.extension else
                       fnmatchcase(relative.casefold(), rule.match.glob.replace("\\", "/").casefold()))
            if matched:
                if rule.action == "move":
                    parts = rule.target.split("/")
                    move.folder, move.subfolder = parts[0], parts[1] if len(parts) == 2 else ""
                    continue
                if rule.action == "exclude":
                    requested.discard(index)
                    continue
                if rule.naming.enabled is None:
                    continue
                if rule.naming.enabled and not (rule.naming.generic_only and not chat_engine.is_generic_original(Path(move.src).name)):
                    if not suggestion.name_evidence or not suggestion.rename_reason.startswith("Grounded in evidence:"):
                        requested.add(index)
                else:
                    requested.discard(index)
    if not requested:
        return
    files = []
    ordered = sorted(requested)
    for index in ordered:
        move = plan.moves[index]
        try:
            verify_file_snapshot(Path(move.src), move.source_snapshot)
        except OSError as error:
            raise HTTPException(409, "A source changed; create a new preview before requesting filenames.") from error
        files.append(FileInfo(path=move.src, filename=Path(move.src).name, extension=Path(move.src).suffix,
                              size_kb=0, date_created="", source_snapshot=move.source_snapshot))
    generated = plan_content_intelligence(
        files, str(plan.source), models, allow_rename=True,
        known_folders=sorted({_target_path(move) for move in plan.moves}),
        instructions=plan.organization_instructions,
        semantic_config=SemanticConfig(enabled=False),
    )
    for index, candidate in zip(ordered, generated.suggestions):
        if candidate.sensitive or not candidate.name_evidence or not candidate.rename_reason.startswith("Grounded in evidence:"):
            continue
        suggestion, move = plan.suggestions[index], plan.moves[index]
        suggestion.suggested_name = sanitize_windows_filename(
            candidate.suggested_name, Path(move.src).name, target_extension=Path(move.dest).suffix,
        )
        suggestion.name_evidence = candidate.name_evidence
        suggestion.rename_reason = candidate.rename_reason
        plan.chat_decisions[f"naming-source:{index}"] = {
            "name": suggestion.suggested_name, "reason": suggestion.rename_reason,
        }


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    plan = plans.get(request.plan_id)
    if plan is None:
        raise HTTPException(404, "Preview expired. Create a new preview before chatting.")
    if not request.message.strip():
        raise HTTPException(422, "Enter a message.")
    # Serialize chat and Apply for this preview; no partially edited plan is exposed.
    with plan.lock:
        if plan.attempted:
            raise HTTPException(409, "Create a new preview before chatting about this folder.")
        snapshot = StoredPlan(
            source=plan.source, moves=deepcopy(plan.moves), suggestions=deepcopy(plan.suggestions),
            chat_rules=list(plan.chat_rules), chat_messages=list(plan.chat_messages),
            chat_decisions=dict(plan.chat_decisions), scan_summary=dict(plan.scan_summary),
            analysis_stats=dict(plan.analysis_stats),
            semantic_index=plan.semantic_index,
            organization_instructions=list(plan.organization_instructions),
            chat_topic_cache=dict(plan.chat_topic_cache), rules_revision=plan.rules_revision,
            pending_clarification=plan.pending_clarification,
        )
        if len(snapshot.moves) != len(snapshot.suggestions):
            raise HTTPException(409, "Create a new preview before chatting.")
        if request.files is not None:
            snapshot.moves = _apply_user_edits(snapshot, ApplyRequest(
                plan_id=request.plan_id, files=request.files,
            ))
        _validate_for_apply(snapshot)
        context = chat_engine.context_for(snapshot)
        prompt_context = parser_context(context, request.message, semantic_index=plan.semantic_index)
        try:
            models = provider_store.activate_runtime() or resolve_models(None)
            user_message = {"role": "user", "content": chat_engine.redact_sensitive(request.message)}
            result = chat_engine.call_json(models, [
                {"role": "system", "content": chat_engine.SYSTEM_PROMPT},
                {"role": "system", "content": (
                    "The context may contain representative intent-parsing samples. "
                    "Rules will be resolved against the entire selected dataset, not only these samples."
                )},
                *snapshot.chat_messages[-12:],
                {"role": "user", "content": json.dumps({"context": prompt_context,
                                                         "message": user_message["content"]})},
            ], chat_engine.ChatResult)
            response_preview = None
            if result.rules:
                resolver = lambda topic, complete_context: resolve_topic(
                    topic, complete_context, models, semantic_index=plan.semantic_index,
                    cache=snapshot.chat_topic_cache,
                )
                _prepare_chat_names(snapshot, result, context, models, resolver)
                moves, suggestions, conflict, affected, decisions = chat_engine.refine(
                    snapshot, result, context, models,
                    topic_resolver=resolver,
                )
                notes = []
                if not affected:
                    notes.append("No files in this preview matched those rules")
                elif conflict:
                    notes.append("Your newer instruction replaces an earlier instruction for the same files")
                if any(item.sensitive for item in suggestions):
                    notes.append("sensitive files were left unchanged")
                if notes:
                    result.reply = "; ".join(notes) + ". Review the preview before confirming."
                # Resolve collisions and validate destinations before storing the new preview.
                taken: set[str] = set()
                for index, move in enumerate(moves):
                    if move.selected:
                        if _same_path(move.dest, move.src):
                            move.selected = False
                        else:
                            try:
                                move.dest = str(_unique_destination(
                                    Path(move.dest), taken, Path(move.src), naming_policy=_naming_policy(decisions, index),
                                ))
                                suggestions[index].suggested_name = Path(move.dest).name
                            except NamingPolicyConflict:
                                move.selected = suggestions[index].selected = False
                                suggestions[index].warnings.append(
                                    "A filename collision cannot satisfy the length/date policy; review this file."
                                )
                new_id = str(uuid4())
                new_plan = StoredPlan(
                    source=plan.source, moves=moves, suggestions=suggestions,
                    chat_rules=[*snapshot.chat_rules, *result.rules], chat_decisions=decisions,
                    scan_summary=dict(plan.scan_summary),
                    analysis_stats=dict(plan.analysis_stats),
                    semantic_index=plan.semantic_index,
                    organization_instructions=list(plan.organization_instructions),
                    chat_topic_cache=dict(snapshot.chat_topic_cache), rules_revision=plan.rules_revision + 1,
                )
                _validate_for_apply(new_plan)
                sanity = evaluate_plan(suggestions)
                response_preview = PreviewResponse(
                    plan_id=new_id, source=str(plan.source), analysis_seconds=0,
                    already_organized=sum(not move.selected and _same_path(move.src, move.dest) for move in moves),
                    scan_summary=dict(plan.scan_summary),
                    analysis_stats=dict(plan.analysis_stats),
                    rules_revision=new_plan.rules_revision,
                    plan_warnings=list(sanity.warnings), requires_confirmation=sanity.requires_confirmation,
                    files=[PlanItem(
                        id=str(index), name=Path(move.src).name,
                        current_path=str(Path(move.src).parent), destination_name=Path(move.dest).name,
                        target_path=_target_path(move), reasoning=move.reason,
                        renamed=Path(move.src).name != Path(move.dest).name, selected=move.selected,
                        category=suggestion.target_path, confidence=suggestion.confidence,
                        evidence_excerpt=suggestion.evidence_excerpt, evidence_sources=suggestion.evidence_sources,
                        warnings=suggestion.warnings, filename_conflict=suggestion.filename_conflict,
                        evidence_sufficient=suggestion.sufficient_for_change,
                        detected_type=suggestion.detected_type, extraction_status=suggestion.extraction_status,
                        rename_reason=suggestion.rename_reason, sensitive=suggestion.sensitive,
                    ) for index, (move, suggestion) in enumerate(zip(moves, suggestions))],
                )
                plans[new_id] = new_plan
                destination_plan = new_plan
            else:
                destination_plan = plan
            if result.needs_clarification:
                destination_plan.pending_clarification = result.needs_clarification
            destination_plan.chat_messages = [
                *snapshot.chat_messages[-10:], user_message,
                {"role": "assistant", "content": result.model_dump_json()},
            ]
            return {"result": result.model_dump(),
                    "preview": response_preview.model_dump() if response_preview else None}
        except (RuntimeError, ValueError) as error:
            raise HTTPException(502, "Chat could not prepare a valid preview. Check Providers and try again.") from error


def _apply_plan(plan: StoredPlan, request: ApplyRequest) -> dict:
    with plan.lock:
        if plan.attempted:
            raise HTTPException(
                409,
                "This preview has already been used by an Apply attempt. Create a new preview.",
            )
        if plan.pending_clarification:
            raise HTTPException(409, "Resolve the chat clarification before applying this preview.")
        sanity = evaluate_plan(plan.suggestions)
        if sanity.requires_confirmation and not request.risk_acknowledged:
            raise HTTPException(409, "Review and acknowledge the unusual plan warnings before organizing.")

        # Mark the preview before touching disk so a concurrent request cannot
        # execute it a second time.
        plan.attempted = True
        plan.apply_state = "applying"
        plan.moves = _apply_user_edits(plan, request)
        for move in plan.moves:
            move.src = str(_resolve_plan_source(move.src, plan.source))

        _validate_for_apply(plan, allow_stale=True)
        requested = sum(1 for move in plan.moves if move.selected)
        result = execute_plan(
            plan.moves,
            str(plan.source),
            dry_run=False,
            source_root=str(plan.source),
        )
        plan.apply_state = result.state
        plan.applied = result.state == "success"
        if result.moved:
            history.remember_folder(str(plan.source))

        payload = {
            "status": result.state,
            "moved": result.moved,
            "requested": requested,
            "failed": result.failed,
            "failures": result.failures,
            "kept_in_root": sum(not move.selected for move in plan.moves),
            "undo_available": result.undo_available and result.moved > 0,
            "hierarchy": _hierarchy(plan.source, result),
        }
        if result.log_error:
            payload["detail"] = result.log_error
            return JSONResponse(status_code=207 if result.moved else 500, content=payload)
        if result.state == "partial":
            payload["detail"] = (
                f"{result.moved} file(s) moved; {result.failed} could not be moved. "
                "Review History or Undo before trying again."
            )
            return JSONResponse(status_code=207, content=payload)
        if result.state == "failed":
            payload["detail"] = (
                "No files were moved. "
                "Review the failure reasons, then create a new preview before trying again."
            )
            return JSONResponse(status_code=500, content=payload)
        return payload


@app.post("/api/apply")
def apply(request: ApplyRequest) -> dict:
    plan = plans.get(request.plan_id)
    if plan is None:
        raise HTTPException(404, "Preview expired. Create a new preview before organizing.")
    if not APPLY_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another Apply operation is already running.")
    try:
        return _apply_plan(plan, request)
    finally:
        APPLY_LOCK.release()


@app.post("/api/undo")
def undo(request: UndoRequest) -> dict:
    source = Path(request.source).expanduser().resolve()
    if not source.is_dir():
        raise HTTPException(400, "Choose an existing folder.")
    result = history.undo_last_run(str(source))
    if result is None:
        return {"status": "none", "restored": 0, "skipped": []}
    return {
        "status": result["status"],
        "restored": result["restored"],
        "skipped": result["skipped"],
    }


@app.post("/api/open-folder")
def open_folder(request: UndoRequest) -> dict:
    source = Path(request.source).expanduser().resolve()
    if not source.is_dir():
        raise HTTPException(400, "Choose an existing folder.")
    try:
        opener = "explorer.exe" if sys.platform == "win32" else "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(source)])
    except OSError as error:
        raise HTTPException(500, f"Could not open the folder: {error}") from error
    return {"opened": True}


@app.post("/api/choose-folder")
def choose_folder() -> dict:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.folder_picker"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as error:
        raise HTTPException(500, f"Could not open the folder picker: {error}") from error
    if result.returncode != 0:
        raise HTTPException(500, "Could not open the folder picker.")
    selected = result.stdout.strip()
    if not selected:
        raise HTTPException(400, "No folder was selected.")
    return {"source": selected}
