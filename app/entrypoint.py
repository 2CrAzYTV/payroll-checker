from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi.responses import FileResponse

from app import server

ASSETS_DIR = Path("/app/assets")


@server.main.app.get("/favicon.ico", include_in_schema=False)
def favicon_ico() -> FileResponse:
    return FileResponse(ASSETS_DIR / "favicon.svg", media_type="image/svg+xml")


@server.main.app.get("/favicon.svg", include_in_schema=False)
def favicon_svg() -> FileResponse:
    return FileResponse(ASSETS_DIR / "favicon.svg", media_type="image/svg+xml")


if __name__ == "__main__":
    uvicorn.run(server.main.app, host="0.0.0.0", port=8788, access_log=False)
