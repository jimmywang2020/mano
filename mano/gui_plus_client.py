"""OpenAI-compatible client for a hosted GUI VLM (default: DashScope gui-plus).

The client only proposes actions; it never touches ADB. Configure via env:
- MANO_API_KEY (or DASHSCOPE_API_KEY)  : provider API key (required)
- MANO_MODEL                           : model id (default gui-plus-2026-02-26)
- MANO_BASE_URL                        : OpenAI-compatible base url
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .errors import ManoError, ManoNetworkError


DEFAULT_MODEL = "gui-plus-2026-02-26"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def endpoint_from_base(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


class GuiVLM:
    """Minimal GUI-VLM client; it proposes actions but never executes ADB."""

    def __init__(self) -> None:
        self.api_key = os.getenv("MANO_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise ManoError("MANO_API_KEY (or DASHSCOPE_API_KEY) is not set")
        self.model = os.getenv("MANO_MODEL", DEFAULT_MODEL)
        self.endpoint = endpoint_from_base(os.getenv("MANO_BASE_URL", DEFAULT_BASE_URL))

    def complete(self, messages: list[dict[str, Any]], max_tokens: int = 700) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.01,
            "max_tokens": max_tokens,
            "stream": False,
            "enable_thinking": False,
            "vl_high_resolution_images": True,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(body)
                error = error_payload.get("error", {})
                error_code = error.get("code") or error.get("type")
            except (json.JSONDecodeError, AttributeError):
                error_code = None
            if error_code == "Arrearage":
                raise ManoError(
                    "The model provider rejected the call: account arrearage or "
                    "abnormal account status (Arrearage). Resolve billing, then re-run smoke."
                ) from exc
            raise ManoError(f"VLM HTTP {exc.code}: {body[:1200]}") from exc
        except urllib.error.URLError as exc:
            raise ManoNetworkError(f"network error calling the VLM: {exc}") from exc

    @staticmethod
    def content(response: dict[str, Any]) -> str:
        try:
            value = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ManoError(f"cannot parse VLM response: {response}") from exc
        if not isinstance(value, str):
            raise ManoError(f"VLM content is not a string: {value!r}")
        return value


# Backwards-friendly alias.
GuiPlus = GuiVLM
