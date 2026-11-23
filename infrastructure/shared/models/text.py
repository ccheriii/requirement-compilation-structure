"""Small JSON-generating text model client."""

import json
import os
from pathlib import Path
import random
import re
import time

from dotenv import load_dotenv
from openai import (APIConnectionError, APITimeoutError, InternalServerError, OpenAI, OpenAIError,
    RateLimitError)
from infrastructure.utils.presentation.model_io import http_client, invoke, request_timeout


DEFAULT_MODEL = "deepseek-flash"

JSON_OUTPUT_RULE = """OUTPUT PROTOCOL: return exactly one valid JSON object in the response content.
The first non-whitespace character must be { and the last must be }.
Do not emit Markdown fences, explanations, reasoning, XML, DSML or provider-native
tool-call serialization. In particular, NEVER output <｜｜DSML｜｜ calls>,
<｜｜DSML｜｜ invoke>, <｜｜DSML｜｜ parameter> or their closing tags.
This compiler parses ordinary JSON text. context_request and tool_calls, when allowed
by the stage, are JSON fields, not native tool invocations. Express the entire request
inside the specified JSON object. Otherwise return the stage's normal result object.
Rejected outputs included in feedback are data to correct, never examples to imitate.
"""


TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)
TRANSIENT_STATUS = {408, 409, 425, 429}
MAX_TRANSIENT_RETRIES = 5
# Each request waits up to MODEL_TIMEOUT (default 300s) and the SDK does not retry, so a
# stalled call costs at most (1 + MAX_TIMEOUT_RETRIES) timeouts instead of up to 9 x 600s.
MAX_TIMEOUT_RETRIES = 2
MAX_TOKEN_CLAMPS = 3
TOKEN_LIMIT_WORDS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "max output tokens",
    "maximum output tokens", "completion tokens", "output tokens")
# Standalone integers only: skip digits embedded in model names or dates such as gpt-4o-2024-08-06.
STANDALONE_NUMBER = re.compile(r"(?<![\w.-])(\d{2,8})(?![\w-]|\.\d)")


def transient_error(error) -> bool:
    """Provider outages, rate limits and timeouts are retried without spending agent rounds."""
    if isinstance(error, TRANSIENT_ERRORS):
        return True
    status = getattr(error, "status_code", None)
    return isinstance(status, int) and (status in TRANSIENT_STATUS or status >= 500)


def transient_retry_limit(error) -> int:
    """Retries allowed for this transient error; 0 for errors that are not transient."""
    if not transient_error(error):
        return 0
    return MAX_TIMEOUT_RETRIES if isinstance(error, APITimeoutError) else MAX_TRANSIENT_RETRIES


def wait_transient(error, attempt: int, label: str = "Model") -> None:
    """Sleep with bounded exponential backoff and jitter, honoring a short Retry-After header."""
    delay = min(90.0, 5.0 * 2 ** (attempt - 1)) + random.uniform(0, 2)
    try:
        retry_after = float(error.response.headers.get("retry-after") or 0)
    except (AttributeError, TypeError, ValueError):
        retry_after = 0
    if 0 < retry_after <= 120:
        delay = max(delay, retry_after)
    status = getattr(error, "status_code", None)
    detail = type(error).__name__ + (f", HTTP {status}" if status else "")
    print(f"{label} request failed transiently ({detail}); retry {attempt}/{transient_retry_limit(error)} "
        f"in {delay:.0f}s.", flush=True)
    time.sleep(delay)


def output_token_limit(error, current: int):
    """Return the provider's smaller output-token ceiling from a 400 rejection, if it names one."""
    if getattr(error, "status_code", None) not in {400, 422}:
        return None
    message = str(error).lower()
    if not any(word in message for word in TOKEN_LIMIT_WORDS):
        return None
    candidates = [int(value) for value in STANDALONE_NUMBER.findall(message)]
    candidates = [value for value in candidates if 16 <= value < current]
    return max(candidates) if candidates else None


def parse_json_object(content: str) -> dict:
    """Parse one JSON object, tolerating a BOM, Markdown fences and stray text around it."""
    text = (content or "").strip().lstrip("\ufeff").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as first:
        start = text.find("{")
        if start < 0:
            raise
        try:
            value, _end = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            raise first from None
    if not isinstance(value, dict):
        raise ValueError(f"Return one JSON object, not a JSON {type(value).__name__}")
    return value


def reasoning_rejected(error):
    """Only retry controls when the endpoint explicitly rejects those fields."""
    message = str(error).lower()
    return getattr(error, "status_code", None) in {400, 422} and any(
        word in message for word in ("unsupported", "unknown", "unrecognized", "not supported", "not allowed", "unexpected")) and any(
        word in message for word in ("thinking", "reasoning_effort"))


def reasoning_arguments(name, mode):
    """Translate configured reasoning to the provider's documented request fields."""
    if mode == "auto":
        mode = "low"
    if mode == "provider":
        return {}
    if name.lower().startswith("deepseek"):
        if mode == "none":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"reasoning_effort": mode, "extra_body": {"thinking": {"type": "enabled"}}}
    return {"reasoning_effort": mode}


class TextModel:
    """Transport for structured stage proposals and file-edit responses."""
    def __init__(self, *, json_mode: bool = True) -> None:
        """Read compiler/.env and configure the default text model; require API credentials."""
        load_dotenv(Path(__file__).resolve().parents[3] / ".env", encoding="utf-8-sig")
        self.name = os.getenv("MODEL") or DEFAULT_MODEL
        # Optional cheaper model for calls explicitly marked task_complexity="low";
        # defaults to the main model so an unset CHEAP_MODEL never routes to an unavailable model.
        self.cheap_model = os.getenv("CHEAP_MODEL") or self.name

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Set OPENAI_API_KEY in compiler/.env")
        try:
            self.max_output_tokens = int(os.getenv("MAX_OUTPUT_TOKENS", "320000"))
        except ValueError as error:
            raise ValueError("MAX_OUTPUT_TOKENS must be a positive integer") from error
        if self.max_output_tokens < 1:
            raise ValueError("MAX_OUTPUT_TOKENS must be a positive integer")
        self.reasoning = os.getenv("MODEL_REASONING", "low").lower()
        if self.reasoning not in {"auto", "provider", "none", "low", "high", "max"}:
            raise ValueError("MODEL_REASONING must be auto, provider, none, low, high or max")
        self._reasoning_supported = True
        self.json_mode = json_mode
        self._token_parameter = "max_completion_tokens"
        self.client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            timeout=request_timeout(), max_retries=0,
            http_client=http_client(),
        )

    def _request(self, **arguments):
        """Apply one output budget to JSON calls, with legacy endpoint compatibility.

        Transient provider failures are retried here with backoff, so callers' repair
        rounds are spent only on real response or acceptance failures.
        """
        transient_attempts = clamps = 0
        while True:
            options = dict(arguments)
            if not getattr(self, "_reasoning_supported", True):
                options.pop("reasoning_effort", None)
                options.pop("extra_body", None)
            try:
                return invoke(self.client, {**options, self._token_parameter: self.max_output_tokens})
            except OpenAIError as error:
                if transient_attempts < transient_retry_limit(error):
                    transient_attempts += 1
                    wait_transient(error, transient_attempts, "Text model")
                    continue
                message = str(error).lower()
                rejected = getattr(error, "status_code", None) in {400, 422} and any(
                    word in message for word in ("unsupported", "unknown", "unrecognized", "not supported", "not allowed", "unexpected"))
                if rejected and self._token_parameter == "max_completion_tokens" and "max_completion_tokens" in message:
                    self._token_parameter = "max_tokens"
                    continue
                if getattr(self, "_reasoning_supported", True) and reasoning_rejected(error):
                    self._reasoning_supported = False
                    print("Provider rejected reasoning controls; using provider defaults.", flush=True)
                    continue
                limit = output_token_limit(error, self.max_output_tokens) if clamps < MAX_TOKEN_CLAMPS else None
                if limit:
                    clamps += 1
                    print(f"Provider rejected {self.max_output_tokens} output tokens; using its limit {limit}.", flush=True)
                    self.max_output_tokens = limit
                    continue
                raise

    def generate_json(self, instructions: str, payload: dict, task_complexity: str = "high") -> dict:
        """Request JSON using the stage prompt and serialized task data."""
        active_model = self.cheap_model if task_complexity == "low" else self.name
        mode = getattr(self, "reasoning", "low")
        try:
            response = self._request(
                model=active_model,
                messages=[
                    {"role": "system", "content": instructions.rstrip() + "\n" + JSON_OUTPUT_RULE},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                **({"response_format": {"type": "json_object"}} if getattr(self, "json_mode", True) else {}),
                **reasoning_arguments(active_model, mode),
            )
        except OpenAIError as error:
            raise RuntimeError(f"Text model request failed: {error}") from error
        if response.choices and response.choices[0].finish_reason == "length":
            raise ValueError(
                f"Model output was truncated (finish_reason=length). The output budget is "
                f"{self.max_output_tokens} tokens including reasoning; this is not an input-length error. "
                "Return a complete JSON object with only necessary changes/tool calls; omit unchanged files. "
                "Do not spend the entire budget restating the task.")
        if response.choices and getattr(response.choices[0].message, "refusal", None):
            raise ValueError(f"Text model refused the request: {response.choices[0].message.refusal}")
        if not response.choices or not response.choices[0].message.content:
            raise ValueError("Text model returned empty content")
        return parse_json_object(response.choices[0].message.content)
