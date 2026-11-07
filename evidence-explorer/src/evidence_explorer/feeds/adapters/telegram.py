"""Saved Telegram channels use the existing public-preview collector."""

from .base import RegistryAdapter


class TelegramAdapter(RegistryAdapter):
    platform = "telegram"
