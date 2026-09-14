"""실제 요청 인증값을 오류와 디버그 결과의 모든 필드에서 제거합니다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from typing import Any, cast
from urllib.parse import quote, quote_plus, unquote

from pydantic import BaseModel

from .debug import DebugRun


def credential_values(
    params: Mapping[str, Any] | None, key_name: str = "serviceKey"
) -> tuple[str, ...]:
    return tuple(
        str(value)
        for key, value in (params or {}).items()
        if key.lower().replace("_", "") in {"servicekey", "authkey"} or key == key_name
    )


def redact_secret(value: Any, *secrets: str) -> Any:
    """모델·불변 dataclass·tuple 형태를 보존하면서 인증값을 지웁니다."""
    variants = set()
    for secret in secrets:
        if secret:
            variants.update({secret, unquote(secret), quote(secret, safe=""), quote_plus(secret)})
    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        for secret in sorted(variants, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "<REDACTED>")
        return value
    if isinstance(value, BaseModel):
        return value.model_copy(
            update={key: redact_secret(item, *secrets) for key, item in value.__dict__.items()}
        )
    if is_dataclass(value) and not isinstance(value, type):
        return replace(
            value,
            **{
                field.name: redact_secret(getattr(value, field.name), *secrets)
                for field in fields(value)
            },
        )
    if isinstance(value, Mapping):
        return {
            redact_secret(key, *secrets): redact_secret(item, *secrets)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_secret(item, *secrets) for item in value)
    if isinstance(value, list):
        return [redact_secret(item, *secrets) for item in value]
    return value


def redact_exception(exc: Exception, *secrets: str) -> None:
    exc.args = redact_secret(exc.args, *secrets)
    for key, value in vars(exc).items():
        setattr(exc, key, redact_secret(value, *secrets))


def redact_debug(run: DebugRun, *secrets: str) -> DebugRun:
    return cast(DebugRun, redact_secret(run, *secrets))
