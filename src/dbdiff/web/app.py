"""FastAPI application: browse run history and drill into a run's diff.

Compute happens in a background worker thread (a run can take seconds while
MariaDB spins up and dumps restore); progress lines are buffered in memory and
polled by the run page over HTMX. Everything browsable comes from the SQLite
store, so the UI never recomputes.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..engine import run_diff
from ..sources import redact, safe_label
from ..store import Store

HERE = Path(__file__).parent


class Job:
    """In-memory progress buffer for a running diff."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        with self._lock:
            self._lines.append(msg)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)


def create_app(store_path: str, config_path: str | None = None) -> FastAPI:
    app = FastAPI(title="dbdiff")
    store = Store(store_path)
    store.init()
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["disp"] = _disp
    templates.env.filters["fromjson"] = json.loads
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    jobs: dict[int, Job] = {}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {"runs": store.list_runs(), "default_config": config_path or ""},
        )

    @app.post("/runs")
    def start_run(base: str = Form(...), new: str = Form(...), config: str = Form("")):
        cfg_path = (config or config_path) or None
        cfg = Config.load(cfg_path)
        run_id = store.create_run(safe_label(base), safe_label(new), redact(base), redact(new))
        job = Job()
        jobs[run_id] = job

        def worker() -> None:
            try:
                run_diff(base, new, cfg, store, log=job.log, run_id=run_id)
            except Exception:  # noqa: BLE001 — already recorded on the run row
                pass

        threading.Thread(target=worker, daemon=True).start()
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int):
        run = store.get_run(run_id)
        if not run:
            return HTMLResponse("run not found", status_code=404)
        ctx: dict = {"run": run}
        if run["status"] != "running":
            ctx["summary"] = json.loads(run["summary_json"]) if run["summary_json"] else None
            ctx["schema_changes"] = [_schema_change(s) for s in store.get_schema_changes(run_id)]
            ctx["tables"] = store.get_table_results(run_id)
        return templates.TemplateResponse(request, "run.html", ctx)

    @app.post("/runs/{run_id}/delete")
    def delete_run(run_id: int):
        store.delete_run(run_id)
        jobs.pop(run_id, None)
        return HTMLResponse("", headers={"HX-Redirect": "/"})

    @app.get("/runs/{run_id}/status", response_class=HTMLResponse)
    def run_status(request: Request, run_id: int):
        run = store.get_run(run_id)
        if not run:
            return HTMLResponse("", status_code=404)
        if run["status"] == "running":
            job = jobs.get(run_id)
            lines = job.snapshot()[-80:] if job else []
            return templates.TemplateResponse(
                request, "_progress.html", {"run_id": run_id, "lines": lines}
            )
        # finished — tell HTMX to reload the page so the overview renders
        return HTMLResponse("", headers={"HX-Refresh": "true"})

    @app.get("/runs/{run_id}/tables/{table}", response_class=HTMLResponse)
    def table_page(request: Request, run_id: int, table: str):
        tr = _table_result(store, run_id, table)
        if not tr:
            return HTMLResponse("table not found", status_code=404)
        counts = {"inserted": tr["inserted"], "modified": tr["modified"], "deleted": tr["deleted"]}
        default = (
            "modified" if counts["modified"] else "inserted" if counts["inserted"] else "deleted"
        )
        return templates.TemplateResponse(
            request,
            "table.html",
            {
                "run_id": run_id,
                "table": table,
                "tr": tr,
                "counts": counts,
                "default": default,
            },
        )

    @app.get("/runs/{run_id}/tables/{table}/rows", response_class=HTMLResponse)
    def table_rows(
        request: Request,
        run_id: int,
        table: str,
        type: str = "modified",
        offset: int = 0,
        limit: int = 50,
    ):
        total = store.count_row_changes(run_id, table, type)
        rows = store.get_row_changes(run_id, table, type, limit=limit, offset=offset)
        view = _build_view(type, rows)
        page = {
            "offset": offset,
            "limit": limit,
            "total": total,
            "shown_from": offset + 1 if total else 0,
            "shown_to": min(offset + limit, total),
            "has_prev": offset > 0,
            "has_next": offset + limit < total,
            "prev_offset": max(0, offset - limit),
            "next_offset": offset + limit,
        }
        return templates.TemplateResponse(
            request,
            "_rows.html",
            {
                "run_id": run_id,
                "table": table,
                "type": type,
                "view": view,
                "page": page,
            },
        )

    return app


# ---- helpers -----------------------------------------------------------------


def _build_view(change_type: str, rows: list[dict]) -> dict:
    if change_type == "modified":
        mods = [
            {
                "key": r["key"],
                "changes": [(c, _at(r["old"], c), _at(r["new"], c)) for c in r["changed"]],
            }
            for r in rows
        ]
        return {"type": "modified", "mods": mods}

    side = "new" if change_type == "inserted" else "old"
    data = [r[side] or {} for r in rows]
    columns = list(data[0].keys()) if data else []
    return {"type": change_type, "columns": columns, "rows": data}


def _table_result(store: Store, run_id: int, table: str) -> dict | None:
    for tr in store.get_table_results(run_id):
        if tr["table_name"] == table:
            return tr
    return None


def _schema_change(s: dict) -> dict:
    detail = json.loads(s["detail_json"]) if s["detail_json"] else None
    return {"kind": s["change_kind"], "table": s["table_name"], "detail": detail}


def _at(row: dict | None, col: str):
    return row.get(col) if row else None


def _disp(v) -> str:
    if v is None:
        return "∅"
    if v == "":
        return "″″"
    return str(v)
