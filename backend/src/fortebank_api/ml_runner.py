"""Private child entry point for a trusted synchronous local ML callable."""

from contextlib import redirect_stdout
from dataclasses import asdict, is_dataclass
import importlib
import json
from pathlib import Path
import sys


def main() -> int:
    try:
        module_name, function_name = sys.argv[1].split(":", 1)
        # Prints from imports/inference must never corrupt the result protocol.
        with redirect_stdout(sys.stderr):
            function = getattr(importlib.import_module(module_name), function_name)
            result = function(Path(sys.argv[2]))
            if is_dataclass(result) and not isinstance(result, type):
                result = asdict(result)
            payload = json.dumps(result, allow_nan=False, ensure_ascii=False)
        sys.stdout.write(payload)
        return 0
    except Exception:
        # Deliberately no traceback: model exceptions may contain private observations.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
