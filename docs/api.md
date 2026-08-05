# HTTP API

SPEC section 6 lists this file as generated from the OpenAPI schema, and
SPEC section 11.2 gives the contract it will describe.

> **Status: no API yet.** The FastAPI application arrives with M2 and M4;
> milestone M1 is instrument control only. The single entry point today is the
> command line:
>
> ```bash
> .venv/bin/nocturne check-config
> ```
>
> which validates `config/*.yaml`, prints the configured rig, and exits non-zero
> with the offending field named if anything is wrong.

When the application exists, this file is regenerated from its OpenAPI schema
rather than written by hand. The endpoints and the two WebSocket streams are
specified in SPEC section 11.2, and every one of them is authenticated with a
bearer token even on the LAN: an unauthenticated `POST /abort` is a lost night.
