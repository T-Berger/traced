"""Saved Reddit communities use the existing approved API integration."""

from .base import RegistryAdapter


class RedditAdapter(RegistryAdapter):
    platform = "reddit"
