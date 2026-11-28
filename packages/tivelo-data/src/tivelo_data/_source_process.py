"""Bounded JSON pipe and parent/worker lifetime for isolated data suppliers."""

import json
import multiprocessing
import os
import threading
from multiprocessing.connection import wait
from time import monotonic

from tivelo_contracts.errors import ErrorCode, TiveloError

MAX_FRAME = 16 * 1024 * 1024


def _send(pipe, message):
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_FRAME:
        raise ValueError("Data source frame exceeds its byte budget")
    pipe.send_bytes(data)


def _receive(pipe):
    message = json.loads(pipe.recv_bytes(MAX_FRAME).decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("Invalid source message")
    return message


def watch_owner(cleanup=None):
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch():
        wait([parent.sentinel])
        try:
            if cleanup is not None:
                cleanup()
        finally:
            os._exit(75)

    threading.Thread(target=watch, daemon=True, name="tivelo-source-owner").start()


class SourceProcess:
    def __init__(self, timeout, credential_ref):
        self.timeout = timeout
        self.credential_ref = credential_ref
        self.process = None
        self.pipe = None

    def start(self, target, arguments, context):
        context.check_cancelled()
        spawn = multiprocessing.get_context("spawn")
        self.pipe, other = spawn.Pipe()
        self.process = spawn.Process(
            name="tivelo-data-provider", target=target, daemon=True, args=(other, *arguments)
        )
        try:
            self.process.start()
        finally:
            other.close()
        self.response(context, "ready")

    def response(self, context, expected):
        deadline = monotonic() + self.timeout
        while True:
            context.check_cancelled()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "Data provider timed out")
            try:
                if not self.pipe.poll(min(remaining, 0.1)):
                    continue
                message = _receive(self.pipe)
                kind = message.get("type")
                if kind == expected:
                    return message
                if kind == "error":
                    code = ErrorCode(message["code"])
                    raise TiveloError(
                        code,
                        f"Data provider reported {code.value}",
                        retry_after=message.get("retry_after"),
                    )
                if kind != "credentials":
                    raise ValueError
                try:
                    binding = dict(context.resolve_credentials(self.credential_ref))
                    if not all(
                        isinstance(k, str) and isinstance(v, str) for k, v in binding.items()
                    ):
                        raise ValueError
                    _send(self.pipe, {"type": "credentials", "values": binding})
                except Exception:
                    raise TiveloError(
                        ErrorCode.AUTHENTICATION_FAILED, "Data source credentials unavailable"
                    ) from None
            except (OSError, EOFError, ValueError, RecursionError, TypeError, KeyError):
                raise TiveloError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    "Data provider stopped or returned an invalid response",
                ) from None

    def close(self):
        if self.pipe is not None:
            try:
                if self.process is not None and self.process.pid and self.process.is_alive():
                    _send(self.pipe, {"type": "close"})
            except OSError:
                pass
            finally:
                self.pipe.close()
        if self.process is not None and self.process.pid:
            self.process.join(0.4)
            if self.process.is_alive():
                self.process.kill()
                self.process.join()
            self.process.close()
        self.process = self.pipe = None
