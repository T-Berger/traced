"""Saved X accounts use the existing bearer-token API integration."""

from .base import RegistryAdapter


class XAdapter(RegistryAdapter):
    platform = "x"
