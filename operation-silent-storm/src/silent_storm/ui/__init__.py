"""Dash views without application initialization or mutable import side effects."""

from .explorer import create_layout, register_callbacks

__all__ = ["create_layout", "register_callbacks"]
