"""Saved YouTube channels use existing title/description feed collection."""

from .base import RegistryAdapter


class YouTubeAdapter(RegistryAdapter):
    platform = "youtube"
