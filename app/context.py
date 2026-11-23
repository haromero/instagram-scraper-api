"""Contexto de petición: request_id compartido entre middleware y handlers."""

from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="req_unknown")


def set_request_id(value: str):
    return _request_id.set(value)


def reset_request_id(token) -> None:
    _request_id.reset(token)


def get_request_id() -> str:
    return _request_id.get()
