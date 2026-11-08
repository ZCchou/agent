"""Admission policy for the production offline application's local transports."""
from dataclasses import replace
from urllib.parse import urlparse

from agent.providers import ProviderProfile
from agent.storage import CoreDataError


def _local_endpoint(value: str) -> str:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} \
            or parsed.username or parsed.password or parsed.query or parsed.fragment or port == -1:
        raise CoreDataError("offline_provider_forbidden", "离线模式仅允许本机回环地址上的模型服务。")
    if parsed.hostname == "localhost":
        parsed = parsed._replace(netloc="127.0.0.1" + (f":{port}" if port is not None else ""))
    return parsed.geturl()


def local_profile(profile: ProviderProfile) -> ProviderProfile:
    if profile.kind == "fake":
        return profile
    endpoint = _local_endpoint(profile.base_url)
    routes = tuple({**route, "endpoint_base_url": _local_endpoint(route.get("endpoint_base_url") or endpoint)}
                   for route in profile.invocation_routes)
    if profile.native_web_adapter != "none":
        raise CoreDataError("offline_provider_forbidden", "离线模式禁用模型公网检索路线。")
    return replace(profile, base_url=endpoint, invocation_routes=routes, local_strict=True)
