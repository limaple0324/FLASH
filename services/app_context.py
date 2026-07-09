"""Simple service registry for FLASH."""

from typing import Any


class AppContext:
    _services: dict[type, Any] = {}

    @classmethod
    def register(cls, service_type: type, instance: Any) -> None:
        cls._services[service_type] = instance

    @classmethod
    def get(cls, service_type: type) -> Any:
        return cls._services.get(service_type)

    @classmethod
    def clear(cls) -> None:
        cls._services.clear()
