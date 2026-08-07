"""Acceptance test 7: secrets are absent from repository and built client
assets.

THREAT_MODEL.md section 5.9: no keys in the repository or client bundle;
environment variables hold secrets; optional integrations are disabled
when keys are absent.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

HIGH_CONFIDENCE_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),  # GitHub PAT
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # OpenAI-style key
    re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"][A-Za-z0-9._\-]{12,}['\"]"),
]

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "bridgesat_agent.egg-info",
}
SKIP_SUFFIXES = {".pyc", ".db", ".sqlite", ".part", ".png", ".jpg", ".svg", ".woff2", ".jsonl", ".csv"}


def _tracked_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def test_no_private_key_material_in_repo() -> None:
    matches = _scan(re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"))
    assert matches == [], f"private key material found in: {matches}"


def test_no_high_confidence_api_key_patterns_in_repo() -> None:
    matches = _scan(re.compile(r"\b(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,})\b"))
    assert matches == [], f"possible API keys found in: {matches}"


def test_no_hardcoded_credentials_in_web_assets() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "web").rglob("*.js")) + sorted((ROOT / "web").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for pattern in HIGH_CONFIDENCE_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)} matches {pattern.pattern[:30]}...")
    assert offenders == [], f"hardcoded credential-like values in client assets: {offenders}"


def test_optional_integrations_require_environment_keys() -> None:
    """Mnemis defaults to an unconfigured transport: with no environment
    key set, the adapter must be inert (enhanced mode off by default)."""
    import app.memory.mnemis_backend as mnemis

    assert mnemis.DEFAULT_BASE_URL == "http://localhost:8010"
    assert mnemis.DEFAULT_API_KEY == ""
    adapter = mnemis.MnemisMemoryAdapter()
    assert adapter.api_key == ""


def _scan(pattern: re.Pattern) -> list[str]:
    matches: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            matches.append(str(path.relative_to(ROOT)))
    return matches
