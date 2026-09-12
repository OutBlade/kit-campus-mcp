"""Logging in to KIT Campus.

Authentication happens on the **portal**, not on the backend: campus.kit.edu's
own login handshake page (campus/login/chooseCampusUser.asp) has been removed
from KIT's server and returns 404, so the only way in is the route the portal's
own iframes take.

    campus.studium.kit.edu/Shibboleth.sso/Login?target=...
      -> idp.scc.kit.edu login form   (j_username / j_password / _eventId_proceed)
      -> optional attribute-release consent form
      -> auto-POST of the SAMLResponse back to the portal
    campus.studium.kit.edu/token.php   -> tokenA (short-lived) + identity
    campus.kit.edu/sp/...?login-token=<tokenA>&login-ts=<timestamp>

The SSO chain is walked generically - whatever form the IdP hands back gets
filled in and resubmitted - so a consent screen is handled automatically and an
unexpected step (a second factor, say) raises a descriptive error instead of
failing silently.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import (
    DEFAULT_ENCODING,
    FALLBACK_ENCODING,
    PORTAL_BASE,
    PORTAL_LOGIN_TARGET,
    TOKEN_URL,
    Settings,
)

LOGIN_ENTRY = (
    f"{PORTAL_BASE}/Shibboleth.sso/Login?target={quote(PORTAL_LOGIN_TARGET, safe='')}"
)
MAX_HOPS = 12

# token.php hands out a token valid for a few minutes; refresh a little early.
TOKEN_TTL_SECONDS = 240
REQUEST_ATTEMPTS = 3
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
logger = logging.getLogger(__name__)


class KitError(RuntimeError):
    """Base class for all errors raised by this package."""


class KitLoginError(KitError):
    """Login could not be completed."""


class KitAuthRequiredError(KitError):
    """A page needed a session but none was available."""


def safe_url(url: str) -> str:
    """Describe an endpoint without its login token, query, or fragment."""
    parsed = urlparse(str(url))
    return f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}"


class KitRequestError(KitError):
    """A request failed, with a diagnostic that contains no credentials."""


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        try:
            return min(30.0, max(0.0, float(response.headers["Retry-After"])))
        except (KeyError, ValueError):
            pass  # An absent or nonnumeric header uses the bounded backoff.
    return 2.0 ** attempt


@dataclass
class KitToken:
    """The short-lived credential the portal issues via /token.php.

    `token_a` authorises the CAS Campus backend (as `login-token` on pages and
    `token` on the JSON services); `token_b` is the equivalent for HISinOne.
    """

    token_a: str
    token_b: str
    timestamp: int
    username: str
    firstname: str
    lastname: str
    matriculation_number: str
    fetched_at: float

    @property
    def stale(self) -> bool:
        return (time.time() - self.fetched_at) > TOKEN_TTL_SECONDS

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.firstname, self.lastname) if part)


def _with_token(url: str, token: KitToken) -> str:
    """Append the login token to a backend URL."""
    separator = "&" if "?" in url else "?"
    return (
        f"{url}{separator}login-token={quote(token.token_a, safe='/@')}"
        f"&login-ts={token.timestamp}"
    )


@dataclass
class HtmlForm:
    """A parsed HTML form ready to be resubmitted."""

    action: str
    method: str
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def names(self) -> set[str]:
        return set(self.fields)


def parse_forms(html: str, base_url: str) -> list[HtmlForm]:
    """Extract every form on the page, pre-filled with its current values."""
    soup = BeautifulSoup(html, "html.parser")
    forms: list[HtmlForm] = []
    for node in soup.find_all("form"):
        action = urljoin(base_url, node.get("action") or base_url)
        method = (node.get("method") or "get").lower()
        fields: dict[str, str] = {}
        for inp in node.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "text").lower()
            if itype in {"checkbox", "radio"} and not inp.has_attr("checked"):
                continue
            if itype == "submit":
                continue
            fields[name] = inp.get("value") or ""
        for sel in node.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            chosen = sel.find("option", selected=True) or sel.find("option")
            fields[name] = (chosen.get("value") if chosen else "") or ""
        for area in node.find_all("textarea"):
            if area.get("name"):
                fields[area["name"]] = area.get_text()
        forms.append(HtmlForm(action=action, method=method, fields=fields))
    return forms


def _decode(response: httpx.Response) -> str:
    """Decode a CAS Campus / IdP response.

    The app declares "Charset=utf-8" and means it, but a few legacy pages still
    emit windows-1252, so fall back rather than filling the text with U+FFFD.
    """
    if response.charset_encoding:
        return response.text
    try:
        return response.content.decode(DEFAULT_ENCODING)
    except UnicodeDecodeError:
        return response.content.decode(FALLBACK_ENCODING, errors="replace")


def _is_idp(url: str) -> bool:
    return urlparse(str(url)).netloc.endswith("scc.kit.edu")


def _pick_login_form(forms: list[HtmlForm]) -> HtmlForm | None:
    for form in forms:
        if "j_username" in form.names:
            return form
    return None


def _pick_consent_form(forms: list[HtmlForm]) -> HtmlForm | None:
    for form in forms:
        if any(name.startswith("_shib_idp_consent") for name in form.names):
            return form
    return None


def _pick_saml_form(forms: list[HtmlForm]) -> HtmlForm | None:
    for form in forms:
        if "SAMLResponse" in form.names or "SAMLRequest" in form.names:
            return form
    return None


def _consent_fields(html: str, base_url: str) -> dict[str, str]:
    """Tick every attribute-release checkbox the consent page offers."""
    soup = BeautifulSoup(html, "html.parser")
    extra: dict[str, list[str]] = {}
    for inp in soup.find_all("input", attrs={"type": "checkbox"}):
        name = inp.get("name") or ""
        if name.startswith("_shib_idp_consent"):
            extra.setdefault(name, []).append(inp.get("value") or "")
    return {k: ",".join(v) for k, v in extra.items()}


class KitSession:
    """An authenticated (or anonymous) HTTP session against CAS Campus."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jar = http.cookiejar.LWPCookieJar(str(settings.cookie_file))
        if settings.cookie_file.exists():
            try:
                self._jar.load(ignore_discard=True, ignore_expires=True)
            except (http.cookiejar.LoadError, OSError):
                pass
        self._client = httpx.AsyncClient(
            # Hand httpx the LWPCookieJar itself. Wrapping it in httpx.Cookies
            # first would make httpx copy the contents into a fresh jar, so
            # everything the server set would be written to that copy and this
            # jar would save empty - the session would never survive a restart.
            cookies=self._jar,
            follow_redirects=True,
            timeout=settings.timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; kit-campus-mcp/0.1)",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
            },
        )
        self._logged_in = False
        self._token: KitToken | None = None
        self._token_attempted = False

    async def __aenter__(self) -> KitSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self.save_cookies()
        await self._client.aclose()

    def save_cookies(self) -> None:
        try:
            self._jar.save(ignore_discard=True, ignore_expires=True)
        except OSError:
            pass

    # ---------------------------------------------------------------- requests

    async def get(self, url: str, **kwargs: Any) -> tuple[str, str]:
        """GET a URL. Returns (decoded_html, final_url)."""
        response = await self._request("GET", url, **kwargs)
        return _decode(response), str(response.url)

    async def post(self, url: str, data: dict[str, str], **kwargs: Any) -> tuple[str, str]:
        """POST form data. Returns (decoded_html, final_url)."""
        response = await self._request("POST", url, data=data, **kwargs)
        return _decode(response), str(response.url)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Retry read requests only; never replay a password or SAML POST."""
        attempts = REQUEST_ATTEMPTS if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            response = None
            try:
                response = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                reason = type(exc).__name__
            except httpx.HTTPError as exc:
                raise KitRequestError(
                    f"{method} {safe_url(url)} failed ({type(exc).__name__})."
                ) from None
            else:
                if response.status_code < 400:
                    return response
                reason = f"HTTP {response.status_code}"
                if response.status_code not in RETRY_STATUSES:
                    raise KitRequestError(
                        f"{method} {safe_url(response.url)} failed ({reason})."
                    ) from None
            if attempt == attempts:
                raise KitRequestError(
                    f"{method} {safe_url(url)} failed after {attempts} "
                    f"attempt(s) ({reason}). No result data was read."
                ) from None
            delay = _retry_delay(response, attempt)
            logger.warning(
                "%s %s: %s; retry %d/%d in %.0fs",
                method, safe_url(url), reason, attempt + 1, attempts, delay,
            )
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def submit(self, form: HtmlForm) -> tuple[str, str]:
        if form.method == "post":
            return await self.post(form.action, form.fields)
        return await self.get(form.action, params=form.fields)

    # ------------------------------------------------------------------- auth

    async def _token_document(self) -> tuple[dict[str, Any], str]:
        """GET token.php. Logged out it answers HTTP 200 with `{}`, so an empty
        document - not an error status and not a redirect - is the signal that
        the portal session is missing."""
        html, final = await self.get(TOKEN_URL)
        try:
            data = json.loads(html)
        except json.JSONDecodeError:
            return {}, final
        return (data if isinstance(data, dict) else {}), final

    async def token(self, force: bool = False) -> KitToken:
        """Fetch (and cache) the portal token that authorises backend requests."""
        if not force and self._token is not None and not self._token.stale:
            return self._token

        data, final = await self._token_document()
        if not data.get("tokenA"):
            await self.login()
            data, final = await self._token_document()
        if not data.get("tokenA"):
            raise KitAuthRequiredError(
                f"Logged in, but {TOKEN_URL} returned no token (landed on "
                f"{safe_url(final)}). The account may not be an active student account, "
                "or KIT changed the portal."
            )
        self._token = KitToken(
            token_a=data["tokenA"],
            token_b=data.get("tokenB", ""),
            timestamp=int(data.get("timestamp", time.time())),
            username=data.get("username", ""),
            firstname=data.get("firstname", ""),
            lastname=data.get("lastname", ""),
            matriculation_number=str(data.get("matriculationNumber", "")),
            fetched_at=time.time(),
        )
        self._logged_in = True
        self.save_cookies()
        return self._token

    async def fetch_authenticated(self, url: str) -> tuple[str, str]:
        """GET a backend page, attaching the portal token that logs us in.

        CAS Campus accepts `login-token`/`login-ts` on any student URL and
        establishes its application session from them, which is how the portal's
        iframes authenticate.
        """
        token = await self.token()
        html, final = await self.walk_sso(*await self.get(_with_token(url, token)))
        if not _needs_login(html, final):
            return html, safe_url(final)
        # Token expired mid-flight, or the app session was dropped: get a fresh
        # one (logging in again if even that fails) and retry once.
        token = await self.token(force=True)
        html, final = await self.walk_sso(*await self.get(_with_token(url, token)))
        if _needs_login(html, final):
            raise KitAuthRequiredError(
                f"Still not authenticated after refreshing the token (landed on "
                f"{safe_url(final)}). The account may not have access to this page."
            )
        return html, safe_url(final)

    async def login(self) -> str:
        """Run the full Shibboleth login. Returns the URL that was landed on."""
        if not self.settings.has_credentials:
            raise KitLoginError(
                "No credentials configured. Set KIT_USERNAME and KIT_PASSWORD "
                "(your KIT account, e.g. ab1234) in the environment or in a .env file."
            )

        html, url = await self.walk_sso(*await self.get(LOGIN_ENTRY))
        self.save_cookies()
        self._logged_in = True
        return url

    async def walk_sso(self, html: str, url: str) -> tuple[str, str]:
        """Complete any Shibboleth round-trip the page is in the middle of.

        Both service providers (the portal and campus.kit.edu) bounce an
        unauthenticated request to the IdP, which answers with an auto-posting
        SAML form that a browser submits via JavaScript. httpx will not, so the
        chain has to be walked by hand: submit whatever form comes back until a
        real page appears. With an existing IdP session this passes through
        silently and never needs the password.
        """
        credentials_sent = False

        for _ in range(MAX_HOPS):
            forms = parse_forms(html, url)
            if not _is_idp(url) and _pick_saml_form(forms) is None:
                return html, url

            saml = _pick_saml_form(forms)
            if saml is not None:
                html, url = await self.submit(saml)
                continue

            consent = _pick_consent_form(forms)
            if consent is not None:
                consent.fields.update(_consent_fields(html, url))
                consent.fields["_eventId_proceed"] = ""
                html, url = await self.submit(consent)
                continue

            login_form = _pick_login_form(forms)
            if login_form is not None:
                if not self.settings.has_credentials:
                    raise KitLoginError(
                        "The IdP asked for a password but no credentials are "
                        "configured. Set KIT_USERNAME and KIT_PASSWORD (your KIT "
                        "account, e.g. ab1234) in the environment or a .env file."
                    )
                if credentials_sent:
                    raise KitLoginError(
                        "The IdP returned the login form again. The username or "
                        "password is most likely wrong. Check KIT_USERNAME / "
                        "KIT_PASSWORD; the username is the short KIT account "
                        "(e.g. ab1234), not the e-mail address."
                    )
                login_form.fields["j_username"] = self.settings.username or ""
                login_form.fields["j_password"] = self.settings.password or ""
                login_form.fields["_eventId_proceed"] = ""
                login_form.fields.pop("fudis_web_authn_assertion_input", None)
                html, url = await self.submit(login_form)
                credentials_sent = True
                continue

            raise KitLoginError(_unknown_step_message(html, url, forms))

        raise KitLoginError(
            f"Login did not finish within {MAX_HOPS} redirects (last URL: {safe_url(url)})."
        )

    @property
    def logged_in(self) -> bool:
        return self._logged_in


# Without a session CAS Campus answers with HTTP 200 and this page, so the
# status code says nothing - the body has to be matched instead.
SESSION_EXPIRED_MARKERS = (
    "sitzung abgelaufen",
    "sitzung ist abgelaufen",
    "noch nicht angemeldet",
    "session expired",
    "your session has expired",
    "not logged in",
)


def _needs_login(html: str, url: str) -> bool:
    """Heuristic: did we get bounced to the IdP, or served the expired-session page?"""
    if _is_idp(url):
        return True
    lowered = html.lower()
    if "j_username" in lowered:
        return True
    if "/campus/login/login.asp" in url or "sessiontimeout" in url.lower():
        return True
    return any(marker in lowered for marker in SESSION_EXPIRED_MARKERS)


def _unknown_step_message(html: str, url: str, forms: list[HtmlForm]) -> str:
    """Build an actionable error for an SSO step the flow cannot answer."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "") or "(no title)"
    field_names = sorted({name for form in forms for name in form.names})
    hint = ""
    joined = " ".join(field_names).lower() + " " + html.lower()
    if any(token in joined for token in ("otp", "totp", "token", "zweiter faktor", "second factor")):
        hint = (
            "\nThis looks like a second factor (MFA). Scripted password login "
            "cannot pass it. Log in once in a browser, copy the _shibsession_* "
            "and ASPSESSIONID cookies for campus.kit.edu, and put them in the "
            "cookie file, or use an account without MFA enforcement."
        )
    return (
        f"The SSO flow stopped at an unexpected step.\n"
        f"  URL:    {safe_url(url)}\n"
        f"  Title:  {title}\n"
        f"  Fields: {', '.join(field_names) or '(no form fields)'}{hint}"
    )
