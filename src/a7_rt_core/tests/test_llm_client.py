"""
Extracted inline tests for llm_client.py.
Run standalone: python tests/test_llm_client.py
"""

import os
import sys

# Allow imports from the parent (a7-rt-core) directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest.mock
from typing import Optional

from a7_rt_core.llm.client import LLMAuthenticationError, LLMClient, LLMError


def _test_llm_client() -> None:
    """
    Verify:
      1.  Successful call returns text content
      2.  tools parameter is forwarded to the API call
      3.  APIError triggers retry up to max_retries, then raises LLMError
      4.  AuthenticationError raises immediately without retry
      5.  LLMError carries cause, attempt_count, model
    """
    ok_str = "\033[32mOK\033[0m"
    fail_str = "\033[31mFAIL\033[0m"
    errors: list[str] = []

    def check(label: str, condition: bool) -> None:
        if condition:
            print(f"  [{ok_str}] {label}")
        else:
            print(f"  [{fail_str}] {label}")
            errors.append(label)

    def expect_raise(label: str, exc_type: type, fn) -> Optional[Exception]:
        try:
            fn()
            print(f"  [{fail_str}] {label}  (no exception raised)")
            errors.append(label)
            return None
        except exc_type as e:
            print(f"  [{ok_str}] {label}")
            return e
        except Exception as e:
            print(f"  [{fail_str}] {label}  (wrong type: {type(e).__name__}: {e})")
            errors.append(label)
            return None

    # ── Stub SDK types (openai chat.completions shape) ────────────────────────

    class _Message:
        def __init__(self, text: str):
            self.content = text

    class _Choice:
        def __init__(self, text: str):
            self.message = _Message(text)

    class _Response:
        def __init__(self, text: str):
            self.choices = [_Choice(text)]

    class _APIError(Exception):
        pass

    class _AuthenticationError(Exception):
        # Named to match the openai SDK class name used in LLMClient
        pass

    _AuthenticationError.__name__ = "AuthenticationError"
    _AuthenticationError.__qualname__ = "AuthenticationError"

    # ── Test 1: Successful call returns text ──────────────────────────────────

    class _OKCompletions:
        def __init__(self):
            self.calls: list[dict] = []

        def create(self, **kwargs) -> _Response:
            self.calls.append(kwargs)
            return _Response("hello from llm")

    class _OKChat:
        def __init__(self):
            self.completions = _OKCompletions()

    class _OKClient:
        def __init__(self):
            self.chat = _OKChat()

    ok_sdk = _OKClient()
    lc = LLMClient(ok_sdk, model="test-model", max_retries=3)
    result = lc.call(system="sys", user="usr")
    check("successful call returns text", result == "hello from llm")

    # ── Test 2: system msg in messages array, tools forwarded ────────────────

    tools_def = [{"name": "search", "description": "search", "input_schema": {}}]
    lc.call(system="sys", user="usr", tools=tools_def)
    last_call = ok_sdk.chat.completions.calls[-1]
    check("tools forwarded to API", last_call.get("tools") == tools_def)
    check(
        "system in messages array",
        last_call["messages"][0] == {"role": "system", "content": "sys"},
    )

    lc.call(system="sys", user="usr")  # no tools
    check(
        "tools absent from call when not provided",
        "tools" not in ok_sdk.chat.completions.calls[-1],
    )

    # ── Test 3: APIError retries up to max_retries then raises LLMError ───────

    class _ErrorCompletions:
        def __init__(self, exc):
            self._exc = exc
            self.calls: list[dict] = []

        def create(self, **kwargs) -> _Response:
            self.calls.append(kwargs)
            raise self._exc

    class _ErrorChat:
        def __init__(self, exc):
            self.completions = _ErrorCompletions(exc)

    class _ErrorClient:
        def __init__(self, exc):
            self.chat = _ErrorChat(exc)

    api_err_client = _ErrorClient(_APIError("rate limited"))
    lc_err = LLMClient(
        api_err_client, model="test-model", max_retries=3, base_delay=0.0
    )
    with unittest.mock.patch("time.sleep"):
        exc = expect_raise(
            "APIError → LLMError after retries",
            LLMError,
            lambda: lc_err.call(system="s", user="u"),
        )

    check("3 attempts made", len(api_err_client.chat.completions.calls) == 3)
    if exc:
        check("LLMError.attempt_count == 3", exc.attempt_count == 3)
        check("LLMError.model correct", exc.model == "test-model")
        check("LLMError.cause is original exc", isinstance(exc.cause, _APIError))

    # ── Test 4: AuthenticationError raises immediately, no retry ─────────────

    auth_exc = _AuthenticationError("bad key")
    auth_client = _ErrorClient(auth_exc)
    lc_auth = LLMClient(auth_client, model="test-model", max_retries=3, base_delay=0.0)
    exc2 = expect_raise(
        "AuthenticationError → immediate raise",
        LLMAuthenticationError,
        lambda: lc_auth.call(system="s", user="u"),
    )
    check("no retry on auth error", len(auth_client.chat.completions.calls) == 1)
    if exc2:
        check("LLMAuthenticationError.attempt_count == 1", exc2.attempt_count == 1)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if errors:
        print(f"\033[31m{len(errors)} test(s) failed:\033[0m")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\033[32mAll llm_client tests passed.\033[0m")


if __name__ == "__main__":
    print("Running llm_client tests...")
    _test_llm_client()
