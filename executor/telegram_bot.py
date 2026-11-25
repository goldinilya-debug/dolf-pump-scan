"""Tiny Telegram helper: send messages, poll commands from ONE chat only (TG_CHAT)."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("tg")


class Telegram:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None, prefix: str = ""):
        self.token = (token or "").strip()
        self.chat = str(chat_id or "").strip()
        self.s = session or requests.Session()
        self.prefix = prefix
        self._recent: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str, throttle_key: str | None = None, throttle_s: float = 1800) -> bool:
        """Send a message. With throttle_key the same message is sent at most once per throttle_s."""
        if throttle_key:
            last = self._recent.get(throttle_key, 0)
            if time.time() - last < throttle_s:
                return False
            self._recent[throttle_key] = time.time()
        msg = (self.prefix + text)[:4000]
        log.info("TG> %s", msg.replace("\n", " | "))
        if not self.enabled:
            return False
        for i in range(3):
            try:
                r = self.s.post(self._url("sendMessage"), data={"chat_id": self.chat, "text": msg,
                                                                "disable_web_page_preview": "true"}, timeout=15)
                if r.status_code == 200 and r.json().get("ok"):
                    return True
                log.warning("telegram send failed: %s %s", r.status_code, r.text[:200])
                if r.status_code in (400, 401, 403, 404):
                    return False
            except requests.RequestException as e:
                log.warning("telegram send error: %s", e)
            time.sleep(2 + 2 * i)
        return False

    def poll(self, offset: int | None, max_age_s: int = 600) -> tuple[list[str], int | None]:
        """Return (commands, new_offset). Only messages from TG_CHAT, not older than max_age_s."""
        if not self.enabled:
            return [], offset
        params = {"timeout": 0, "allowed_updates": '["message","channel_post"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            r = self.s.get(self._url("getUpdates"), params=params, timeout=15)
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("telegram poll error: %s", e)
            return [], offset
        if not j.get("ok"):
            log.warning("telegram poll not ok: %s", str(j)[:200])
            return [], offset
        cmds, now = [], time.time()
        for u in j.get("result", []):
            offset = max(offset or 0, int(u["update_id"]) + 1)
            m = u.get("message") or u.get("channel_post") or {}
            if str((m.get("chat") or {}).get("id")) != self.chat:
                continue
            if now - float(m.get("date", 0)) > max_age_s:
                continue  # stale command (e.g. sent while the bot was offline) -> ignore
            text = (m.get("text") or "").strip()
            if text.startswith("/"):
                head, _, rest = text.partition(" ")
                head = head.split("@")[0].lower()
                cmds.append((head + (" " + rest.strip() if rest.strip() else "")).strip())
        return cmds, offset
