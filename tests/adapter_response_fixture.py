"""Mock HTTP responses that exercise the same raw streaming boundary as a socket."""

import json as json_module

import httpx


def streamed_response(status_code, *, json=None, content=None, headers=None, **kwargs):
    body = json_module.dumps(json).encode() if json is not None else (content or b"")
    if isinstance(body, str):
        body = body.encode()
    return httpx.Response(status_code, stream=httpx.ByteStream(body), headers=headers, **kwargs)
