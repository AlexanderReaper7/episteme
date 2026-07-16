from .base import SourceAdapter

_ADAPTERS: dict[str, SourceAdapter] = {}


def register(adapter_cls: type) -> type:
    """Class decorator: instantiate and register a SourceAdapter by its type_name."""
    adapter = adapter_cls()
    _ADAPTERS[adapter.type_name] = adapter
    return adapter_cls


def get_adapter(type_name: str) -> SourceAdapter:
    try:
        return _ADAPTERS[type_name]
    except KeyError:
        raise LookupError(
            f"No source adapter registered for type {type_name!r}; "
            f"known types: {sorted(_ADAPTERS)}"
        ) from None
