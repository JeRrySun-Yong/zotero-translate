"""Settings for the translation service and the paper task.

The pipeline model has two endpoints (ADR 0004), each with its own
environment file outside the repository:

- flash (``model.env``): translation batches, review, algorithms and
  paragraph redos; ``DEFAULT_MODEL``, 40 requests in flight;
- argon (``argon.env``): the paper dossier, the term list, the promoted
  glosses request, the copies of slow flash sends (hedging) and flash
  requests that timed out;
  ``DEFAULT_ARGON_MODEL``, ``DEFAULT_ARGON_CONCURRENCY`` requests in flight.

Both files hold ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY``, and optionally
``AI_DEFAULT_MODEL`` and ``ZT_REQUEST_TIMEOUT_SECONDS``. The argon file may
also set ``ZT_CONCURRENCY``, its limit of requests in flight; flash's limit
comes from the ``--concurrency`` option. When both files name the same
address and key, the two are one endpoint with one budget of flash's limit,
and argon's ``ZT_CONCURRENCY`` is ignored (``PipelineModels``). The plugin
cannot change any of these settings: they are the built-in model profile,
named "Gemini" unless ``model.env`` (flash's file, ``--env-file``) says
otherwise with ``ZT_PROFILE_NAME`` (M5 ticket 02; ``load_builtin_profile_name``).
Only the display name changes, never the profile's id ``gemini`` or a
model request; a name a user profile's name could not be (over 60
characters, a control character) makes ``serve`` and ``run`` exit at
startup. Only those two read it: a service shows the name it read when it
started (a worker gets it as ``--profile-name``), so a later edit of the
file fails no task and no listing. A user profile (ADR 0005,
``zotero_translate.profiles``) is one such file for both roles, in
``DEFAULT_PROFILES_DIR`` (``load_profile_settings``).

``ZT_STREAM=1`` in an endpoint file (M4 ticket 12) makes its requests
streamed (``ModelSettings.stream``); ``0`` or no line, as for Gemini's two
files, leaves them as they were. It is a user profile's setting: Command
Code's DeepSeek Fast answers a plain request that thinks longer than about
125 seconds with HTTP 524. ``run`` reads it from the files it is given.

A user profile's tasks append the prompt rules for user profiles to their
translation batch and review prompts (M4 ticket 15, ADR 0005 supplement,
``zotero_translate.profile_rules``): ``PipelineSettings.user_profile_rules``
is on exactly when the settings are a user profile's
(``load_profile_settings``: the worker's ``--profile-file``, and ``run
--profile-file``), and off for Gemini's two files (``load_pipeline_settings``
has no way to turn it on), so Gemini's request bodies stay as they were. It
is not a line of the file: the user profiles' files written before the
ticket, and a hand-written file given to ``run --profile-file``, need no
change.

Hedging (M3 ticket 15, ADR 0004) is on unless ``run`` or ``serve`` gets
``--no-hedging``: a flash send of a translation batch or a review that has
run ``HEDGE_AFTER_SECONDS`` with no end gets a copy sent to argon. Test only:
``ZT_TEST_HEDGE_AFTER_SECONDS`` (an environment variable, inherited by the
service's task subprocesses) replaces the 400 seconds.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_MODEL = "gemini-3.8-flash-high"
DEFAULT_CONCURRENCY = 40
DEFAULT_ARGON_MODEL = "gemini-4-argon-high"
# Below the 19 of 40 concurrent requests that argon answered in two probes.
DEFAULT_ARGON_CONCURRENCY = 16
DEFAULT_PROFILE_CONCURRENCY = 16  # a user profile's, when it gives none (ADR 0005)
DEFAULT_PORT = 8920
# The built-in profile's name when ``model.env`` has no ``ZT_PROFILE_NAME``
# (M5 ticket 02), and the longest name any profile may have.
DEFAULT_BUILTIN_NAME = "Gemini"
PROFILE_NAME_SETTING = "ZT_PROFILE_NAME"
MAX_PROFILE_NAME_LENGTH = 60
HEDGE_AFTER_SECONDS = 400.0  # a flash send this old with no end gets an argon copy
HEDGE_AFTER_SETTING = "ZT_TEST_HEDGE_AFTER_SECONDS"  # test only: replaces it
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
# The user profiles' files, ``<profile id>.env`` (ADR 0005); the test
# fixtures point ``ZT_PROFILES_DIR`` at the test's own directory.
PROFILES_DIR_SETTING = "ZT_PROFILES_DIR"
DEFAULT_PROFILES_DIR = Path(
    os.environ.get(
        PROFILES_DIR_SETTING, Path.home() / ".config" / "zotero-translate" / "profiles"
    )
)

_ENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_LINE_ENDS = re.compile(r"\r\n|\n|\r")  # where an env file is cut into lines (``env_entries``)


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
    # Streamed requests (M4 ticket 12, ``ZT_STREAM``): the body asks for an
    # event stream, whose content deltas make the answer. Off for the
    # built-in Gemini, whose request bodies (and cache keys) stay as they were.
    stream: bool = False


def read_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ignore blank lines and comments.

    A line ends at a line feed or a carriage return, as in every editor
    that wrote the file, and nowhere else (``env_entries``): a vertical
    tab, a form feed, NEL, U+2028 and the like stay in the line's value, so
    that a value cannot start a setting of its own, e.g. a profile's name.
    """
    values: dict[str, str] = {}
    for key, value in env_entries(Path(path).read_text(encoding="utf-8")):
        values[key] = _unquote(value)
    return values


def env_entries(text: str) -> Iterator[tuple[str, str]]:
    """``(key, value as written)`` of each ``KEY=VALUE`` line of an env file's ``text``, in order.

    The one place that cuts an env file into lines (``read_env_file``,
    ``load_builtin_profile_name``): at ``\\r\\n``, ``\\n`` and ``\\r`` only, not
    wherever ``str.splitlines`` would.
    """
    for line in _LINE_ENDS.split(text):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match:
            yield match.group(1), match.group(2)


def _unquote(value: str) -> str:
    """``value`` without the matching quotes around it, if it has them."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


@dataclass(frozen=True)
class PipelineSettings:
    """The two endpoints of the pipeline model (ADR 0004)."""

    flash: ModelSettings
    argon: ModelSettings
    # Seconds after which a flash send of a hedged request kind gets an
    # argon copy (``zotero_translate.model_client``); None: no hedging.
    hedge_after_seconds: float | None = None
    # The model profile, ``{"id", "name"}``, of a service task (ADR 0005);
    # None for ``run``.
    profile: dict | None = None
    # The prompt rules for user profiles only (M4 ticket 15,
    # ``zotero_translate.profile_rules``): on for every user profile's task,
    # off for the built-in Gemini, whose prompts stay as they were.
    user_profile_rules: bool = False


def load_pipeline_settings(
    env_file: Path,
    argon_env_file: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    cache_dir: Path | None = None,
    hedging: bool = True,
    profile: dict | None = None,
) -> PipelineSettings:
    """Read both environment files; raise ``SettingsError`` if either is missing or incomplete.

    With ``hedging``, the threshold is ``HEDGE_AFTER_SECONDS``, or the test
    setting ``ZT_TEST_HEDGE_AFTER_SECONDS``. ``profile`` (``{"id",
    "name"}``, the built-in profile in the service) goes to the task report.
    These are the built-in profile's settings: never the prompt rules for
    user profiles.
    """
    return PipelineSettings(
        flash=load_model_settings(env_file, concurrency, cache_dir),
        argon=load_argon_settings(argon_env_file, cache_dir),
        hedge_after_seconds=_hedge_after() if hedging else None,
        profile=profile,
    )


def _hedge_after() -> float:
    """``HEDGE_AFTER_SECONDS``, or the test setting ``ZT_TEST_HEDGE_AFTER_SECONDS``."""
    if not os.environ.get(HEDGE_AFTER_SETTING):
        return HEDGE_AFTER_SECONDS
    hedge_after = _positive_seconds(os.environ[HEDGE_AFTER_SETTING])
    if hedge_after is None:
        raise SettingsError(f"{HEDGE_AFTER_SETTING} 不是正数")
    return hedge_after


def load_model_settings(
    env_file: Path, concurrency: int = DEFAULT_CONCURRENCY, cache_dir: Path | None = None
) -> ModelSettings:
    """Flash: read ``OPENAI_BASE_URL``, ``OPENAI_API_KEY`` and ``AI_DEFAULT_MODEL``.

    The optional ``ZT_REQUEST_TIMEOUT_SECONDS`` replaces the 480-second request
    timeout; tests use it to make timeouts happen in a few seconds.
    """
    values = _read_endpoint_file(env_file, "找不到管线模型的环境文件")
    return _endpoint_settings(env_file, values, DEFAULT_MODEL, concurrency, cache_dir)


def load_builtin_profile_name(env_file: Path) -> str:
    """The built-in profile's name in flash's environment file (M5 ticket 02).

    ``ZT_PROFILE_NAME`` with its spaces at both ends removed; "Gemini" when
    the file has no such line or its value is empty. Raise ``SettingsError``
    when the file is missing, unreadable or incomplete (as
    ``load_model_settings`` does), when the name has more than 60 characters
    or a control character, the limits of a user profile's name, or when its
    quote is not closed in its line.

    Only ``serve`` and ``run`` call it, once at startup: loading the model
    settings (a worker, the listing) does not look at the name.

    The name's value is taken as written (``env_entries``, the cut into
    lines ``read_env_file`` shares), before quotes and spaces are taken off:
    a vertical tab, form feed, NEL in it stay in the name for the check for
    control characters to see, and none of them starts a setting.
    """
    _read_endpoint_file(env_file, "找不到管线模型的环境文件")
    try:
        text = Path(env_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SettingsError(f"读不了环境文件 {env_file}：{type(e).__name__}") from e
    value = ""
    for key, written in env_entries(text):
        if key == PROFILE_NAME_SETTING:
            value = written  # the last line wins, as in read_env_file
    if value[:1] in ("'", '"') and not (len(value) >= 2 and value[-1] == value[0]):
        raise SettingsError(
            f"环境文件 {env_file} 中的 {PROFILE_NAME_SETTING} 的引号没有配对（值要写在一行里）"
        )
    name = _unquote(value).strip()
    if not name:
        return DEFAULT_BUILTIN_NAME
    if len(name) > MAX_PROFILE_NAME_LENGTH:
        raise SettingsError(
            f"环境文件 {env_file} 中的 {PROFILE_NAME_SETTING} 太长："
            f"最多 {MAX_PROFILE_NAME_LENGTH} 个字符，现有 {len(name)} 个"
        )
    if has_control_character(name):
        raise SettingsError(
            f"环境文件 {env_file} 中的 {PROFILE_NAME_SETTING} 含控制字符（制表符、换行、零宽字符等）"
        )
    return name


def has_control_character(text: str) -> bool:
    """Whether ``text`` has a character of a Unicode ``C`` category (control, format, ...)."""
    return any(unicodedata.category(c).startswith("C") for c in text)


def load_argon_settings(env_file: Path, cache_dir: Path | None = None) -> ModelSettings:
    """Argon: like ``load_model_settings``; ``ZT_CONCURRENCY`` replaces the limit of 16."""
    values = _read_endpoint_file(env_file, f"找不到 argon（{DEFAULT_ARGON_MODEL}）的环境文件")
    concurrency = _concurrency_setting(env_file, values, DEFAULT_ARGON_CONCURRENCY)
    return _endpoint_settings(env_file, values, DEFAULT_ARGON_MODEL, concurrency, cache_dir)


def load_profile_settings(
    profile_file: Path,
    cache_dir: Path | None = None,
    hedging: bool = True,
    profile: dict | None = None,
    concurrency: int | None = None,
) -> PipelineSettings:
    """A user profile (ADR 0005): one endpoint file for both roles.

    The file has the keys of an endpoint file, ``AI_DEFAULT_MODEL``
    required, and ``ZT_CONCURRENCY`` (default ``DEFAULT_PROFILE_CONCURRENCY``),
    the limit of the one budget both roles share; ``ZT_PROFILE_NAME`` is
    the profile's name (``zotero_translate.profiles``). Both roles name the
    same address and key, so ``PipelineModels`` makes them one endpoint
    without hedging, and the task log says so. ``profile`` (``{"id",
    "name"}``) goes to the task report. Its tasks have the prompt rules for
    user profiles (``user_profile_rules``, M4 ticket 15), whatever the file
    says: a file written before the ticket gets them too. ``concurrency``
    (``run --profile-file … --concurrency``, for development) replaces the
    file's limit.
    """
    values = _read_endpoint_file(profile_file, "找不到模型配置的文件")
    if not values.get("AI_DEFAULT_MODEL"):
        raise SettingsError(f"模型配置的文件 {profile_file} 缺少：AI_DEFAULT_MODEL")
    if concurrency is None:
        concurrency = _concurrency_setting(profile_file, values, DEFAULT_PROFILE_CONCURRENCY)
    endpoint = _endpoint_settings(profile_file, values, DEFAULT_MODEL, concurrency, cache_dir)
    return PipelineSettings(
        flash=endpoint,
        argon=endpoint,
        hedge_after_seconds=_hedge_after() if hedging else None,
        profile=profile,
        user_profile_rules=True,
    )


def _concurrency_setting(env_file: Path, values: dict[str, str], default: int) -> int:
    if not values.get("ZT_CONCURRENCY"):
        return default
    try:
        concurrency = int(values["ZT_CONCURRENCY"])
    except ValueError:
        concurrency = 0
    if concurrency < 1:
        raise SettingsError(f"环境文件 {env_file} 中的 ZT_CONCURRENCY 不是正整数")
    return concurrency


def _read_endpoint_file(env_file: Path, missing_reason: str) -> dict[str, str]:
    env_file = Path(env_file)
    try:
        if not env_file.is_file():
            raise SettingsError(f"{missing_reason}：{env_file}")
        values = read_env_file(env_file)
    except (OSError, UnicodeDecodeError) as e:  # unreadable or not UTF-8: a settings error too
        raise SettingsError(f"读不了环境文件 {env_file}：{type(e).__name__}") from e
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
    stream = stream_setting(values.get("ZT_STREAM", ""))
    if stream is None:
        raise SettingsError(f"环境文件 {env_file} 中的 ZT_STREAM 要是 1 或 0")
    return ModelSettings(
        base_url=values["OPENAI_BASE_URL"].rstrip("/"),
        api_key=values["OPENAI_API_KEY"],
        model=values.get("AI_DEFAULT_MODEL") or default_model,
        concurrency=concurrency,
        timeout_seconds=timeout_seconds,
        cache_dir=Path(cache_dir) if cache_dir else None,
        stream=stream,
    )


def stream_setting(text: str) -> bool | None:
    """``ZT_STREAM`` of an endpoint file: ``1`` on, ``0`` or nothing off; None for any other value."""
    return {"": False, "0": False, "1": True}.get(text.strip())


def base_url_problem(base_url: str) -> str | None:
    """Why ``base_url`` cannot be an endpoint address, or None if it can.

    It must be ``http://`` or ``https://`` with a host name and, if it
    names one, a port from 1 to 65535.
    """
    try:
        url = urlsplit(base_url)
        port = url.port  # raises ValueError for a port out of range or not a number
    except ValueError as e:
        return f"地址格式不对（{e}）"
    if url.scheme not in ("http", "https"):
        return "地址要以 http:// 或 https:// 开头"
    if not url.hostname:
        return "地址缺少主机名"
    if port == 0:
        return "地址的端口要在 1 到 65535 之间"
    return None


def _positive_seconds(text: str) -> float | None:
    try:
        seconds = float(text)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def model_cache_dir(data_dir: Path) -> Path:
    """Where the response cache lives inside a data directory."""
    return Path(data_dir) / "model-cache"
