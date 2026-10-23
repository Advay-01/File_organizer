"""History endpoints for the React app. Mount once in app/api.py:

    from app.history_api import router as history_router
    app.include_router(history_router)

GET  /history                      runs from every remembered folder (each has a `folder`)
GET  /history?folder=<path>        runs for one output folder
GET  /history/folders              remembered output folders
GET  /history/{run_id}?folder=...  every move of one run (from_name -> to_name, folder, renamed)
POST /history/{run_id}/undo        body {"folder": "<path>"}; safe, never overwrites

After your confirm/apply endpoint succeeds, call `history.remember_folder(output_folder)` so the
folder appears in the global list. All logic lives in app/core/history.py (fully unit-tested).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import history

router = APIRouter(prefix="/history", tags=["history"])


class UndoRequest(BaseModel):
    folder: str


def _folder(folder: str) -> str:
    """A folder is valid only if it exists and has recorded runs (no probing arbitrary paths)."""
    p = Path(folder)
    if not p.is_dir() or not (p / history.UNDO_LOG).is_file():
        raise HTTPException(status_code=404, detail="No history recorded for that folder")
    return str(p)


@router.get("/folders")
def folders() -> list:
    return history.known_folders()


@router.get("")
def runs(folder: str | None = None) -> list:
    return history.list_runs(_folder(folder)) if folder else history.all_runs()


@router.get("/{run_id}")
def run_detail(run_id: int, folder: str) -> dict:
    try:
        return history.get_run(_folder(folder), run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such run")


@router.post("/{run_id}/undo")
def undo(run_id: int, body: UndoRequest) -> dict:
    try:
        return history.undo_run(_folder(body.folder), run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such run")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

