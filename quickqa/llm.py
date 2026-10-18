"""Planner clients (Claude Haiku 5.5 by default, Cerebras optional) and TypeSafe Jev (selector / verifier)."""
from __future__ import annotations

import base64
import json
import os
import time
import uuid

import requests

CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
RETRY_STATUS = {429, 500, 502, 503, 504, 529}


def _post(session: requests.Session, url: str, *, attempts: int = 4, **kw) -> dict:
    delay = 0.5
    for i in range(attempts):
        try:
            r = session.post(url, timeout=60, **kw)
        except requests.RequestException:
            if i == attempts - 1:
                raise
        else:
            if r.status_code not in RETRY_STATUS or i == attempts - 1:
                if r.status_code >= 400:
                    raise RuntimeError(f"{url} -> {r.status_code}: {r.text[:500]}")
                return r.json()
            ra = r.headers.get("retry-after")
            delay = float(ra) if ra and ra.replace(".", "").isdigit() else delay
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def _user_env(name: str) -> str | None:
    """Env var, falling back to the Windows per-user environment (a child process may not inherit it)."""
    val = os.environ.get(name)
    if val or os.name != "nt":
        return val
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


class PromptTooLarge(Exception):
    pass


class Claude:
    """Planner on the Claude API (default claude-haiku-5-5).

    Haiku 5.5 bills $0.10/MTok input only while a prompt is <= 100K tokens ($0.50 above), so every request is
    checked against `max_input_tokens` before it is sent: a cheap local estimate, and an exact count_tokens
    call only when the estimate gets near the limit. The caller shrinks the prompt on PromptTooLarge.
    """

    PRICE_IN, PRICE_OUT = 0.10, 0.50  # USD / MTok for prompts <= 100K tokens

    def __init__(self, model: str = "claude-haiku-5-5", effort: str = "medium", max_input_tokens: int = 100_000):
        import anthropic
        key = _user_env("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        # Pin the public endpoint: a host app (e.g. Claude Code) may export its own ANTHROPIC_BASE_URL.
        self.client = anthropic.Anthropic(api_key=key, max_retries=3, timeout=60.0,
                                          base_url=os.environ.get("QUICKQA_ANTHROPIC_BASE_URL",
                                                                  "https://api.anthropic.com"))
        self.model, self.effort, self.max_input_tokens = model, effort, max_input_tokens
        self.calls = self.input_tokens = self.output_tokens = self.peak_input_tokens = 0

    @staticmethod
    def _estimate(system: str, user: str, image_jpeg: bytes | None) -> int:
        # Deliberately pessimistic (Korean text and JSON run ~2-3 chars/token); images ~ w*h/750.
        est = (len(system) + len(user)) // 2
        if image_jpeg:
            from PIL import Image
            import io
            w, h = Image.open(io.BytesIO(image_jpeg)).size
            est += w * h // 750 + 100
        return est

    def json_chat(self, system: str, user: str, schema: dict, image_jpeg: bytes | None = None,
                  max_tokens: int = 8000) -> tuple[dict, dict]:
        content: list = []
        if image_jpeg:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(image_jpeg).decode()}})
        content.append({"type": "text", "text": user})
        kw = dict(model=self.model, system=[{"type": "text", "text": system}],
                  messages=[{"role": "user", "content": content}])
        if self._estimate(system, user, image_jpeg) > self.max_input_tokens * 0.6:
            n = self.client.messages.count_tokens(**kw).input_tokens
            if n > self.max_input_tokens:
                raise PromptTooLarge(n)
        if self.effort == "none":
            extra = dict(thinking={"type": "disabled"}, output_config={"effort": "low"})
        else:
            extra = dict(thinking={"type": "adaptive"}, output_config={"effort": self.effort})
        extra["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        resp = self.client.messages.create(max_tokens=max_tokens, **kw, **extra)
        u = resp.usage
        self.calls += 1
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.peak_input_tokens = max(self.peak_input_tokens, u.input_tokens)
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"planner refused: {getattr(resp.stop_details, 'category', None)}")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("planner hit max_tokens before finishing its JSON")
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text), {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}

    def usage_report(self) -> dict:
        return {"model": self.model, "calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "peak_input_tokens": self.peak_input_tokens,
                "est_cost_usd": round(self.input_tokens * self.PRICE_IN / 1e6
                                      + self.output_tokens * self.PRICE_OUT / 1e6, 5)}


class Cerebras:
    def __init__(self, model: str = "qwen-3.8-27b", reasoning_effort: str = "low"):
        key = os.environ.get("CEREBRAS_API_KEY")
        if not key:
            raise RuntimeError("CEREBRAS_API_KEY is not set")
        self.model, self.reasoning_effort = model, reasoning_effort
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})

    def json_chat(self, system: str, user: str, schema: dict, image_jpeg: bytes | None = None,
                  max_tokens: int = 4096) -> tuple[dict, dict]:
        content: list | str = user
        if image_jpeg:
            content = [{"type": "text", "text": user},
                       {"type": "image_url", "image_url": {
                           "url": "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode()}}]
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "reasoning_effort": self.reasoning_effort,
            "max_completion_tokens": max_tokens,
            "temperature": 0.2,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "step", "strict": True, "schema": schema}},
        }
        data = _post(self.s, CEREBRAS_URL, json=body)
        msg = data["choices"][0]["message"]
        self.calls = getattr(self, "calls", 0) + 1
        return json.loads(msg["content"]), data.get("usage", {})

    def usage_report(self) -> dict:
        return {"model": self.model, "calls": getattr(self, "calls", 0)}


class Jev:
    def __init__(self, model: str = "jev-latest", vision_model: str = "jev-preview"):
        key = os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        self.model, self.vision_model = model, vision_model
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})

    def ask(self, state, questions: dict, images: dict[str, bytes] | None = None) -> dict:
        """`images` maps a name to JPEG bytes; reference it in state/instructions via `image_ref(name)`."""
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {}
        if images or any(q.get("type") == "box" for q in questions.values()):
            body["model"] = self.vision_model
            body["assets"] = {ref: {"type": "image", "format": "jpg", "data_base64": base64.b64encode(b).decode()}
                              for ref, b in (images or {}).items()}
            headers["TypeSafe-Danger-Zone-Images"] = "true"
        return _post(self.s, TYPESAFE_URL, json=body, headers=headers)["answers"]


def new_image_ref(tag: str) -> str:
    return f"{uuid.uuid4().hex[:12]}-{tag}"
