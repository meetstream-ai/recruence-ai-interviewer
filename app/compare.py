"""Comparing secrets (tokens, keys, signatures) in constant time, whatever text arrives.

`secrets.compare_digest` refuses two strings unless both are ASCII, so a link, key or header with an accented
character raised a TypeError and a 500 instead of a plain "not found" or "not signed in" (audit 2026-11-10, L1).
Comparing the UTF-8 bytes works for any text and stays constant-time."""

from __future__ import annotations

import hmac
from typing import Optional


def same(given: Optional[str], expected: Optional[str]) -> bool:
    """Whether `given` equals `expected`, in time that doesn't depend on where they differ. Empty never matches."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass"))
