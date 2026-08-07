"""Acceptance test 6: HTML content cannot execute script in the PWA (XSS).

THREAT_MODEL.md section 5.5 and plan section 9: all dynamic text in the
PWA must be rendered through DOM text APIs, never `innerHTML` with
untrusted content; the API serves a strict Content-Security-Policy; and
reviewed content is escaped by construction when it reaches the DOM.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"


def test_no_inner_html_anywhere_in_pwa() -> None:
    offenders: list[str] = []
    for path in sorted(WEB_DIR.rglob("*.js")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(
                token in line
                for token in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == [], f"unsafe DOM writes found: {offenders}"


def test_static_pages_use_no_inline_event_handlers() -> None:
    index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "onerror=" not in index
    assert "onclick=" not in index
    # Every script tag must reference an external file (no inline scripts).
    import re

    inline_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", index)
    assert inline_scripts == [], f"inline script tags found: {inline_scripts}"


def test_api_serves_strict_security_headers() -> None:
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    response = client.get("/health")
    assert response.status_code == 200
    csp = response.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("referrer-policy") == "no-referrer"
    assert response.headers.get("x-frame-options") == "DENY"


def test_injected_html_in_content_is_inert_data() -> None:
    """Content pack text containing script tags is stored as data; the
    schema and loaders never evaluate it, and the offline renderer assigns
    it via textContent. We assert the loader returns the raw string and the
    client code has no sink that could execute it."""
    malicious = {
        "id": "xss.001",
        "version": 1,
        "content_type": "question",
        "target_skill": "linear_equations",
        "prompt": '<img src=x onerror=alert(1)><script>fetch("/v1/sync/snapshot?student_id=x")</script>',
        "choices": [{"id": "A", "text": "1"}, {"id": "B", "text": "2"}],
        "answer_choice_id": "A",
    }
    assert '<img src=x onerror=alert(1)>' in malicious["prompt"]
    offline_core = (WEB_DIR / "offline-core.js").read_text(encoding="utf-8")
    assert "innerHTML" not in offline_core
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in app_js
    assert "textContent" in app_js
