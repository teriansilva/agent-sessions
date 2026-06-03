"""Upload route (agent-sessions#265): save a pasted/dropped file to the shared
uploads dir so an agent session can read it by path. Moved verbatim from
``main.create_app``.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.post("/api/upload")
    async def upload_context(
        file: UploadFile = File(...),
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Save a pasted/dropped image or file so a Claude/opencode session can read
        # it by path (the web terminal can't carry image paste itself). Lands in a
        # shared ~/.agent-sessions/uploads/ — never in a project working tree.
        # Stream-read with a hard cap so a huge upload can't exhaust memory.
        max_bytes = 25 * 1024 * 1024
        size, chunks = 0, []
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail="file too large (max 25 MB)")
            chunks.append(chunk)
        if not size:
            raise HTTPException(status_code=422, detail="empty upload")
        # Sanitise to a bare, safe basename — no path separators, no traversal.
        raw_name = Path(file.filename or "upload").name
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name)[:80] or "upload"
        dest_dir = Path.home() / ".agent-sessions" / "uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"{stamp}-{safe}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stamp}-{n}-{safe}"
            n += 1
        dest.write_bytes(b"".join(chunks))
        return JSONResponse({"path": str(dest), "name": safe})
