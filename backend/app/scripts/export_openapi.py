"""Write the OpenAPI document to disk.

The frontend and `deviceagent` API clients are **generated** from this file and
never hand-written — that is what makes the cross-repo submodule splits safe,
because the contract is then machine-checked rather than maintained by hand in
three places.

    python -m app.scripts.export_openapi [output_path]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import app

DEFAULT_OUTPUT = Path("openapi.json")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    output = Path(args[0]) if args else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
