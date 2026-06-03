import pytest
from types import SimpleNamespace
import telegram.bot as bot


def test_send_telegram_message_retries_in_plain_text(monkeypatch):
    posts = []

    def fake_post(url, json, timeout):
        posts.append((url, json, timeout))
        if len(posts) == 1:
            return SimpleNamespace(status_code=400, text="Bad Request")
        return SimpleNamespace(status_code=200, text="OK")

    monkeypatch.setattr(bot.requests, "post", fake_post)
    monkeypatch.setattr(bot.settings, "TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setattr(bot.settings, "TELEGRAM_CHAT_ID", "chat123")

    assert bot.send_telegram_message("Hello *world*") is True
    assert len(posts) == 2
    assert posts[0][1]["parse_mode"] == "MarkdownV2"
    assert posts[1][1].get("parse_mode") is None
    assert "*" not in posts[1][1]["text"]
