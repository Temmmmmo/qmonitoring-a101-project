# -*- coding: utf-8 -*-
"""Explicit opt-in HTTP transport. No default server, redirects, credentials log."""
from __future__ import unicode_literals

import hashlib
import json
import math
import os

try:
    from urllib.parse import urlsplit, urlunsplit
except ImportError:
    from urlparse import urlsplit, urlunsplit

from qm_revit_probe import text_type
from qm_trial_input import _unique_object, _reject_constant
from qm_revit_source_preview import _finite_tree

MAX_SOURCE_BYTES, MAX_RESPONSE_BYTES = 30*1024*1024, 32*1024*1024
CALCULATION_SCHEMA = "qmonitoring-workflow-calculation-request/v1"
ANALYSIS_SCHEMA = "qmonitoring-workflow-analysis/v1"


def server_endpoint(value):
    if not isinstance(value, text_type) or not value or len(value) > 2000 or any(ord(c) < 32 for c in value):
        raise ValueError("Enter an explicit trusted server URL")
    url = urlsplit(value)
    if url.username is not None or url.password is not None or url.query or url.fragment:
        raise ValueError("Do not put credentials, query strings or fragments in the server URL")
    if not url.hostname or url.scheme not in ("http", "https"):
        raise ValueError("A full HTTP(S) URL is required")
    if url.scheme == "http" and url.hostname.lower() not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Remote servers require HTTPS; HTTP is allowed only for loopback")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("Invalid server port")
    path = url.path.rstrip("/")
    if not path.endswith("/api/revit/workflow/analyze"):
        path += "/api/revit/workflow/analyze"
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def read_source(path, suffix):
    if os.path.splitext(path)[1].lower() != suffix:
        raise ValueError("Choose an actual "+suffix+" source")
    with open(path, "rb") as stream:
        data = stream.read(MAX_SOURCE_BYTES+1)
    if not 0 < len(data) <= MAX_SOURCE_BYTES:
        raise ValueError("Source is empty or exceeds 30 MiB")
    name = os.path.basename(path.replace("\\", "/"))
    if any(ord(char) < 32 or char == '"' for char in name):
        raise ValueError("Unsupported source filename")
    return {"filename": name, "content": data, "sha256": hashlib.sha256(data).hexdigest()}


def calculation_request(direction, config, profile, mapping_id, sources):
    chosen = {}
    for key in ("background_diameter_mm", "background_step_mm", "anchorage_diameters",
                "minimum_zone_fe_count", "algorithm", "mass_preference"):
        chosen[key] = config[key]
    hashes = {}
    for role, source in sources.items():
        hashes[role] = source["sha256"]
    data = {"schema_version": CALCULATION_SCHEMA, "direction": direction, "settings": chosen,
        "axis_profile": profile, "mapping_id": mapping_id, "source_sha256": hashes}
    _finite_tree(data)
    return json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_analysis(content, request_bytes=None):
    if not 0 < len(content) <= MAX_RESPONSE_BYTES:
        raise ValueError("Empty or oversized calculation response")
    def finite_float(value):
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            raise ValueError("Non-finite response number")
        return result
    data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant, parse_float=finite_float)
    _finite_tree(data)
    if (not isinstance(data, dict) or data.get("schema_version") != ANALYSIS_SCHEMA
            or data.get("units") != "mm" or data.get("calculation_performed") is not True
            or data.get("placement_eligible") is not False or data.get("engineering_approval") is not False
            or data.get("source_demand_preserved") is not True):
        raise ValueError("Not a complete graphic-only workflow analysis response")
    if request_bytes is not None:
        expected = json.loads(request_bytes.decode("utf-8"))
        if (data.get("request_sha256") != hashlib.sha256(request_bytes).hexdigest()
                or data.get("request") != expected or data.get("source_sha256") != expected["source_sha256"]):
            raise ValueError("Response does not match the exact confirmed request/source bytes")
    return data


def post_calculation(url, request_bytes, sources, confirmed=False, bearer_token=None):
    """.NET HTTP is available in IronPython; certificate verification stays enabled.

    No transaction is held during the request. No auto redirect/retry or default
    credentials. Authentication token stays only in memory and is never reported.
    """
    endpoint = server_endpoint(url)
    if confirmed is not True:
        raise ValueError("Explicit consent to send these source files to this server is required")
    if bearer_token and (not isinstance(bearer_token, text_type) or len(bearer_token) > 8192
                         or any(ord(c) <= 32 for c in bearer_token)):
        raise ValueError("Invalid bearer token")
    import clr
    clr.AddReference("System.Net.Http")
    from System import Array, Byte, Int64, TimeSpan
    from System.Net.Http import HttpClient, HttpClientHandler, MultipartFormDataContent, ByteArrayContent, StringContent
    from System.Net.Http.Headers import AuthenticationHeaderValue
    handler, client, multipart, response = None, None, None, None
    try:
        handler = HttpClientHandler()
        handler.AllowAutoRedirect = False
        handler.UseCookies = False
        handler.UseDefaultCredentials = False
        client = HttpClient(handler)
        client.Timeout = TimeSpan.FromSeconds(300)
        if bearer_token:
            client.DefaultRequestHeaders.Authorization = AuthenticationHeaderValue("Bearer", bearer_token)
        multipart = MultipartFormDataContent()
        multipart.Add(StringContent(request_bytes.decode("utf-8")), "request_json")
        for role in ("dxf", "shk"):
            if role not in sources:
                continue
            source = sources[role]
            content = ByteArrayContent(Array[Byte](bytearray(source["content"])))
            multipart.Add(content, role, source["filename"])
        # HeadersRead avoids buffering an unbounded remote body before our cap.
        from System.Net.Http import HttpRequestMessage, HttpMethod, HttpCompletionOption
        request = HttpRequestMessage(HttpMethod.Post, endpoint)
        request.Content = multipart
        try:
            response = client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead).GetAwaiter().GetResult()
            if not response.IsSuccessStatusCode:
                raise ValueError("Calculation server HTTP {0}; no family placement started".format(int(response.StatusCode)))
            buffering = response.Content.LoadIntoBufferAsync(Int64(MAX_RESPONSE_BYTES))
            # HeadersRead ends HttpClient.Timeout at the headers. Bound body time
            # separately; disposal closes the transfer on timeout.
            if not buffering.Wait(TimeSpan.FromSeconds(30)):
                raise ValueError("Calculation response body timed out; no family placement started")
            buffering.GetAwaiter().GetResult()
            raw = response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            return decode_analysis(bytes(bytearray(raw)), request_bytes)
        finally:
            request.Dispose()
    except ValueError:
        raise
    except Exception:
        # Network exceptions may include URL/authorization diagnostics; do not log them.
        error = ValueError("Calculation HTTP transport failed or timed out; no family placement started")
        error.__suppress_context__ = True
        raise error
    finally:
        for item in (response, multipart, client, handler):
            if item is not None:
                item.Dispose()
