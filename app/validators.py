"""Validaciones de seguridad para datos de entrada."""

import ipaddress
import socket
from urllib.parse import urlparse, urlunparse

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}

_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    # ::ffff:127.0.0.1 y similares deben evaluarse como su IPv4 equivalente.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _assert_safe_url(raw: str, *, allow_userinfo: bool, resolve: bool) -> str:
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise ValueError("URL inválida.") from exc

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Solo se permiten URLs http o https.")

    if not allow_userinfo and (parsed.username or parsed.password):
        raise ValueError("La URL no puede incluir credenciales.")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("La URL no incluye un host válido.")

    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise ValueError("El host de destino no está permitido.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("El puerto de la URL no es válido.") from exc

    # Los literales IP se comprueban siempre, sin tocar la red.
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise ValueError("El destino apunta a una red interna y está bloqueado.")
        return raw

    if not resolve:
        return raw

    try:
        infos = socket.getaddrinfo(
            host,
            port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ValueError("No se pudo resolver el host de destino.") from exc

    for info in infos:
        address = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise ValueError("El host resolvió a una dirección no válida.")
        if _is_blocked_ip(ip):
            raise ValueError("El destino apunta a una red interna y está bloqueado.")

    return raw


def assert_safe_outbound_url(raw: str, *, resolve: bool = False) -> str:
    """Rechaza URLs que podrían producir SSRF contra la red interna.

    ``resolve=False`` valida sintaxis, credenciales y literales IP (rápido y sin
    red). Usa ``resolve=True`` en el momento del envío para bloquear también
    DNS rebinding.
    """
    return _assert_safe_url(raw, allow_userinfo=False, resolve=resolve)


def assert_safe_proxy_url(raw: str, *, resolve: bool = False) -> str:
    """Como ``assert_safe_outbound_url`` pero permite userinfo en la URL."""
    return _assert_safe_url(raw, allow_userinfo=True, resolve=resolve)


def split_proxy_credentials(raw: str) -> tuple[str, str | None, str | None]:
    """Separa usuario/contraseña de la URL para no persistirlos en claro."""
    parsed = urlparse(raw)
    username = parsed.username
    password = parsed.password
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    clean = urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
    return clean, username, password
