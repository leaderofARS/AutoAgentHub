"""Outbound messaging helpers for Telegram Bot API and Meta WhatsApp Cloud API."""

from __future__ import annotations

import httpx

from autoagenthub.config import get_settings


async def send_telegram_message(text: str, chat_id: str | int | None = None) -> dict:
    """Send a text message to a Telegram chat."""
    settings = get_settings()
    target = str(chat_id or settings.telegram_default_chat_id).strip()
    if not settings.telegram_bot_token or not target:
        raise ValueError("TELEGRAM_BOT_TOKEN and chat_id/TELEGRAM_DEFAULT_CHAT_ID are required.")

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json={"chat_id": target, "text": text[:4000]})
        resp.raise_for_status()
        return resp.json()


async def send_whatsapp_message(text: str, recipient: str | None = None) -> dict:
    """Send a text message via the Meta WhatsApp Cloud API."""
    settings = get_settings()
    to_number = (recipient or settings.whatsapp_default_recipient).strip()
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id or not to_number:
        raise ValueError(
            "WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, and recipient are required."
        )

    url = f"https://graph.facebook.com/v21.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text[:4000]},
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()
