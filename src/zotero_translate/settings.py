"""Settings for the translation service and the paper task.

The pipeline model has two endpoints (ADR 0004), each with its own
environment file outside the repository:

- flash (``model.env``): translation batches, review, algorithms and
  paragraph redos; ``DEFAULT_MODEL``, 40 requests in flight;
- argon (``argon.env``): the paper dossier, the term list, the promoted
  glosses request, and flash requests that timed out twice in a row;
  ``DEFAULT_ARGON_MODEL``, ``DEFAULT_ARGON_CONCURRENCY`` requests in flight.

Both files hold ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY``, and optionally
``AI_DEFAULT_MODEL`` and ``ZT_REQUEST_TIMEOUT_SECONDS``. The argon file may
also set ``ZT_CONCURRENCY``, its limit of requests in flight; flash's limit
comes from the ``--concurrency`` option. The plugin cannot change any of
these settings.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

DEFAULT_MODEL = "gemini-3.8-flash-high"
DEFAULT_CONCURRENCY = 40
DEFAULT_ARGON_MODEL = "gemini-4-argon-high"
# Below the 19 of 40 concurrent requests that argon answered in two probes.
DEFAULT_ARGON_CONCURRENCY = 16
DEFAULT_PORT = 8920
DEFAULT_DATA_DIR = Path(
    os.environ.get(
        "ZT_DATA_DIR", Path.home() / "Library" / "Application Support" / "zotero-translate"
    )
)
DEFAULT_ENV_FILE = Path(
    os.environ.get("ZT_ENV_FILE", Path.home() / ".config" / "zotero-translate" / "model.env")
)
DEFAULT_ARGON_ENV_FILE = Path(
    os.environ.get(
        "ZT_ARGON_ENV_FILE", Path.home() / ".config" / "zotero-translate" / "argon.env"
    )
)

_ENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class SettingsError(Exception):
    """The environment file is missing or incomplete."""


@dataclass(frozen=True)
class ModelSettings:
    """How the pipeline model client calls the pipeline model."""

    base_url: str
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    concurrency: int = DEFAULT_CONCURRENCY
    reasoning_effort: str = "high"
    temperature: float = 1.0
    timeout_seconds: float = 480.0  # whole request, from sending to the last byte read
    max_retries: int = 3  # per request, after the first attempt (ADR 0002)
    retry_backoff_seconds: float = 1.0  # first wait; doubles on each retry
    # The waits before the resends of a request that could not reach the
    # endpoint: together about a minute, the network outage of closure-02.
    connection_retry_seconds: tuple[float, ...] = (5.0, 15.0, 45.0)
    cache_dir: Path | None = None  # successful responses; None turns the cache off


def read_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ignore blank lines and comments."""
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match:
            value = match.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[match.group(1)] = value
    return values


@dataclass(frozen=True)
class PipelineSettings:
    """The two endpoints of the pipeline model (ADR 0004)."""

    flash: ModelSettings
    argon: ModelSettings


def load_pipeline_settings(
    env_file: Path,
    argon_env_file: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    cache_dir: Path | None = None,
) -> PipelineSettings:
    """Read both environment files; raise ``SettingsError`` if either is missing or incomplete."""
    return PipelineSettings(
        flash=load_model_settings(env_file, concurrency, cache_dir),
        argon=load_argon_settings(argon_env_file, cache_dir),
    )


def load_model_settings(
    env_file: Path, concurrency: int = DEFAULT_CONCURRENCY, cache_dir: Path | None = None
) -> ModelSettings:
    """Flash: read ``OPENAI_BASE_URL``, ``OPENAI_API_KEY`` and ``AI_DEFAULT_MODEL``.

    The optional ``ZT_REQUEST_TIMEOUT_SECONDS`` replaces the 480-second request
    timeout; tests use it to make timeouts happen in a few seconds.
    """
    values = _read_endpoint_file(env_file, "找不到管线模型的环境文件")
    return _endpoint_settings(env_file, values, DEFAULT_MODEL, concurrency, cache_dir)


def load_argon_settings(env_file: Path, cache_dir: Path | None = None) -> ModelSettings:
    """Argon: like ``load_model_settings``; ``ZT_CONCURRENCY`` replaces the limit of 16."""
    values = _read_endpoint_file(env_file, f"找不到 argon（{DEFAULT_ARGON_MODEL}）的环境文件")
    concurrency = DEFAULT_ARGON_CONCURRENCY
    if values.get("ZT_CONCURRENCY"):
        try:
            concurrency = int(values["ZT_CONCURRENCY"])
        except ValueError:
            concurrency = 0
        if concurrency < 1:
            raise SettingsError(f"环境文件 {env_file} 中的 ZT_CONCURRENCY 不是正整数")
    return _endpoint_settings(env_file, values, DEFAULT_ARGON_MODEL, concurrency, cache_dir)


def _read_endpoint_file(env_file: Path, missing_reason: str) -> dict[str, str]:
    env_file = Path(env_file)
    if not env_file.is_file():
        raise SettingsError(f"{missing_reason}：{env_file}")
    values = read_env_file(env_file)
    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if not values.get(k)]
    if missing:
        raise SettingsError(f"环境文件 {env_file} 缺少：{', '.join(missing)}")
    return values


def _endpoint_settings(
    env_file: Path,
    values: dict[str, str],
    default_model: str,
    concurrency: int,
    cache_dir: Path | None,
) -> ModelSettings:
    timeout_seconds = ModelSettings.timeout_seconds
    if values.get("ZT_REQUEST_TIMEOUT_SECONDS"):
        timeout_seconds = _positive_seconds(values["ZT_REQUEST_TIMEOUT_SECONDS"])
        if timeout_seconds is None:
            raise SettingsError(f"环境文件 {env_file} 中的 ZT_REQUEST_TIMEOUT_SECONDS 不是正数")
    return ModelSettings(
        base_url=values["OPENAI_BASE_URL"].rstrip("/"),
        api_key=values["OPENAI_API_KEY"],
        model=values.get("AI_DEFAULT_MODEL") or default_model,
        concurrency=concurrency,
        timeout_seconds=timeout_seconds,
        cache_dir=Path(cache_dir) if cache_dir else None,
    )


def _positive_seconds(text: str) -> float | None:
    try:
        seconds = float(text)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def model_cache_dir(data_dir: Path) -> Path:
    """Where the response cache lives inside a data directory."""
    return Path(data_dir) / "model-cache"
