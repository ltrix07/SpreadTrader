from __future__ import annotations

from pathlib import Path


def _patch_http_cookiejar() -> None:
    try:
        import http
    except Exception:
        return

    patch_dir = Path(__file__).resolve().parent / "_stdlib_patches" / "http"
    if not patch_dir.exists():
        return

    http_path = getattr(http, "__path__", None)
    if http_path is None:
        return

    patch_path = str(patch_dir)
    if patch_path not in http_path:
        http_path.append(patch_path)


_patch_http_cookiejar()
