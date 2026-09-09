"""Safe, machine-readable failure classification."""

import re


class Failure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            raise ValueError("Invalid failure code")
        super().__init__(code)
        self.code = code
        self.retryable = retryable
