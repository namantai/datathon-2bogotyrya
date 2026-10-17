"""Small dependency-free ASGI test helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI


SYNTHETIC_JPEG_BASE64 = (
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAPTGF2YzYzLjEuMTAyAP/bAEMACAoKCwoLDQ0NDQ0N"
    "EA8QEBAQEBAQEBAQEBISEhUVFRISEhAQEhIUFBUVFxcXFRUVFRcXGRkZHh4cHCMjJCsrM//E"
    "AEsAAQEAAAAAAAAAAAAAAAAAAAAIAQEAAAAAAAAAAAAAAAAAAAAAEAEAAAAAAAAAAAAAAAAA"
    "AAAAEQEAAAAAAAAAAAAAAAAAAAAA/8AAEQgACAAIAwEiAAIRAAMRAP/aAAwDAQACEQMRAD8A"
    "n8AH/9k="
)


@dataclass(frozen=True, slots=True)
class ASGIResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.content)


async def request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> ASGIResponse:
    """Issue one complete HTTP request directly against an ASGI app."""

    request_headers = {key.lower(): value for key, value in (headers or {}).items()}
    request_headers.setdefault("host", "testserver")
    request_headers.setdefault("content-length", str(len(body)))
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in request_headers.items()
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    messages: list[dict[str, Any]] = []
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)

    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in response_start.get("headers", [])
    }
    return ASGIResponse(
        status_code=response_start["status"],
        content=response_body,
        headers=response_headers,
    )


def multipart_file(
    content: bytes,
    *,
    filename: str = "synthetic.mov",
    field_name: str = "file",
    content_type: str = "video/quicktime",
) -> tuple[bytes, str]:
    boundary = "fortebank-synthetic-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    body += content
    body += f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"
