from __future__ import annotations

import copy
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    # overrides example: {"paths": {"input_dir": "/tmp/x"}} (used by tests and CLI flags)
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if overrides:
        cfg = _deep_merge(cfg, overrides)

    # make paths absolute so the pipeline works from any folder
    for key in ("input_dir", "expected_rejects", "db_path", "output_dir"):
        value = cfg["paths"].get(key)
        if value:
            p = Path(value)
            cfg["paths"][key] = p if p.is_absolute() else PROJECT_ROOT / p
    return cfg


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
