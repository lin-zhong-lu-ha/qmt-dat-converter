"""Machine-local paths, kept outside the tracked example configuration."""

import json
import os
from pathlib import Path
import tempfile

from .model import ConverterError


DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.local.json"


def resolve_local_path(value: str, config_path: Path) -> str:
    value = value.strip()
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(config_path).resolve().parent / path
    return str(path.resolve())


def _validate(values, path):
    if not isinstance(values, dict) or set(values) - {"source", "output", "period"}:
        raise ConverterError("invalid-config: only source, output and period are supported")
    result = {"source": "", "output": "", "period": "both", **values}
    if any(not isinstance(value, str) for value in result.values()):
        raise ConverterError("invalid-config: all values must be strings")
    if result["period"] not in {"1d", "1m", "both"}:
        raise ConverterError("invalid-config: period must be 1d, 1m or both")
    for field in ("source", "output"):
        result[field] = resolve_local_path(result[field], path)
    return result


def load_settings(path: Path = DEFAULT_CONFIG, *, required=False) -> dict:
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        if required:
            raise ConverterError(f"config-not-found: {path}") from exc
        return {"source": "", "output": "", "period": "both"}
    except (OSError, UnicodeError) as exc:
        raise ConverterError(f"config-read-failed: {path}: {exc}") from exc
    try:
        return _validate(json.loads(raw), path)
    except (ValueError, OSError) as exc:
        raise ConverterError(f"invalid-config: {path}: {exc}") from exc


def save_settings(path: Path = DEFAULT_CONFIG, *, source: str, output: str, period: str) -> None:
    path = Path(path).resolve()
    temporary = None
    try:
        values = _validate({"source": source, "output": output, "period": period}, path)
        if not values["source"] or not values["output"]:
            raise ConverterError("invalid-config: source and output must not be empty")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(values, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError) as exc:
        raise ConverterError(f"config-save-failed: {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
