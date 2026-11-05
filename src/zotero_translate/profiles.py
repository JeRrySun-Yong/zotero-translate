"""Model profiles (ADR 0005): which endpoint and model each role of a paper task uses.

The built-in profile ``gemini`` ("Gemini") is the two endpoints of the
service's ``--env-file`` and ``--argon-env-file``; it cannot be edited or
deleted, and needs no connection test. A user profile has one endpoint for
both roles; its settings and API key live in ``<profiles dir>/<id>.env``
(mode 600, the directory 700), in the format of the endpoint files
(``zotero_translate.settings.load_profile_settings``) plus
``ZT_PROFILE_NAME``. The id is 12 hexadecimal digits the service picks.

The current profile and each profile's last connection test are saved in
``<data dir>/model-profiles.json``: the profile a submitted task records
belongs to the service of that data directory, so a development service
with a data directory of its own never changes the user's. A test counts
only for the settings it tested: a fingerprint of the address, key, model
and request timeout is saved with it (a SHA-256 digest; the key itself is
not), so an edit of any of these, through the service or in the file,
makes the profile untested. Its name and concurrency limit are not part
of it.

Only a profile whose last test succeeded can be applied. The current
profile goes back to Gemini, with an event log line (当前配置改回 Gemini),
when its file is gone or unreadable, or it is no longer tested: checked
when the service starts, and whenever the current profile is read (each
listing, submission, apply).

No API key leaves this module except to the worker, through the file:
answers give ``{"set": true, "last4": ...}``, and the log callback gets the
profile id, name and a reason, never a key.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from zotero_translate import task_dir as td
from zotero_translate.model_client import REDACTED
from zotero_translate.settings import DEFAULT_PROFILE_CONCURRENCY
from zotero_translate.settings import ModelSettings
from zotero_translate.settings import SettingsError
from zotero_translate.settings import base_url_problem
from zotero_translate.settings import load_pipeline_settings
from zotero_translate.settings import read_env_file

BUILTIN_ID = "gemini"
BUILTIN_NAME = "Gemini"
STATE_FILE = "model-profiles.json"  # in the data directory
MAX_CONCURRENCY = 256
MAX_REQUEST_TIMEOUT_SECONDS = 3600
_FIELDS = ("name", "base_url", "api_key", "model", "concurrency", "request_timeout_seconds")
_REQUIRED = ("name", "base_url", "api_key", "model")
_LENGTHS = {"name": 60, "base_url": 500, "api_key": 500, "model": 200}
# The connection settings a test is valid for (see the module docstring).
_TESTED_FIELDS = ("base_url", "api_key", "model", "request_timeout_seconds")


class ProfileError(Exception):
    """A request about profiles that cannot be done: ``status`` is the HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    concurrency: int = DEFAULT_PROFILE_CONCURRENCY
    request_timeout_seconds: float | None = None

    def fingerprint(self) -> str:
        data = json.dumps([getattr(self, name) for name in _TESTED_FIELDS])
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def model_settings(self) -> ModelSettings:
        timeout = self.request_timeout_seconds or ModelSettings.timeout_seconds
        return ModelSettings(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            concurrency=self.concurrency,
            timeout_seconds=float(timeout),
        )


# log(profile_id, event, name, reason): one event log line (zotero_translate.service).
LogProfileEvent = Callable[[str, str, str, "str | None"], None]


class ProfileStore:
    """The profiles of one service: the built-in one, the user profiles, the current one."""

    def __init__(
        self,
        profiles_dir: Path,
        data_dir: Path,
        env_file: Path,
        argon_env_file: Path,
        concurrency: int,
        log: LogProfileEvent,
    ):
        self.profiles_dir = Path(profiles_dir)
        self.state_file = Path(data_dir) / STATE_FILE
        self.env_file = Path(env_file)
        self.argon_env_file = Path(argon_env_file)
        self.concurrency = concurrency
        self._log = log
        self._lock = threading.RLock()

    # -- reading -------------------------------------------------------------

    def path(self, profile_id: str) -> Path:
        return self.profiles_dir / f"{profile_id}.env"

    def current(self) -> tuple[str, str]:
        """``(id, name)`` of the current profile, after the check (module docstring)."""
        with self._lock:
            state = self._state()
            profile = self._read(state["current"]) if state["current"] else None
            if state["current"] and not self._usable(state, profile):
                reason = (
                    "配置文件不在了或读不了"
                    if profile is None
                    else "配置的连接设置改过，尚未通过测试连接"
                )
                old = state["current"]
                state["current"] = None
                self._save(state)
                self._log(old, "current_reset", profile.name if profile else None, reason)
                profile = None
            return (profile.id, profile.name) if profile else (BUILTIN_ID, BUILTIN_NAME)

    def for_submission(self, profile_id: str | None) -> tuple[str, str]:
        """``(id, name)`` of the profile a new task records: the one named, else the current one.

        A named profile must exist (else 422) and, unless it is Gemini, be
        tested (else 409): e.g. the plugin's retry names the failed task's.
        """
        if profile_id is None:
            return self.current()
        if profile_id == BUILTIN_ID:
            return BUILTIN_ID, BUILTIN_NAME
        with self._lock:
            profile = self._read(profile_id)
            if profile is None:
                raise ProfileError(422, "找不到这份模型配置")
            if not self._usable(self._state(), profile):
                raise ProfileError(409, "这份配置还没有通过测试连接，不能用来提交")
            return profile.id, profile.name

    def listing(self) -> dict:
        """``{"current": id, "profiles": [the built-in one, then the user profiles by name]}``."""
        with self._lock:
            current_id, _ = self.current()
            state = self._state()
            profiles = [self._builtin(current_id)]
            for profile in sorted(self._all(), key=lambda p: (p.name.casefold(), p.id)):
                profiles.append(self._answer(profile, state, current_id))
            return {"current": current_id, "profiles": profiles}

    def get(self, profile_id: str) -> dict:
        with self._lock:
            if profile_id == BUILTIN_ID:
                return self._builtin(self.current()[0])
            profile = self._existing(profile_id)
            return self._answer(profile, self._state(), self.current()[0])

    # -- writing -------------------------------------------------------------

    def create(self, body: dict) -> dict:
        fields = _validate(body, required=_REQUIRED)
        fields.setdefault("concurrency", DEFAULT_PROFILE_CONCURRENCY)
        fields.setdefault("request_timeout_seconds", None)
        with self._lock:
            profile = Profile(id=uuid.uuid4().hex[:12], **fields)
            self._write(profile)
            self._log(profile.id, "created", profile.name, None)
            return self.get(profile.id)

    def update(self, profile_id: str, body: dict) -> dict:
        """Change the fields given; the key stays unless given. A new
        connection setting makes the profile untested (and no longer current)."""
        fields = _validate(body, required=())
        with self._lock:
            if profile_id == BUILTIN_ID:
                raise ProfileError(409, "内置配置 Gemini 由翻译服务的启动参数决定，不能修改")
            old = self._existing(profile_id)
            profile = Profile(**{**old.__dict__, **fields})
            self._write(profile)
            self._log(profile.id, "updated", profile.name, None)
            return self.get(profile.id)

    def delete(self, profile_id: str, users: int) -> dict:
        """Delete the profile; refused while ``users`` (unfinished tasks) hold it."""
        with self._lock:
            if profile_id == BUILTIN_ID:
                raise ProfileError(409, "内置配置 Gemini 不能删除")
            profile = self._existing(profile_id)
            if users:
                raise ProfileError(409, f"还有 {users} 个未结束的论文任务使用这份配置，结束后才能删除")
            self.path(profile_id).unlink()
            state = self._state()
            state["tests"].pop(profile_id, None)
            self._save(state)
            self._log(profile_id, "deleted", profile.name, None)
            return {"deleted": profile_id, "current": self.current()[0]}

    def record_test(self, profile_id: str, fingerprint: str, check) -> dict:
        """Save the outcome of a connection test (``model_client.ConnectionCheck``)
        of the settings with ``fingerprint``, which ``test_settings`` gave:
        an edit while the test ran leaves the profile untested."""
        with self._lock:
            profile = self._existing(profile_id)
            # check_connection redacts the key; so does this, for whatever
            # else goes into the answer, the state file and the log.
            message = check.message.replace(profile.api_key, REDACTED) if profile.api_key else check.message
            state = self._state()
            state["tests"][profile_id] = {
                "result": check.result,
                "message": message,
                "seconds": check.seconds,
                "http_status": check.http_status,
                "tested_at": time.time(),
                "fingerprint": fingerprint,
            }
            self._save(state)
            self._log(profile_id, "tested", profile.name, message)
            return {
                "ok": check.ok,
                "result": check.result,
                "message": message,
                "seconds": check.seconds,
                "http_status": check.http_status,
            }

    def test_settings(self, profile_id: str) -> tuple[ModelSettings, str]:
        """The settings a connection test of the profile uses, and their fingerprint."""
        with self._lock:
            if profile_id == BUILTIN_ID:
                raise ProfileError(409, "内置配置 Gemini 不需要测试连接")
            profile = self._existing(profile_id)
            return profile.model_settings(), profile.fingerprint()

    def apply(self, profile_id: str) -> dict:
        with self._lock:
            state = self._state()
            if profile_id == BUILTIN_ID:
                name = BUILTIN_NAME
                state["current"] = None
            else:
                profile = self._existing(profile_id)
                if not self._usable(state, profile):
                    raise ProfileError(409, "这份配置还没有通过测试连接，不能应用")
                name = profile.name
                state["current"] = profile_id
            self._save(state)
            self._log(profile_id, "applied", name, None)
            return {"current": profile_id, "name": name}

    # -- internals -----------------------------------------------------------

    def _builtin(self, current_id: str) -> dict:
        answer = {
            "id": BUILTIN_ID,
            "name": BUILTIN_NAME,
            "builtin": True,
            "current": current_id == BUILTIN_ID,
            "tested": True,
        }
        try:
            settings = load_pipeline_settings(self.env_file, self.argon_env_file, self.concurrency)
        except SettingsError as e:
            return {**answer, "error": str(e)}
        roles = {"flash": settings.flash, "argon": settings.argon}
        return {
            **answer,
            "base_url": settings.flash.base_url,
            "model": settings.flash.model,
            "roles": {
                role: {"base_url": s.base_url, "model": s.model, "concurrency": s.concurrency}
                for role, s in roles.items()
            },
        }

    def _answer(self, profile: Profile, state: dict, current_id: str) -> dict:
        test = state["tests"].get(profile.id)
        if test is not None and test.get("fingerprint") != profile.fingerprint():
            test = None  # it tested other settings
        return {
            "id": profile.id,
            "name": profile.name,
            "builtin": False,
            "current": profile.id == current_id,
            "tested": self._usable(state, profile),
            "base_url": profile.base_url,
            "model": profile.model,
            "concurrency": profile.concurrency,
            "request_timeout_seconds": profile.request_timeout_seconds,
            # The last 4 characters only of a key long enough to keep the rest unknown.
            "api_key": {
                "set": bool(profile.api_key),
                "last4": profile.api_key[-4:] if len(profile.api_key) >= 8 else "",
            },
            "last_test": (
                {k: test[k] for k in ("result", "message", "seconds", "http_status", "tested_at")}
                if test
                else None
            ),
        }

    @staticmethod
    def _usable(state: dict, profile: Profile | None) -> bool:
        if profile is None:
            return False
        test = state["tests"].get(profile.id)
        return bool(
            test and test["result"] == "succeeded" and test["fingerprint"] == profile.fingerprint()
        )

    def _existing(self, profile_id: str) -> Profile:
        profile = self._read(profile_id)
        if profile is None:
            raise ProfileError(404, "找不到这份模型配置")
        return profile

    def _all(self) -> list[Profile]:
        if not self.profiles_dir.is_dir():
            return []
        found = (self._read(path.stem) for path in self.profiles_dir.glob("*.env"))
        return [p for p in found if p is not None]

    def _read(self, profile_id: str) -> Profile | None:
        """The profile in its file; None if there is none, or it is unreadable or incomplete."""
        if not _is_id(profile_id):
            return None
        try:
            values = read_env_file(self.path(profile_id))
            timeout = values.get("ZT_REQUEST_TIMEOUT_SECONDS")
            return Profile(
                id=profile_id,
                name=values.get("ZT_PROFILE_NAME") or profile_id,
                base_url=values["OPENAI_BASE_URL"],
                api_key=values["OPENAI_API_KEY"],
                model=values["AI_DEFAULT_MODEL"],
                concurrency=int(values.get("ZT_CONCURRENCY") or DEFAULT_PROFILE_CONCURRENCY),
                request_timeout_seconds=float(timeout) if timeout else None,
            )
        except (OSError, UnicodeDecodeError, KeyError, ValueError):
            return None

    def _write(self, profile: Profile) -> None:
        """Replace the profile's file atomically; mode 600 from its creation on."""
        self.profiles_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lines = {
            "ZT_PROFILE_NAME": profile.name,
            "OPENAI_BASE_URL": profile.base_url,
            "OPENAI_API_KEY": profile.api_key,
            "AI_DEFAULT_MODEL": profile.model,
            "ZT_CONCURRENCY": str(profile.concurrency),
        }
        if profile.request_timeout_seconds is not None:
            lines["ZT_REQUEST_TIMEOUT_SECONDS"] = repr(float(profile.request_timeout_seconds))
        # Quoted, so that read_env_file gives every value back as written.
        text = "".join(f'{key}="{value}"\n' for key, value in lines.items())
        path = self.path(profile.id)
        tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(text)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _state(self) -> dict:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        current = state.get("current")
        tests = state.get("tests")
        return {
            "current": current if isinstance(current, str) else None,
            "tests": tests if isinstance(tests, dict) else {},
        }

    def _save(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        td.write_json(self.state_file, state, indent=2)


def _is_id(text: str) -> bool:
    return len(text) == 12 and all(c in "0123456789abcdef" for c in text)


def _validate(body: dict, required: tuple[str, ...]) -> dict:
    """The profile fields of a request body, checked; raise ``ProfileError`` (400)."""
    unknown = sorted(set(body) - set(_FIELDS))
    if unknown:
        raise ProfileError(400, f"未知的字段：{', '.join(unknown)}")
    missing = [name for name in required if name not in body]
    if missing:
        raise ProfileError(400, f"缺少字段：{', '.join(missing)}")
    fields = {}
    for name in ("name", "base_url", "api_key", "model"):
        if name not in body:
            continue
        value = body[name]
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(400, f"{name} 不能为空")
        value = value.strip()
        if len(value) > _LENGTHS[name] or any(unicodedata.category(c).startswith("C") for c in value):
            raise ProfileError(400, f"{name} 太长或含控制字符")
        if name != "name" and any(c.isspace() for c in value):
            raise ProfileError(400, f"{name} 不能含空白")
        fields[name] = value
    if "base_url" in fields:
        problem = base_url_problem(fields["base_url"])
        if problem:
            raise ProfileError(400, f"base_url 无效：{problem}")
        fields["base_url"] = fields["base_url"].rstrip("/")
    if "concurrency" in body:
        value = body["concurrency"]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_CONCURRENCY:
            raise ProfileError(400, f"concurrency 要是 1 到 {MAX_CONCURRENCY} 的整数")
        fields["concurrency"] = value
    if "request_timeout_seconds" in body:
        value = body["request_timeout_seconds"]
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 < value <= MAX_REQUEST_TIMEOUT_SECONDS
        ):
            raise ProfileError(
                400, f"request_timeout_seconds 要是 0 到 {MAX_REQUEST_TIMEOUT_SECONDS} 之间的秒数，或 null"
            )
        fields["request_timeout_seconds"] = None if value is None else float(value)
    return fields
