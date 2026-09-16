"""Load a scanner package's ``scan.py`` in isolation.

Senpi scanners do ``import scoring`` for their sibling module, and every package names it the
same. Each load therefore runs with the scanner directory at the front of ``sys.path`` and
evicts the modules it imported from ``sys.modules`` afterwards, so two packages never share a
``scoring``. The loaded module keeps its own references and works normally.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ScanFn = Callable[[dict[str, Any], Any], Any]


class ScannerLoadError(RuntimeError):
    pass


def load_scan(scanner_dir: Path, entrypoint: str = "scan.py", *, alias: str = "") -> ScanFn:
    scanner_dir = scanner_dir.resolve()
    path = scanner_dir / entrypoint
    if not path.is_file():
        raise ScannerLoadError(f"{path} not found")
    mod_name = f"_hl_scanner_{alias or scanner_dir.name}_{abs(hash(str(path)))}"

    before = set(sys.modules)
    sys.path.insert(0, str(scanner_dir))
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise ScannerLoadError(f"cannot create import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ScannerLoadError(f"{path}: {exc!r}") from exc
    finally:
        sys.path.remove(str(scanner_dir))
        for name in set(sys.modules) - before:
            file = getattr(sys.modules[name], "__file__", None)
            if file and Path(file).resolve().is_relative_to(scanner_dir):
                del sys.modules[name]

    fn = getattr(module, "scan", None)
    if not callable(fn):
        raise ScannerLoadError(f"{path} does not export scan(inputs, ctx)")
    return fn  # type: ignore[no-any-return]
