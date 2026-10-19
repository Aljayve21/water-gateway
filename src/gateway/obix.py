"""oBIX REST client.

oBIX is the open XML object model for building automation, and every NeoView
installation ships with a Niagara 4 licence - Niagara exposes an oBIX interface
at ``/obix``. That makes it a far better front door to NeoView than guessing at
its database: it is a documented, stable, read/write REST API.

Typical reads against a Niagara oBIX interface::

    GET /obix/Ai/Site1/Totalizer/value
    GET /obix/Ai/Site1/?refs          # list everything in a folder

Responses are XML in the http://obix.org/ namespace::

    <val xmlns="http://obix.org/" is="obix:Point" name="Ai/Site1/Totalizer">
      <real val="1234.5" unit="obix:units/cubic-meters"/>
    </val>

Only the standard library is used: ``urllib`` plus ``xml.etree``. The gateway
has no HTTP dependency installed and this must not add one, so blocking calls
are pushed to a worker thread and the async wrappers stay non-blocking.

Security notes that shaped the design:
  * credentials are never logged and never appear in error messages;
  * TLS verification is on by default and only relaxed when explicitly asked;
  * redirects are not followed blindly - a redirect to another host would leak
    the Authorization header, so the target host is re-checked.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.cookiejar
import logging
import os
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("gateway.obix")

OBIX_NS = {"obix": "http://obix.org/"}

# oBIX leaf elements carry the value in a val attribute.
_VALUE_TAGS = ("real", "int", "bool", "string", "dateTime", "duration", "enum")

# ── Niagara web login (SCRAM) ────────────────────────────────────────────
# Niagara 4 stations authenticate the web login with SCRAM over the login
# endpoint. The scheme is advertised as "scram-sha512" but the shipped login
# page (login/digestLoginN4.js) drives it with SHA-256 keys, so that is what we
# reproduce. The endpoint requires TLS; the client upgrades itself if the
# station answers with a redirect to https.
_SCRAM_LOGIN_PATH = "j_security_check/"
_SCRAM_CONTENT_TYPE = "application/x-niagara-login-support"
_SCRAM_HASH = "sha256"
_SCRAM_CHANNEL_BINDING = "biws"  # base64("n,,") - no channel binding


class _LoginRedirect(Exception):
    """Raised internally when the login endpoint answers with a redirect."""

    def __init__(self, location: Optional[str]) -> None:
        super().__init__(location or "")
        self.location = location


def _origin_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _swap_to_https(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(("https", parts.netloc, parts.path, "", ""))


def _scram_username(username: str) -> str:
    """SASLprep-lite: NFKC-normalise and escape the SCRAM delimiters."""
    normalized = unicodedata.normalize("NFKC", username)
    return normalized.replace("=", "=3D").replace(",", "=2C")


def _scram_password(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def _scram_parse(message: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in message.split(","):
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
    return fields


class ObixError(RuntimeError):
    """An oBIX request failed. Message never contains credentials."""


@dataclass(frozen=True)
class ObixPoint:
    """One point read from an oBIX server."""

    name: str
    value: Any
    is_type: str
    unit: Optional[str] = None
    href: Optional[str] = None

    @property
    def as_float(self) -> Optional[float]:
        """Numeric value, or None when the point is not numeric/usable."""
        if isinstance(self.value, bool):
            return 1.0 if self.value else 0.0
        try:
            return float(self.value)
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class Facets:
    """Configuration facets a station publishes for a point.

    Niagara numeric points carry ``min``/``max``/``precision``/``unit`` as
    attributes on the point object itself - the very "point config" the
    validator reasons about, read live instead of guessed. Missing facets stay
    ``None`` so callers can tell "no facet" from "facet is zero".
    """

    unit: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    precision: Optional[float] = None
    is_type: str = ""
    href: Optional[str] = None

    @property
    def has_bounds(self) -> bool:
        return self.min_value is not None or self.max_value is not None

    @property
    def is_empty(self) -> bool:
        return not (self.unit or self.has_bounds or self.precision is not None)

    def as_dict(self) -> dict[str, Any]:
        """Only the facets that are actually present, ready for a cache file."""
        out: dict[str, Any] = {}
        if self.unit:
            out["unit"] = self.unit
        if self.min_value is not None:
            out["min"] = self.min_value
        if self.max_value is not None:
            out["max"] = self.max_value
        if self.precision is not None:
            out["precision"] = self.precision
        return out


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _parse_value(text: str) -> Any:
    """oBIX encodes booleans as 'true'/'false'; keep those as bool."""
    lowered = text.strip().lower()
    if lowered in ("true", "1"):
        return True
    if lowered in ("false", "0"):
        return False
    return text


def _point_from_value(element) -> ObixPoint:
    raw = element.get("val")
    if raw is None:
        raise ObixError("oBIX leaf element has no val attribute")
    tag = _strip_ns(element.tag)
    return ObixPoint(
        name=element.get("name") or "",
        value=_parse_value(raw),
        is_type=element.get("is") or f"obix:{tag}",
        unit=element.get("unit"),
        href=element.get("href"),
    )


def parse_point(xml: str) -> ObixPoint:
    """Parse a single-point response.

    Handles both shapes seen in the wild: a value element wrapped in a point
    object (mock/legacy ``<obj>``), and the value *as* the root element
    (Niagara returns ``<real val="..."/>`` for a point href).
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ObixError(f"malformed oBIX XML: {exc}") from exc

    root_tag = _strip_ns(root.tag)
    if root_tag == "err":
        raise ObixError(f"oBIX error: {root.get('display') or root.get('is') or 'err'}")
    if root_tag in _VALUE_TAGS:
        return _point_from_value(root)

    for child in root:
        if _strip_ns(child.tag) in _VALUE_TAGS:
            raw = child.get("val")
            if raw is None:
                raise ObixError("oBIX leaf element has no val attribute")
            return ObixPoint(
                name=root.get("name") or "",
                value=_parse_value(raw),
                is_type=child.get("is") or f"obix:{_strip_ns(child.tag)}",
                unit=child.get("unit"),
                href=root.get("href") or child.get("href"),
            )
    raise ObixError(f"no value element in oBIX response: {xml[:120]}")


def parse_refs(xml: str) -> list[ObixPoint]:
    """Parse a folder listing (``?refs``) into points.

    A ref may carry its value in ``val`` (``?refs``) or only a human ``display``
    (Niagara href listings); name/href/is are always kept so callers can walk
    the tree even when a value is absent.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ObixError(f"malformed oBIX XML: {exc}") from exc

    points: list[ObixPoint] = []
    for ref in root:
        if _strip_ns(ref.tag) != "ref":
            continue
        raw = ref.get("val")
        points.append(
            ObixPoint(
                name=ref.get("name") or "",
                value=_parse_value(raw) if raw is not None else ref.get("display"),
                is_type=ref.get("is") or "obix:Unknown",
                href=ref.get("href"),
            )
        )
    return points


def _attr_float(element, name: str) -> Optional[float]:
    """Read a numeric XML attribute, or ``None`` when absent/unparseable."""
    raw = element.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _facet_element(root):
    """The element carrying the facets: the value leaf, else the object root."""
    if _strip_ns(root.tag) in _VALUE_TAGS:
        return root
    for child in root:
        if _strip_ns(child.tag) in _VALUE_TAGS:
            return child
    return root


def parse_facets(xml: str) -> Facets:
    """Parse a point object into its configuration facets.

    Handles both shapes seen in the wild: facets on the value element
    (Niagara's ``<real val=".." min=".." max=".." unit=".."/>``) and facets on
    a wrapping object whose child holds the value. Absent facets stay ``None``;
    an empty result is not an error.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ObixError(f"malformed oBIX XML: {exc}") from exc

    if _strip_ns(root.tag) == "err":
        raise ObixError(f"oBIX error: {root.get('display') or root.get('is') or 'err'}")

    element = _facet_element(root)
    return Facets(
        unit=element.get("unit"),
        min_value=_attr_float(element, "min"),
        max_value=_attr_float(element, "max"),
        precision=_attr_float(element, "precision"),
        is_type=element.get("is") or "",
        href=element.get("href") or root.get("href"),
    )


class ObixClient:
    """Minimal oBIX REST client.

    Blocking by design (urllib); use :meth:`fetch_point` / :meth:`fetch_refs` for
    the async variants used inside the poll loop.
    """

    def __init__(
        self,
        base_url: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 10.0,
        verify_tls: bool = True,
        auth: str = "auto",
    ) -> None:
        if not base_url:
            raise ValueError("obix base_url is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._username = username or None
        self._password = password or ""
        # "basic"  - send an Authorization header (older Niagara, most oBIX).
        # "scram"  - Niagara 4 SCRAM-SHA-512 web login, then reuse the session.
        # "auto"   - try Basic first and fall back to SCRAM on a 401.
        self._auth_mode = (auth or "auto").lower()
        self._auth_header: Optional[str] = None
        self._logged_in = False
        self._cookies = http.cookiejar.CookieJar()
        context = (
            ssl.create_default_context()
            if verify_tls
            else ssl._create_unverified_context()
        )
        # Login opener carries the cookie jar so the SCRAM session is kept, and
        # refuses to auto-follow redirects (a redirect is treated as "use TLS").
        self._login_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies),
            urllib.request.HTTPSHandler(context=context),
            _DenyRedirect(),
        )
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )
        if username and self._auth_mode != "scram":
            token = base64.b64encode(
                f"{username}:{password or ''}".encode("utf-8")
            ).decode("ascii")
            self._auth_header = f"Basic {token}"

    # ── request plumbing ───────────────────────────────────────────────
    def _send(self, path: str, method: str = "GET", body: Optional[bytes] = None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", "application/xml")
        if body is not None:
            req.add_header("Content-Type", "application/xml")
        if self._auth_header:
            req.add_header("Authorization", self._auth_header)
        with self._opener.open(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _request(self, path: str, method: str = "GET", body: Optional[bytes] = None):
        if self._auth_mode == "scram":
            self._login()
        try:
            return self._send(path, method, body)
        except urllib.error.HTTPError as exc:
            # "auto": Basic was refused - switch to the SCRAM login and retry.
            if (
                exc.code == 401
                and self._auth_mode == "auto"
                and self._username
                and not self._logged_in
            ):
                self._auth_header = None
                self._login()
                return self._send(path, method, body)
            raise ObixError(f"HTTP {exc.code} from oBIX server") from exc
        except urllib.error.URLError as exc:
            raise ObixError(f"cannot reach oBIX server: {exc.reason}") from exc
        except OSError as exc:
            raise ObixError(f"oBIX request failed: {exc}") from exc

    # ── Niagara SCRAM web login ────────────────────────────────────────
    def _post_login_support(self, url: str, body: str) -> str:
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
        req.add_header("Content-Type", _SCRAM_CONTENT_TYPE)
        req.add_header("Accept", "*/*")
        try:
            with self._login_opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location") if exc.headers else None
                raise _LoginRedirect(location) from exc
            raise ObixError(f"SCRAM login failed (HTTP {exc.code})") from exc
        except urllib.error.URLError as exc:
            raise ObixError(f"cannot reach oBIX server: {exc.reason}") from exc

    def _scram_handshake(self, auth_url: str) -> None:
        hash_fn = getattr(hashlib, _SCRAM_HASH)
        client_nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        bare = f"n={_scram_username(self._username or '')},r={client_nonce}"
        server_first = self._post_login_support(
            auth_url,
            "action=sendClientFirstMessage&clientFirstMessage=n,," + bare,
        )
        fields = _scram_parse(server_first)
        if not {"r", "s", "i"} <= fields.keys():
            raise ObixError("SCRAM: incomplete server-first message")
        if not fields["r"].startswith(client_nonce):
            raise ObixError("SCRAM: server nonce does not extend the client nonce")

        salt = base64.b64decode(fields["s"])
        iterations = int(fields["i"])
        salted = hashlib.pbkdf2_hmac(
            _SCRAM_HASH,
            _scram_password(self._password).encode("utf-8"),
            salt,
            iterations,
            dklen=hash_fn().digest_size,
        )
        client_key = hmac.new(salted, b"Client Key", hash_fn).digest()
        stored_key = hash_fn(client_key).digest()

        without_proof = f"c={_SCRAM_CHANNEL_BINDING},r={fields['r']}"
        auth_message = f"{bare},{server_first.strip()},{without_proof}"
        client_signature = hmac.new(
            stored_key, auth_message.encode("utf-8"), hash_fn
        ).digest()
        proof = bytes(a ^ b for a, b in zip(client_key, client_signature))
        client_final = without_proof + ",p=" + base64.b64encode(proof).decode("ascii")

        server_final = self._post_login_support(
            auth_url,
            "action=sendClientFinalMessage&clientFinalMessage=" + client_final,
        )
        signature = _scram_parse(server_final).get("v")
        if not signature:
            raise ObixError("SCRAM authentication failed")
        expected = base64.b64encode(
            hmac.new(
                hmac.new(salted, b"Server Key", hash_fn).digest(),
                auth_message.encode("utf-8"),
                hash_fn,
            ).digest()
        ).decode("ascii")
        if not hmac.compare_digest(signature, expected):
            raise ObixError("SCRAM server signature mismatch")

    def _login(self) -> None:
        if self._logged_in or not self._username:
            return
        auth_url = _origin_of(self.base_url) + "/" + _SCRAM_LOGIN_PATH
        try:
            self._scram_handshake(auth_url)
        except _LoginRedirect as redirect:
            # Niagara requires TLS for the login endpoint; upgrade and retry so
            # the (Secure) session cookie is reused by later requests.
            if redirect.location and urllib.parse.urlsplit(redirect.location).scheme == "https":
                self.base_url = _swap_to_https(self.base_url)
                self._scram_handshake(
                    _origin_of(self.base_url) + "/" + _SCRAM_LOGIN_PATH
                )
            else:
                raise ObixError("oBIX login was redirected; check OBIX_BASE_URL") from redirect
        self._logged_in = True

    def get_point(self, path: str, *, raw: bool = False) -> ObixPoint:
        """Read one point.

        By default ``/value`` is appended (mock/legacy servers expose the value
        as a child). Pass ``raw=True`` for servers that return the value as the
        point object itself (Niagara), so the href is fetched unchanged.
        """
        if not raw and not path.endswith("/value"):
            path = f"{path.rstrip('/')}/value"
        return parse_point(self._request(path))

    def get_facets(self, path: str, *, raw: bool = True) -> Facets:
        """Read the configuration facets a station publishes for one point.

        Niagara exposes ``min``/``max``/``precision``/``unit`` on the point
        object itself; mock/legacy servers put them on the ``/value`` child.
        Both shapes are tried. An object with no facets is not an error - the
        caller decides what an empty result means.
        """
        base = path.rstrip("/")
        candidates = [base, f"{base}/value"] if raw else [f"{base}/value", base]
        best = Facets()
        saw_success = False
        last_error: Optional[ObixError] = None
        for candidate in candidates:
            try:
                xml = self._request(candidate)
                facets = parse_facets(xml)
            except ObixError as exc:
                last_error = exc
                continue
            # A readable object with no facets (a bool/occupancy point, say) is
            # a valid "no facets" answer, not a failure - keep it and don't let
            # a failing sibling candidate turn it into an error.
            saw_success = True
            if not facets.is_empty:
                return facets
            best = facets
        if saw_success:
            return best
        if last_error is not None:
            raise last_error
        return best

    def list_refs(self, path: str) -> list[ObixPoint]:
        """List everything in a folder.

        Niagara rejects the ``?refs`` query and returns the refs from the plain
        object; other servers require the query. Try the plain object first and
        fall back to ``?refs`` so both shapes work.
        """
        base = path.rstrip("/")
        candidates = [base]
        if "?" not in base:
            candidates.append(f"{base}?refs")
        for candidate in candidates:
            try:
                refs = parse_refs(self._request(candidate))
            except ObixError:
                continue
            if refs:
                return refs
        return []

    def write_point(self, path: str, value: Any) -> None:
        """Write a point value (used for AI results pushed back to the BMS)."""
        if not path.endswith("/value"):
            path = f"{path.rstrip('/')}/value"
        if isinstance(value, bool):
            payload = f'<?xml version="1.0" encoding="utf-8"?><bool xmlns="http://obix.org/" val="{"true" if value else "false"}"/>'
        elif isinstance(value, (int, float)):
            payload = f'<?xml version="1.0" encoding="utf-8"?><real xmlns="http://obix.org/" val="{float(value):.6g}"/>'
        else:
            escaped = (
                str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            payload = f'<?xml version="1.0" encoding="utf-8"?><string xmlns="http://obix.org/" val="{escaped}"/>'
        self._request(path, method="PUT", body=payload.encode("utf-8"))

    # ── async wrappers for the poll loop ───────────────────────────────
    async def fetch_point(self, path: str, *, raw: bool = False) -> ObixPoint:
        return await asyncio.to_thread(self.get_point, path, raw=raw)

    async def read_facets(self, path: str, *, raw: bool = True) -> Facets:
        return await asyncio.to_thread(self.get_facets, path, raw=raw)

    async def fetch_refs(self, path: str) -> list[ObixPoint]:
        return await asyncio.to_thread(self.list_refs, path)

    async def push_point(self, path: str, value: Any) -> None:
        await asyncio.to_thread(self.write_point, path, value)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so an Authorization header cannot be replayed elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise ObixError(f"oBIX server redirected ({code}); refusing to follow")


class _DenyRedirect(urllib.request.HTTPRedirectHandler):
    """Turn redirects into HTTPError so the caller can inspect the Location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None
