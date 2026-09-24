from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


_STAGE_HELPER = r"""
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from ruamel.yaml import YAML

path = Path(sys.argv[1])
expected_id = sys.argv[2]
updates = json.load(sys.stdin)

if not isinstance(updates, dict):
    raise SystemExit(20)
if set(updates) - {"title", "description"}:
    raise SystemExit(21)
if not updates:
    raise SystemExit(22)
for key, value in updates.items():
    if not isinstance(value, str):
        raise SystemExit(23)

yaml = YAML()
yaml.preserve_quotes = True
with path.open("r", encoding="utf-8") as source:
    document = yaml.load(source)

if not isinstance(document, dict):
    raise SystemExit(24)
if str(document.get("id", "")).strip() != expected_id:
    raise SystemExit(25)

for key, value in updates.items():
    document[key] = value

original_mode = stat.S_IMODE(path.stat().st_mode)
fd, tmp_name = tempfile.mkstemp(
    prefix=f".{path.name}.",
    suffix=".tmp",
    dir=path.parent,
)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        yaml.dump(document, target)
        target.flush()
        os.fsync(target.fileno())
    os.chmod(tmp_name, original_mode)
    os.replace(tmp_name, path)
finally:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass
"""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    timed_out: bool = False


class CommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        input_text: str | None,
        timeout_seconds: float,
    ) -> ProcessResult:
        ...


class SubprocessRunner:
    """Run argv-only child processes while discarding their provider output."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        input_text: str | None,
        timeout_seconds: float,
    ) -> ProcessResult:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ProcessResult(returncode=124, timed_out=True)
        return ProcessResult(returncode=completed.returncode)


@dataclass(frozen=True, slots=True)
class BrowserBotRuntime:
    python_executable: Path
    bot_checkout: Path
    config_path: Path
    download_dir_name: str = "downloaded-ads"
    language: str = "de"
    timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        for field_name in ("python_executable", "bot_checkout", "config_path"):
            value = getattr(self, field_name)
            if not value.is_absolute():
                raise ValueError(f"{field_name} must be an absolute path")
        directory = Path(self.download_dir_name)
        if (
            not self.download_dir_name
            or directory.is_absolute()
            or directory.name != self.download_dir_name
            or self.download_dir_name in {".", ".."}
        ):
            raise ValueError("download_dir_name must be one relative directory name")
        if not self.language.strip():
            raise ValueError("language must not be blank")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def workspace(self) -> Path:
        return self.config_path.parent

    @property
    def download_root(self) -> Path:
        return self.workspace / self.download_dir_name


class BrowserBotError(RuntimeError):
    """Base error for the isolated external browser-bot boundary."""


class BrowserBotWorkspaceError(BrowserBotError):
    pass


class BrowserBotProcessError(BrowserBotError):
    def __init__(
        self,
        *,
        phase: str,
        returncode: int,
        timed_out: bool = False,
    ) -> None:
        self.phase = phase
        self.returncode = returncode
        self.timed_out = timed_out
        suffix = " (timeout)" if timed_out else ""
        super().__init__(
            f"browser bot {phase} failed with return code {returncode}{suffix}"
        )


def _validated_ad_id(ad_id: str) -> str:
    normalized = str(ad_id).strip()
    if (
        not normalized
        or len(normalized) > 32
        or not normalized.isascii()
        or not normalized.isdigit()
    ):
        raise ValueError("ad_id must contain only ASCII digits")
    return normalized


class BrowserBotAdapter:
    """External-process adapter for sync and in-place content update only.

    No AGPL code is imported by mark-api. A successful process exit is not a
    remote success receipt; the core SafeWriteOrchestrator must perform the
    independent platform post-readback.
    """

    def __init__(
        self,
        runtime: BrowserBotRuntime,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self._runtime = runtime
        self._runner = runner or SubprocessRunner()

    def _bot_argv(self, command: str, ad_id: str) -> tuple[str, ...]:
        return (
            str(self._runtime.python_executable),
            "-m",
            "kleinanzeigen_bot",
            f"--lang={self._runtime.language}",
            "--workspace-mode=portable",
            "--config",
            str(self._runtime.config_path),
            command,
            f"--ads={ad_id}",
        )

    def _run(
        self,
        *,
        phase: str,
        argv: tuple[str, ...],
        input_text: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        try:
            result = self._runner.run(
                argv,
                cwd=cwd or self._runtime.bot_checkout,
                input_text=input_text,
                timeout_seconds=self._runtime.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - sanitize process boundary.
            raise BrowserBotProcessError(
                phase=phase,
                returncode=127,
            ) from None
        if result.returncode != 0:
            raise BrowserBotProcessError(
                phase=phase,
                returncode=result.returncode,
                timed_out=result.timed_out,
            )

    def sync(self, ad_id: str) -> None:
        target_id = _validated_ad_id(ad_id)
        self._run(
            phase="sync",
            argv=self._bot_argv("download", target_id),
        )

    def _find_ad_yaml(self, ad_id: str) -> Path:
        target_id = _validated_ad_id(ad_id)
        candidates = sorted(
            self._runtime.download_root.glob(
                f"ad_{target_id}_*/ad_{target_id}.yaml"
            )
        )
        files = [path for path in candidates if path.is_file()]
        if len(files) != 1:
            raise BrowserBotWorkspaceError(
                f"expected exactly one synced YAML for ad {target_id}, "
                f"found {len(files)}"
            )

        path = files[0]
        root = self._runtime.download_root.resolve()
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(root):
            raise BrowserBotWorkspaceError("synced YAML escapes the download workspace")
        return path

    def _stage_content(
        self,
        *,
        ad_id: str,
        title: str | None,
        description: str | None,
    ) -> Path:
        target_id = _validated_ad_id(ad_id)
        updates: dict[str, str] = {}
        if title is not None:
            if not isinstance(title, str):
                raise TypeError("title must be a string or None")
            updates["title"] = title
        if description is not None:
            if not isinstance(description, str):
                raise TypeError("description must be a string or None")
            updates["description"] = description
        if not updates:
            raise ValueError("at least one content field must be provided")

        ad_yaml = self._find_ad_yaml(target_id)
        payload = json.dumps(
            updates,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._run(
            phase="stage",
            argv=(
                str(self._runtime.python_executable),
                "-c",
                _STAGE_HELPER,
                str(ad_yaml),
                target_id,
            ),
            input_text=payload,
            cwd=self._runtime.workspace,
        )
        return ad_yaml

    def update_content(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        target_id = _validated_ad_id(ad_id)
        self._stage_content(
            ad_id=target_id,
            title=title,
            description=description,
        )
        self._run(
            phase="update",
            argv=self._bot_argv("update", target_id),
        )