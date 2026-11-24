"""Jira Cloud configuration and HTTP transport."""

import base64
import http.client
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "/rest/api/3"
AGILE = "/rest/agile/1.0"
SOFTWARE = "/rest/software/1.0"
NO_BODY = object()
REST_PATH = re.compile(r"/rest/(?:api/3|agile/1\.0|software/1\.0|servicedeskapi)(?:/[\w.~:-]+)+", re.ASCII)


class JiraError(Exception):
    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class InputError(Exception):
    pass


def parse_json(text):
    def object_from_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("JSON keys must be unique")
        return result

    def reject_constant(_):
        raise ValueError("JSON numbers must be finite")

    return json.loads(text, object_pairs_hook=object_from_pairs, parse_constant=reject_constant)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def get_credentials():
    state = os.environ.get("KIAO_WORKER_STATE_PATH")
    root = Path(os.environ.get("KIAO_WORKER_ROOT_PATH") or "/tmp")
    path = (Path(state) if state else root / ".kiao-config") / "atlassian.json"
    if not path.is_absolute():
        raise InputError("Kiao worker state path must be absolute")
    try:
        config = parse_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"Cannot read {path} ({exc.strerror})") from None
    except ValueError:
        raise InputError(f"Invalid JSON in {path}") from None

    jira = config.get("jira") if isinstance(config, dict) else None
    if not isinstance(jira, dict):
        raise InputError(f"Set the jira object in {path}")

    def required(name):
        value = jira.get(name)
        if not isinstance(value, str) or not value.strip():
            raise InputError(f"Set jira.{name} in {path}")
        return value.strip()

    url, email, token = (required(name) for name in ("url", "email", "token"))
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.port != 0
                 and not parsed.username and "@" not in parsed.netloc and "\\" not in parsed.netloc
                 and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                 and not any(ch.isspace() or ord(ch) < 32 for ch in url))
    except ValueError:
        valid = False
    if not valid:
        raise InputError(f"jira.url in {path} must be an HTTPS site origin")
    if ":" in email or any(ch.isspace() for ch in email + token):
        raise InputError(f"Invalid jira.email or jira.token in {path}")

    base = f"https://{parsed.netloc}"
    if "cloudId" in jira:
        try:
            cloud_id = uuid.UUID(jira["cloudId"])
        except (ValueError, TypeError, AttributeError):
            raise InputError(f"jira.cloudId in {path} must be a UUID") from None
        base = f"https://api.atlassian.com/ex/jira/{cloud_id}"
    return base, email, token


def validate_path(path):
    if not REST_PATH.fullmatch(path) or any(part in (".", "..") for part in path.split("/")):
        raise InputError("Use a Jira REST path without a URL, query string or encoded segments")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JiraClient:
    def __init__(self, *, raw=False):
        self.raw = raw
        self.opener = build_opener(NoRedirect())
        self.secrets = ()
        self.base = None

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return text

    def open(self, method, path, *, query=None, data=None, headers=None):
        validate_path(path)
        if self.base is None:
            self.base, email, token = get_credentials()
            self.authorization = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
            self.secrets = (self.authorization, token)
        url = self.base + path
        if query:
            url += "?" + urlencode(query, doseq=True)
        request = Request(url, data=data, method=method, headers={
            "Accept": "application/json",
            "Authorization": f"Basic {self.authorization}",
            **(headers or {}),
        })
        try:
            return self.opener.open(request, timeout=30)
        except HTTPError as exc:
            with exc:
                raise self.http_error(exc) from None
        except (URLError, OSError, http.client.HTTPException) as exc:
            raise JiraError(f"Jira request failed ({exc})") from None

    def http_error(self, exc):
        status = exc.code
        message = f"Jira returned HTTP {status}"
        if 300 <= status < 400:
            return JiraError(message + " (redirect refused)", status=status)
        try:
            data = json.loads(exc.read(16_384))
        except (ValueError, OSError, http.client.HTTPException):
            data = None
        details = []
        if isinstance(data, dict):
            errors = data.get("errorMessages")
            if isinstance(errors, list):
                details.extend(error for error in errors if isinstance(error, str))
            fields = data.get("errors")
            if isinstance(fields, dict):
                details.extend(f"{key} ({value})" for key, value in fields.items() if isinstance(value, str))
            if not details and isinstance(data.get("message"), str):
                details.append(data["message"])
        if details:
            message += ". " + " | ".join(self.redact(detail)[:250] for detail in details[:5])
        retry_after = exc.headers.get("Retry-After") if status in (429, 503) else None
        return JiraError(message, status=status, retry_after=retry_after)

    def request(self, method, path, *, query=None, payload=NO_BODY, view=None):
        data, headers = None, None
        if payload is not NO_BODY:
            try:
                data = json_text(payload).encode("utf-8")
            except (ValueError, UnicodeError):
                raise InputError("Payload must be valid UTF-8 JSON with finite numbers") from None
            headers = {"Content-Type": "application/json"}
        with self.open(method, path, query=query, data=data, headers=headers) as response:
            return self.read_response(response, view)

    def read_response(self, response, view):
        try:
            body = response.read()
            data = parse_json(body) if body else None
        except (OSError, http.client.HTTPException):
            raise JiraError("Jira response ended before it could be read") from None
        except ValueError:
            raise JiraError("Jira did not return JSON") from None
        if not body:
            if view:
                raise JiraError("Jira returned an empty response")
            return {"ok": True}
        return view(data) if view and not self.raw else data

    def upload(self, issue, path, view):
        if not path.is_file():
            raise InputError("Upload requires a regular file")
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise InputError("Upload requires a regular file")
            boundary = uuid.uuid4().hex
            filename = path.name.replace("%", "%25").replace('"', "%22").replace("\\", "%5C")
            filename = filename.replace("\r", "%0D").replace("\n", "%0A")
            prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                      "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
            suffix = f"\r\n--{boundary}--\r\n".encode("ascii")

            def multipart():
                yield prefix
                remaining = info.st_size
                while remaining:
                    chunk = source.read(min(remaining, 65_536))
                    if not chunk:
                        raise JiraError("File changed size during upload")
                    yield chunk
                    remaining -= len(chunk)
                yield suffix

            headers = {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(prefix) + info.st_size + len(suffix)),
                "X-Atlassian-Token": "no-check",
            }
            with self.open("POST", f"{API}/issue/{issue}/attachments", data=multipart(), headers=headers) as response:
                return self.read_response(response, view)

    def download(self, attachment, destination):
        if os.path.lexists(destination):
            raise InputError("Attachment destination already exists")
        path = f"{API}/attachment/content/{attachment}"
        with self.open("GET", path, query={"redirect": "false"}, headers={"Accept": "*/*"}) as response:
            with tempfile.NamedTemporaryFile(dir=destination.parent) as output:
                try:
                    shutil.copyfileobj(response, output)
                except http.client.HTTPException:
                    raise JiraError("Jira attachment download ended before it was complete") from None
                size = output.tell()
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or size != int(length)):
                    raise JiraError("Jira attachment download ended before it was complete")
                output.flush()
                os.link(output.name, destination)
        return {"id": attachment, "file": str(destination), "size": size}
