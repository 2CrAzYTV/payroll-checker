from __future__ import annotations

import uvicorn

from app import main
from app.payroll_parser import parse_payroll_text

# Patch the parser used by the upload endpoint without changing persisted data structures.
main.parse_payroll_text = parse_payroll_text
main.APP_VERSION = "0.2.1"
main.app.version = "0.2.1"


if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=8788, access_log=False)
