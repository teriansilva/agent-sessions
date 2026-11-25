"""Fixture: the standard library's own ways out of the process.

Neither of these imports httpx or spells a method `post`, and neither payload is readable —
the message is built from a role map and the URL from pieces. What they cannot avoid is
pulling in a network library and making a call, which is what the inventory is about.
"""

import http.client
from urllib import request

SYSTEM = "You are a helpful assistant."
ROLE = "system"
ROLE_FIELDS = {"role": ROLE}


def via_urlopen():
    message = dict(ROLE_FIELDS, content=SYSTEM)
    body = {"model": "m", "messages": [message]}
    return request.urlopen("https://ai.example/v1/chat/" + "completions", data=repr(body).encode())


def via_http_client():
    message = dict(ROLE_FIELDS, content=SYSTEM)
    conn = http.client.HTTPSConnection("ai.example")
    conn.request("POST", "/v1/chat/" + "completions", repr({"messages": [message]}))
    return conn.getresponse()
