"""Official Sign in with ChatGPT credentials for a local/self-hosted runtime.

Credentials belong to this app, not Codex. No existing application's credentials
are imported. The default storage directory is outside the repository.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
import uuid
import webbrowser

import jwt
import requests


ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
APP_NAME = "Mer Portfolio"
_PROFILE_ID = re.compile(r"^[a-f0-9]{24}$")
_ISSUED_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_TOKEN_KEYS = ("access_token", "refresh_token", "id_token")


class ChatGPTAuthError(RuntimeError):
    """A safe-to-display authentication error, without credentials or URLs."""

    def __init__(self, message: str, *, oauth_error: str | None = None):
        super().__init__(message)
        self.oauth_error = oauth_error


def _matches(value, expected: str) -> bool:
    return isinstance(value, str) and hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))


def default_auth_dir() -> Path:
    configured = os.environ.get("CHATGPT_AUTH_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "mer-portfolio" / "chatgpt"


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ChatGPTAuthError("Credential storage must not be a symlink.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _read_json(path: Path) -> dict:
    if path.is_symlink():
        raise ChatGPTAuthError("Credential file must not be a symlink.")
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ChatGPTAuthError("Credential file could not be read.") from None
    if not isinstance(value, dict):
        raise ChatGPTAuthError("Credential file has an invalid format.")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ChatGPTAuthError("Credential file permissions must be owner-only (0600).")
    return value


def _atomic_private_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ChatGPTAuthError("Credential file must not be a symlink.")
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CredentialStore:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory or default_auth_dir()).expanduser().absolute()
        # Explicitly prevent env configuration from writing credentials into git.
        for ancestor in (Path(__file__).resolve().parent, *Path(__file__).resolve().parents):
            if (ancestor / ".git").exists():
                if self.directory.resolve().is_relative_to(ancestor):
                    raise ChatGPTAuthError("Keep CHATGPT_AUTH_DIR outside the repository.")
                break
        _private_directory(self.directory)
        self.profiles_dir = self.directory / "profiles"
        _private_directory(self.profiles_dir)

    @contextmanager
    def locked(self):
        """Serialize reads/refresh/writes across processes, including rotation."""
        lock_path = self.directory / ".lock"
        if lock_path.is_symlink():
            raise ChatGPTAuthError("Credential lock must not be a symlink.")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + 40
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        if os.fstat(fd).st_size == 0:
                            os.write(fd, b"0")
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise ChatGPTAuthError("Credential refresh is busy; try again.") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)  # Closing the descriptor also releases its lock.

    def host_id(self) -> str:
        with self.locked():
            path = self.directory / "host.json"
            value = _read_json(path)
            if not value.get("ext_agent_host_id"):
                value = {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())}
                _atomic_private_json(path, value)
            return value["ext_agent_host_id"]

    def _path(self, account: str) -> Path:
        if not _PROFILE_ID.fullmatch(account):
            raise ChatGPTAuthError("Unknown ChatGPT account label.")
        return self.profiles_dir / (account + ".json")

    def selected_account(self, account: str | None = None) -> str:
        value = account or os.environ.get("CHATGPT_ACCOUNT")
        if not value:
            value = _read_json(self.directory / "active.json").get("account")
        if not value or not _PROFILE_ID.fullmatch(value):
            raise ChatGPTAuthError("No ChatGPT account selected; run python chatgpt_auth.py login.")
        return value

    def read_profile(self, account: str | None = None) -> dict:
        account = self.selected_account(account)
        profile = _read_json(self._path(account))
        if not profile or profile.get("account") != account:
            raise ChatGPTAuthError("Selected ChatGPT account was not found.")
        return profile

    def save_profile(self, profile: dict, *, activate: bool = False) -> str:
        identity = "\0".join((profile["issuer"], profile["subject"], profile["client_id"]))
        account = hashlib.sha256(identity.encode()).hexdigest()[:24]
        record = dict(profile, account=account)
        _atomic_private_json(self._path(account), record)
        if activate:
            _atomic_private_json(self.directory / "active.json", {"account": account})
        return account

    def select(self, account: str) -> None:
        with self.locked():
            self.read_profile(account)
            _atomic_private_json(self.directory / "active.json", {"account": account})

    def status(self) -> dict:
        """Return only non-secret metadata; never token payloads or hints."""
        active = _read_json(self.directory / "active.json").get("account")
        profiles = []
        for path in sorted(self.profiles_dir.glob("*.json")):
            record = _read_json(path)
            profiles.append({
                "account": record.get("account"),
                "email": record.get("email", ""),
                "active": record.get("account") == active,
                "signed_in": bool(record.get("access_token") and record.get("refresh_token")),
                "expires_at": record.get("expires_at"),
            })
        return {"accounts": profiles}


class ChatGPTAuth:
    def __init__(self, store: CredentialStore | None = None, *, session=None, account: str | None = None):
        self.store = store or CredentialStore()
        self.session = session or requests.Session()
        self.account = account
        self._configuration = None

    @staticmethod
    def _json_response(response, operation: str) -> dict:
        if not 200 <= response.status_code < 300:
            try:
                failure = response.json()
            except ValueError:
                failure = {}
            if isinstance(failure, dict) and failure.get("error") == "invalid_grant":
                raise ChatGPTAuthError(f"{operation}: authorization expired; sign in again.", oauth_error="invalid_grant")
            raise ChatGPTAuthError(f"{operation} failed (HTTP {response.status_code}); sign in again if needed.")
        try:
            data = response.json()
        except ValueError:
            raise ChatGPTAuthError(f"{operation} returned invalid JSON.") from None
        if not isinstance(data, dict):
            raise ChatGPTAuthError(f"{operation} returned an invalid object.")
        return data

    def _request_json(self, method: str, url: str, operation: str, **kwargs) -> dict:
        try:
            response = getattr(self.session, method)(url, timeout=30, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise ChatGPTAuthError(f"{operation} could not reach OpenAI.") from None
        try:
            return self._json_response(response, operation)
        finally:
            response.close()

    def configuration(self) -> dict:
        if self._configuration is None:
            data = self._request_json("get", DISCOVERY_URL, "OpenAI discovery")
            if data.get("issuer") != ISSUER:
                raise ChatGPTAuthError("OpenAI discovery issuer did not match.")
            for key in ("jwks_uri", "revocation_endpoint"):
                endpoint = data.get(key)
                parsed = urlparse(endpoint or "")
                if parsed.scheme != "https" or parsed.netloc != "auth.openai.com":
                    raise ChatGPTAuthError(f"OpenAI discovery {key} was invalid.")
            self._configuration = data
        return self._configuration

    def validate_id_token(self, token: str, client_id: str, *, nonce: str | None = None, subject: str | None = None) -> dict:
        """Verify signature before trusting identity, email, nonce, or audience."""
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not header.get("kid"):
                raise ValueError("unsupported signing key")
            jwks = self._request_json("get", self.configuration()["jwks_uri"], "OpenAI signing keys")
            keys = [item for item in jwks.get("keys", []) if item.get("kid") == header["kid"] and item.get("kty") == "RSA"]
            if len(keys) != 1:
                raise ValueError("unknown signing key")
            key = jwt.PyJWK.from_dict(keys[0], algorithm="RS256").key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                                options={"require": ["exp", "iss", "aud", "sub", "iat"]})
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError("missing subject")
            if nonce is not None and not _matches(claims.get("nonce", ""), nonce):
                raise ValueError("nonce mismatch")
            if subject is not None and claims["sub"] != subject:
                raise ValueError("account mismatch")
            return claims
        except ChatGPTAuthError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise ChatGPTAuthError("ChatGPT ID token signature or identity validation failed.") from None

    def prepare_login(self, redirect_uri: str, *, new_account: bool = False, registration_client_id: str | None = None) -> dict:
        profile = None
        if registration_client_id and (not _ISSUED_CLIENT_ID.fullmatch(registration_client_id) or registration_client_id == DYNAMIC_CLIENT_ID):
            raise ChatGPTAuthError("Pending ChatGPT registration client ID was invalid.")
        if not new_account and not registration_client_id:
            try:
                profile = self.store.read_profile(self.account)
            except ChatGPTAuthError as exc:
                # An absent active account starts registration; a selected bad account is an error.
                if self.account or os.environ.get("CHATGPT_ACCOUNT") or "No ChatGPT account selected" not in str(exc):
                    raise
        verifier = secrets.token_urlsafe(48)
        attempt = {
            "state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
            "verifier": verifier, "redirect_uri": redirect_uri,
            "client_id": registration_client_id or (profile["client_id"] if profile else DYNAMIC_CLIENT_ID),
            "subject": profile["subject"] if profile else None,
            "host_id": self.store.host_id(),
        }
        parameters = {
            "client_id": attempt["client_id"], "ext_agent_host_id": attempt["host_id"],
            "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
            "resource": RESOURCE, "state": attempt["state"], "nonce": attempt["nonce"],
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
        }
        if profile:
            if profile.get("id_token"):
                parameters["id_token_hint"] = profile["id_token"]
            if profile.get("email"):
                parameters["login_hint"] = profile["email"]
        elif attempt["client_id"] == DYNAMIC_CLIENT_ID:
            parameters["agent_name_hint"] = APP_NAME
        attempt["authorization_url"] = AUTHORIZE_URL + "?" + urlencode(parameters)
        return attempt

    def complete_login(self, attempt: dict, callback: dict) -> str:
        if not _matches(callback.get("state", ""), attempt["state"]):
            raise ChatGPTAuthError("ChatGPT callback state did not match.")
        if callback.get("error"):
            raise ChatGPTAuthError("ChatGPT sign-in was denied or cancelled.")
        returned_id = callback.get("client_id")
        if attempt["client_id"] == DYNAMIC_CLIENT_ID:
            if not isinstance(returned_id, str) or not _ISSUED_CLIENT_ID.fullmatch(returned_id) or returned_id == DYNAMIC_CLIENT_ID:
                raise ChatGPTAuthError("ChatGPT registration did not issue a client ID.")
            client_id = returned_id
        else:
            client_id = attempt["client_id"]
            if returned_id and returned_id != client_id:
                raise ChatGPTAuthError("ChatGPT callback changed the selected client ID.")
        if not callback.get("code"):
            raise ChatGPTAuthError("ChatGPT callback did not include an authorization code.")
        # Keep a newly issued registration only in this attempt until identity is
        # verified. An expired one-time code can then restart without registering
        # another client or writing unverified credentials.
        attempt["issued_client_id"] = client_id
        tokens = self._request_json("post", TOKEN_URL, "ChatGPT code exchange", data={
            "grant_type": "authorization_code", "client_id": client_id, "code": callback["code"],
            "code_verifier": attempt["verifier"], "redirect_uri": attempt["redirect_uri"], "resource": RESOURCE,
        })
        claims = self.validate_id_token(tokens.get("id_token", ""), client_id,
                                        nonce=attempt["nonce"], subject=attempt["subject"])
        profile = {
            "issuer": ISSUER, "subject": claims["sub"], "email": claims.get("email", ""),
            "client_id": client_id, "ext_agent_host_id": attempt["host_id"],
        }
        profile = self._token_record(profile, tokens)
        with self.store.locked():
            account = self.store.save_profile(profile, activate=True)
            self.account = account
            return account

    @staticmethod
    def _token_record(profile: dict, tokens: dict) -> dict:
        scopes = str(tokens.get("scope", " ".join(profile.get("scopes", [])))).split()
        if "chatgpt.tokens.use.direct" not in scopes:
            raise ChatGPTAuthError("ChatGPT plan usage permission was not granted.")
        if str(tokens.get("token_type", "Bearer")).lower() != "bearer":
            raise ChatGPTAuthError("ChatGPT returned an unsupported token type.")
        merged = dict(profile)
        for key in _TOKEN_KEYS:
            if tokens.get(key):
                if not isinstance(tokens[key], str):
                    raise ChatGPTAuthError("ChatGPT returned an invalid credential format.")
                merged[key] = tokens[key]
        if not merged.get("access_token") or not merged.get("refresh_token"):
            raise ChatGPTAuthError("ChatGPT returned incomplete renewable credentials.")
        try:
            expires_in = float(tokens["expires_in"])
            if expires_in <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ChatGPTAuthError("ChatGPT access-token expiry was invalid.") from None
        merged.update(scopes=scopes, token_type="Bearer", expires_at=time.time() + expires_in)
        return merged

    def access_token(self) -> str:
        # Read inside the refresh lock so a second process uses the newly rotated token.
        with self.store.locked():
            account = self.store.selected_account(self.account)
            profile = self.store.read_profile(account)
            # A live client keeps its selected account and cached model catalog.
            # Switching the active account takes effect on the next client/run.
            self.account = account
            if not profile.get("access_token") or not profile.get("refresh_token"):
                raise ChatGPTAuthError("ChatGPT account is signed out; run python chatgpt_auth.py login.")
            if "chatgpt.tokens.use.direct" not in profile.get("scopes", []):
                raise ChatGPTAuthError("ChatGPT plan usage permission was not granted.")
            if float(profile.get("expires_at", 0)) > time.time() + 60:
                return profile["access_token"]
            tokens = self._request_json("post", TOKEN_URL, "ChatGPT session refresh", data={
                "grant_type": "refresh_token", "client_id": profile["client_id"],
                "refresh_token": profile["refresh_token"], "resource": RESOURCE,
            })
            if tokens.get("id_token"):
                self.validate_id_token(tokens["id_token"], profile["client_id"], subject=profile["subject"])
            renewed = self._token_record(profile, tokens)
            self.store.save_profile(renewed)
            return renewed["access_token"]

    def logout(self) -> bool:
        """Clear local tokens, preserving registration and host; report revocation."""
        with self.store.locked():
            profile = self.store.read_profile(self.account)
            confirmed = not bool(profile.get("refresh_token"))
            if profile.get("refresh_token"):
                for attempt in range(2):
                    retry = False
                    try:
                        response = self.session.post(self.configuration()["revocation_endpoint"], data={
                            "token": profile["refresh_token"], "token_type_hint": "refresh_token", "client_id": profile["client_id"],
                        }, timeout=15, allow_redirects=False)
                        confirmed = response.status_code == 200
                        retry = response.status_code >= 500
                        response.close()
                    except (ChatGPTAuthError, requests.RequestException):
                        confirmed = False
                        retry = True
                    if confirmed or not retry or attempt == 1:
                        break
                    time.sleep(0.25)
            for key in (*_TOKEN_KEYS, "expires_at", "scopes", "token_type"):
                profile.pop(key, None)
            self.store.save_profile(profile)
            return confirmed

    def login(self, *, new_account: bool = False, port: int = 0, timeout: float = 300) -> str:
        result = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # HTTP request paths contain OAuth codes; never log them.

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != "/auth/callback":
                    self.send_error(404)
                    return
                values = parse_qs(parsed.query)
                if any(len(value) != 1 for value in values.values()):
                    self.send_error(400)
                    return
                callback = {key: value[0] for key, value in values.items()}
                if not _matches(callback.get("state", ""), attempt["state"]):
                    self.send_error(400, "Invalid authorization state")
                    return
                result.update(callback)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Authorization returned. You can close this tab; check your terminal for validation.")

        server = HTTPServer(("127.0.0.1", port), CallbackHandler)
        try:
            attempt = self.prepare_login(f"http://127.0.0.1:{server.server_port}/auth/callback", new_account=new_account)
            deadline = time.monotonic() + timeout
            for exchange_attempt in range(2):
                result.clear()
                print("Continue with ChatGPT: opening your browser. Credentials are stored outside the repository.")
                if not webbrowser.open(attempt["authorization_url"]):
                    # Do not print an authorization URL that can contain an id_token_hint.
                    raise ChatGPTAuthError("Browser could not open. Run login on a desktop with a system browser.")
                while not result and time.monotonic() < deadline:
                    server.timeout = min(1, max(0.01, deadline - time.monotonic()))
                    server.handle_request()
                if not result:
                    raise ChatGPTAuthError("ChatGPT sign-in timed out.")
                try:
                    return self.complete_login(attempt, result)
                except ChatGPTAuthError as exc:
                    if exc.oauth_error != "invalid_grant" or exchange_attempt == 1:
                        raise
                    if attempt["subject"] is not None:
                        attempt = self.prepare_login(attempt["redirect_uri"])
                    else:
                        attempt = self.prepare_login(attempt["redirect_uri"], registration_client_id=attempt["issued_client_id"])
                    print("The authorization code expired. Starting a fresh authorization for the same registration.")
            raise ChatGPTAuthError("ChatGPT sign-in did not complete.")
        finally:
            server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Mer Portfolio: official Continue with ChatGPT")
    parser.add_argument("--auth-dir", help="Protected credential directory outside git")
    parser.add_argument("--account", help="Saved account label from status")
    sub = parser.add_subparsers(dest="command", required=True)
    login_parser = sub.add_parser("login")
    login_parser.add_argument("--new", action="store_true", help="Register an additional account or workspace")
    login_parser.add_argument("--port", type=int, default=0)
    sub.add_parser("status")
    sub.add_parser("models")
    sub.add_parser("logout")
    sub.add_parser("use")
    args = parser.parse_args()
    try:
        store = CredentialStore(args.auth_dir)
        auth = ChatGPTAuth(store, account=args.account)
        if args.command == "login":
            account = auth.login(new_account=args.new, port=args.port)
            print(f"ChatGPT connected. Account label: {account}")
        elif args.command == "status":
            print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        elif args.command == "models":
            from chatgpt_client import ChatGPTClient, ChatGPTClientError
            try:
                print(json.dumps(ChatGPTClient(auth=auth).list_models(), ensure_ascii=False, indent=2))
            except ChatGPTClientError as exc:
                print(f"ChatGPT connection: {exc}")
                return 1
        elif args.command == "use":
            if not args.account:
                raise ChatGPTAuthError("Select an account with --account ACCOUNT use.")
            store.select(args.account)
            print("Selected ChatGPT account updated.")
        elif args.command == "logout":
            confirmed = auth.logout()
            print("Signed out locally. " + ("Remote session revoked." if confirmed else "Remote revocation was not confirmed; disconnect this app in ChatGPT Settings."))
        return 0
    except ChatGPTAuthError as exc:
        print(f"ChatGPT connection: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
