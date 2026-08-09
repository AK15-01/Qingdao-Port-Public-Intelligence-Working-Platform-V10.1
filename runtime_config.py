from __future__ import annotations

"""Small, side-effect-free runtime configuration shared by local and public UI."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Callable, Mapping, Optional


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DEMO_DATABASE = PROJECT_ROOT / "data" / "demo" / "portscope_demo.db"
TRUE_VALUES = {"1", "true", "yes", "on", "是", "启用"}
FALSE_VALUES = {"0", "false", "no", "off", "否", "禁用", ""}


def parse_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default


def streamlit_secret(name: str) -> Optional[str]:
    """Read one Streamlit secret without requiring a secrets file to exist."""
    try:
        import streamlit as st

        value = st.secrets.get(name)
    except Exception:
        return None
    if value is None:
        return None
    return str(value).strip()


def runtime_value(
    name: str,
    default: str = "",
    *,
    environ: Optional[Mapping[str, str]] = None,
    secret_reader: Optional[Callable[[str], Optional[str]]] = None,
) -> tuple[str, str]:
    """Return value and non-sensitive source label (environment wins)."""
    values = os.environ if environ is None else environ
    if name in values:
        return str(values.get(name) or "").strip(), "环境变量"
    reader = streamlit_secret if secret_reader is None else secret_reader
    secret = reader(name)
    if secret is not None:
        return secret, "Streamlit secrets"
    return str(default or "").strip(), "默认值"


def is_public_demo_mode(
    *,
    environ: Optional[Mapping[str, str]] = None,
    secret_reader: Optional[Callable[[str], Optional[str]]] = None,
) -> bool:
    value, _ = runtime_value(
        "PUBLIC_DEMO_MODE",
        "false",
        environ=environ,
        secret_reader=secret_reader,
    )
    return parse_bool(value, default=False)


def _project_path(value: str, default: Path) -> Path:
    candidate = Path(value).expanduser() if value else default
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


@dataclass(frozen=True)
class RuntimeConfig:
    public_demo_mode: bool
    demo_database_path: Path
    deepseek_key_present: bool
    deepseek_key_source: str

    @property
    def mode_label(self) -> str:
        return "公网只读 Demo" if self.public_demo_mode else "本地完整版"


def load_runtime_config() -> RuntimeConfig:
    public_mode = is_public_demo_mode()
    database_value, _ = runtime_value("PORTSCOPE_DEMO_DB_PATH", "")
    key, key_source = runtime_value("DEEPSEEK_API_KEY", "")
    return RuntimeConfig(
        public_demo_mode=public_mode,
        demo_database_path=_project_path(database_value, DEFAULT_DEMO_DATABASE),
        deepseek_key_present=bool(key),
        deepseek_key_source=key_source if key else "未配置",
    )


_WINDOWS_PATH = re.compile(r"(?i)[A-Z]:\\(?:[^\\\s]+\\)+[^\s]*")
_API_KEY = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b")
_AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+")


def redact_sensitive_text(value: object, *, limit: int = 300) -> str:
    """Produce a short diagnostic suitable for logs or a public error boundary."""
    text = str(value or "")
    text = _API_KEY.sub("sk-****", text)
    text = _AUTH_HEADER.sub(r"\1****", text)
    text = _WINDOWS_PATH.sub("[本地路径已隐藏]", text)
    text = text.replace(str(PROJECT_ROOT), "[项目目录]")
    return text[: max(0, limit)]


def project_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name
