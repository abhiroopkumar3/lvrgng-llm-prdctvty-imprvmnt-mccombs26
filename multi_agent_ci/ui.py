# ui.py
# FastAPI server. All pipeline I/O flows through these endpoints.
# SSE streaming for real-time pipeline events.
# Serves index.html as the single-page UI.

import os
import uuid
import json
import asyncio
import logging
import threading
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import BRIEF_MD

logger = logging.getLogger(__name__)

app = FastAPI(title="Multi-Agent CI System", version="1.0.0")

# ─── In-memory run registry ───────────────────────────────────────────────────
_runs: dict[str, dict] = {}


# ─── Request/Response models ──────────────────────────────────────────────────

class StartRequest(BaseModel):
    company: str

class HumanValidateRequest(BaseModel):
    action: str
    reason: Optional[str] = ""


# ─── Serve index.html ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>index.html not found</h1>", status_code=404)


# ─── POST /start ──────────────────────────────────────────────────────────────

@app.post("/start")
async def start_pipeline(req: StartRequest):
    company = req.company.strip()
    if not company:
        raise HTTPException(status_code=400, detail="Company name cannot be empty")

    run_id = str(uuid.uuid4())[:8]
    _runs[run_id] = {
        "state":     None,
        "thread":    None,
        "completed": False,
        "company":   company,
        "brief":     "",
        "draft":     "",
    }

    from agents import ui_agent_intake, run_pipeline, get_event_queue, _event_queues

    def _run():
        try:
            _event_queues[run_id] = []
            from agents import push_event

            state = ui_agent_intake(company, run_id=run_id)
            if state is None:
                push_event(run_id, {
                    "type":      "error",
                    "message":   f"Company '{company}' could not be validated.",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                _runs[run_id]["completed"] = True
                return

            _runs[run_id]["state"] = state
            state["_run_id"] = run_id  # type: ignore

            push_event(run_id, {
                "type":      "log",
                "agent":     "ui_intake",
                "event":     "pipeline_initialized",
                "message":   f"Pipeline started for: {state['company']}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            final_state = run_pipeline(state)

            # Robustly extract brief/draft from whatever LangGraph returns
            brief = ""
            draft = ""

            if isinstance(final_state, dict):
                brief = final_state.get("brief", "") or ""
                draft = final_state.get("draft", "") or ""

            # Also check original state object (mutated in-place by LangGraph)
            if not brief and not draft:
                brief = state.get("brief", "") or ""
                draft = state.get("draft", "") or ""

            # Cache at top level for instant retrieval
            _runs[run_id]["brief"]     = brief
            _runs[run_id]["draft"]     = draft
            _runs[run_id]["state"]     = final_state if isinstance(final_state, dict) else state
            _runs[run_id]["completed"] = True

            logger.info(f"Pipeline complete for {run_id} | brief={len(brief)}chars | draft={len(draft)}chars")

        except Exception as e:
            logger.exception(f"Pipeline error for run {run_id}: {e}")
            if _runs[run_id].get("state"):
                s = _runs[run_id]["state"]
                if isinstance(s, dict):
                    _runs[run_id]["brief"] = s.get("brief", "") or ""
                    _runs[run_id]["draft"] = s.get("draft", "") or ""
            from agents import push_event
            push_event(run_id, {
                "type":      "error",
                "message":   f"Pipeline error: {str(e)}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            _runs[run_id]["completed"] = True

    t = threading.Thread(target=_run, daemon=True)
    _runs[run_id]["thread"] = t
    t.start()

    return {"run_id": run_id, "status": "started", "company": company}


# ─── GET /stream/{run_id} ─────────────────────────────────────────────────────

@app.get("/stream/{run_id}")
async def stream_events(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run ID not found")

    from agents import get_event_queue

    async def event_generator():
        last_index = 0
        max_empty_polls = 3000

        for _ in range(max_empty_polls):
            q = get_event_queue(run_id)
            while last_index < len(q):
                event = q[last_index]
                last_index += 1
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "pipeline_complete":
                    yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                    return

            if _runs[run_id].get("completed") and last_index >= len(get_event_queue(run_id)):
                yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                return

            await asyncio.sleep(0.1)

        yield f"data: {json.dumps({'type': 'stream_timeout'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ─── POST /human_validate/{run_id} ────────────────────────────────────────────

@app.post("/human_validate/{run_id}")
async def human_validate(run_id: str, req: HumanValidateRequest):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run ID not found")
    from agents import resolve_human_validation
    result = resolve_human_validation(run_id, req.action, req.reason or "")
    return {"status": result.get("status", req.action), "hv_retries": result.get("hv_retries", 0), "run_id": run_id}


# ─── GET /download/{run_id} ───────────────────────────────────────────────────

@app.get("/download/{run_id}")
async def download_pdf(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run ID not found")

    run     = _runs[run_id]
    brief   = run.get("brief", "") or (run.get("state") or {}).get("brief", "") or ""
    draft   = run.get("draft", "") or (run.get("state") or {}).get("draft", "") or ""
    content = brief if brief else draft
    company = run.get("company", "company")

    if not content:
        raise HTTPException(status_code=404, detail="Brief not yet available")

    timestamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_company = "".join(c if c.isalnum() or c in "_-" else "_" for c in company)
    filename     = f"{safe_company}_{timestamp}.pdf"
    pdf_path = os.path.join(tempfile.gettempdir(), filename)

    try:
        _generate_pdf_reportlab(content, pdf_path, company)
    except Exception as e:
        logger.warning(f"reportlab failed: {e}")
        txt_path = pdf_path.replace(".pdf", ".txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(content)
        return FileResponse(path=txt_path, filename=filename.replace(".pdf", ".txt"), media_type="text/plain")

    return FileResponse(path=pdf_path, filename=filename, media_type="application/pdf")


def _generate_pdf_reportlab(brief: str, output_path: str, company: str):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    doc = SimpleDocTemplate(output_path, pagesize=letter,
        rightMargin=inch, leftMargin=inch, topMargin=inch, bottomMargin=inch)
    styles      = getSampleStyleSheet()
    title_style = ParagraphStyle('T', parent=styles['Heading1'], fontSize=16,
        textColor=colors.HexColor('#1a1a2e'), spaceAfter=12)
    h2_style    = ParagraphStyle('H2', parent=styles['Heading2'], fontSize=13,
        textColor=colors.HexColor('#16213e'), spaceAfter=8, spaceBefore=16)
    body_style  = ParagraphStyle('B', parent=styles['Normal'], fontSize=10, leading=14, spaceAfter=6)

    story = []
    for line in brief.split('\n'):
        line = line.strip()
        if not line:              story.append(Spacer(1, 6))
        elif line.startswith('# '):  story.append(Paragraph(line[2:], title_style))
        elif line.startswith('## '): story.append(Paragraph(line[3:], h2_style))
        elif line.startswith('---'): story.append(Spacer(1, 12))
        else:
            line = line.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
            story.append(Paragraph(line, body_style))
    doc.build(story)


# ─── GET /audit/{run_id} ──────────────────────────────────────────────────────

@app.get("/audit/{run_id}")
async def get_audit(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run ID not found")
    state = _runs[run_id].get("state") or {}
    state = state if isinstance(state, dict) else {}
    audit = state.get("audit", [])
    context_content = ""
    try:
        with open(BRIEF_MD.replace("BRIEF.md", "CONTEXT.md"), encoding="utf-8") as f:
            context_content = f.read()
    except Exception:
        pass
    return {
        "run_id":        run_id,
        "company":       _runs[run_id].get("company", ""),
        "completed":     _runs[run_id].get("completed", False),
        "audit_entries": len(audit),
        "audit":         audit,
        "context_md":    context_content,
    }


# ─── GET /status/{run_id} ─────────────────────────────────────────────────────

@app.get("/status/{run_id}")
async def get_status(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run ID not found")
    run   = _runs[run_id]
    state = run.get("state") or {}
    state = state if isinstance(state, dict) else {}

    brief = run.get("brief", "") or state.get("brief", "") or ""
    draft = run.get("draft", "") or state.get("draft", "") or ""
    has_content = bool(brief) or bool(draft)

    return {
        "run_id":    run_id,
        "company":   run.get("company", ""),
        "completed": run.get("completed", False),
        "retries":   state.get("retries", 0),
        "has_brief": has_content,
        "error":     state.get("error", ""),
        "stage": (
            "complete"    if has_content             else
            "validating"  if state.get("draft")      else
            "writing"     if state.get("research")   else
            "researching" if state.get("plan")        else
            "planning"    if state.get("company")     else
            "initializing"
        ),
    }


# ─── GET /brief/{run_id} ──────────────────────────────────────────────────────

@app.get("/brief/{run_id}")
async def get_brief(run_id: str):
    """Return the final brief text directly."""
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run ID not found")
    run   = _runs[run_id]
    state = run.get("state") or {}
    state = state if isinstance(state, dict) else {}

    brief   = run.get("brief", "") or state.get("brief", "") or ""
    draft   = run.get("draft", "") or state.get("draft", "") or ""
    content = brief if brief else draft

    return {"run_id": run_id, "brief": content, "has_brief": bool(content)}
