"""GitHub只读探针底座：固定主机/路由、固定实际连接、独立凭据和共享预算。

不使用系统代理、浏览器登录、Git凭据或环境中的隐式令牌。
网络错误只提供固定错误码，原始服务正文及异常不进入审计。
"""

import hashlib
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
import unicodedata
import zlib
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from repoagent.snapshots.repository import safe_relative

HOST = "api.github.com"
OID = re.compile(r"[0-9a-f]{40}\Z")
NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


class GitHubError(ValueError):
    def __init__(self, code, *, stage=None, status=None):
        self.code = code
        self.stage = stage
        self.status = status
        super().__init__(code)


def repository_name(url):
    """只接受仓库首页；URL是标识输入，不直接交给网络层。"""
    if (
        not isinstance(url, str)
        or len(url) > 512
        or any(ord(char) < 33 or ord(char) == 127 for char in url)
    ):
        raise GitHubError("invalid_repository_url")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or (parsed.query or parsed.fragment or "?" in url or "#" in url)
    ):
        raise GitHubError("invalid_repository_url")
    path = parsed.path.removesuffix("/").removesuffix(".git")
    parts = path.split("/")
    if (
        len(parts) != 3
        or parts[0]
        or any(not NAME.fullmatch(part) or part in {".", ".."} for part in parts[1:])
    ):
        raise GitHubError("invalid_repository_url")
    return "/".join(parts[1:])


def validate_route(route):
    parts = route.split("/")
    if (
        len(parts) < 4
        or parts[:2] != ["", "repos"]
        or any(not NAME.fullmatch(part) or part in {".", ".."} for part in parts[2:4])
    ):
        raise GitHubError("route_rejected")
    if len(parts) == 4:
        return "metadata"
    if len(parts) == 7 and parts[4:6] in (["git", "trees"], ["git", "blobs"]):
        suffix = parts[6]
        if parts[5] == "trees":
            suffix = suffix.removesuffix("?recursive=1")
        if OID.fullmatch(suffix):
            return parts[5]
    if len(parts) == 5 and parts[4].startswith("commits?sha="):
        query = parts[4].removeprefix("commits?sha=")
        encoded = query.removesuffix("&per_page=1")
        ref = unquote(encoded)
        if (query.endswith("&per_page=1") and ref and len(ref) <= 200
            and quote(ref, safe="") == encoded
            and not any(ord(c) < 33 or ord(c) == 127 for c in ref)):
            return "commit"
    raise GitHubError("route_rejected")


def public_address(address):
    ip = ipaddress.ip_address(address)
    # IPv4映射和IPv6过渡地址不能绕过IPv4本地/私网保护。
    return ip.is_global and not (
        ip.is_multicast
        or ip.is_unspecified
        or getattr(ip, "ipv4_mapped", None)
        or getattr(ip, "sixtofour", None)
        or getattr(ip, "teredo", None)
    )


def resolve_public(timeout):
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM))
        except OSError:
            result.put(None)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = result.get(timeout=timeout)
    except queue.Empty:
        raise GitHubError("dns_timeout") from None
    if not addresses or any(not public_address(a[4][0]) for a in addresses):
        raise GitHubError("dns_address_rejected")
    # 只选择一次连接，不自动尝试其他地址、代理或镜像。
    return sorted(addresses, key=lambda a: a[0] != socket.AF_INET)[0]


@dataclass
class NetworkBudget:
    max_requests: int = 20
    max_bytes: int = 16 * 1024 * 1024
    max_seconds: float = 45
    cancelled: object = field(default=lambda: False, repr=False)
    remaining: object = field(default=lambda: float("inf"), repr=False)
    requests: int = 0
    bytes: int = 0
    seconds: float = 0
    events: list = field(default_factory=list)

    def check(self):
        if self.cancelled():
            raise GitHubError("cancelled")
        if self.seconds >= self.max_seconds:
            raise GitHubError("network_time_limit")
        if self.remaining() <= 0:
            raise GitHubError("network_time_limit")
        if self.bytes >= self.max_bytes:
            raise GitHubError("network_byte_limit")


class DirectTransport:
    """实际socket只连已核对IP，TLS仍校验GitHub主机名；不自动重定向。"""

    def request(self, route, headers, limit, seconds, budget):
        deadline = time.monotonic() + seconds
        connection = raw = secured = None
        stage, status = "dns", None

        def remaining(cap):
            budget.check()
            left = min(deadline - time.monotonic(), budget.remaining())
            if left <= 0:
                raise TimeoutError()
            return min(cap, left)

        try:
            family, kind, proto, _, address = resolve_public(remaining(3))
            stage = "connect"
            raw = socket.socket(family, kind, proto)
            raw.settimeout(remaining(3))
            raw.connect(address)
            if raw.getpeername()[0] != address[0] or not public_address(raw.getpeername()[0]):
                raise GitHubError("peer_address_rejected")
            stage = "tls"
            raw.settimeout(remaining(8))
            secured = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
            stage = "headers"
            secured.settimeout(remaining(10))
            connection = http.client.HTTPSConnection(HOST, timeout=10)
            connection.sock = secured
            connection.request("GET", route, headers=headers)
            secured.settimeout(remaining(10))
            response = connection.getresponse()
            status = response.status
            info = {key.lower(): value for key, value in response.getheaders()}
            encoding = info.get("content-encoding", "identity")
            if encoding != "identity" and not (encoding == "gzip" and headers.get("Accept-Encoding") == "gzip"):
                raise GitHubError("unexpected_content_encoding")
            if "content-length" in info:
                try:
                    length = int(info["content-length"])
                except ValueError:
                    raise GitHubError("invalid_content_length") from None
                if not 0 <= length <= limit:
                    raise GitHubError("response_too_large")
            stage = "body"
            chunks, size = [], 0
            while True:
                secured.settimeout(remaining(10))
                chunk = response.read1(min(65536, limit + 1 - size))
                size += len(chunk)
                budget.bytes += len(chunk)
                budget.check()
                if size > limit:
                    raise GitHubError("response_too_large")
                if not chunk:
                    break
                chunks.append(chunk)
            if "content-length" in info and size != length:
                raise GitHubError("github_incomplete_response")
            data = b"".join(chunks)
            if encoding == "gzip":
                try:
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    decoded = decoder.decompress(data, limit + 1)
                except zlib.error:
                    raise GitHubError("invalid_compressed_response") from None
                if len(decoded) > limit:
                    raise GitHubError("response_too_large")
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise GitHubError("invalid_compressed_response")
                # Charge the larger of wire and expanded sizes; compression must
                # not bypass the shared byte budget or decoded response cap.
                budget.bytes += max(0, len(decoded) - size)
                remaining(10)
                data = decoded
            info["_wire_bytes"] = size
            return status, info, data
        except GitHubError as error:
            error.stage = error.stage or stage
            error.status = error.status or status
            raise
        except ssl.SSLCertVerificationError:
            raise GitHubError("github_certificate_failed", stage=stage, status=status) from None
        except ssl.SSLError:
            raise GitHubError("github_tls_failed", stage=stage, status=status) from None
        except TimeoutError:
            code = {"dns": "dns_timeout", "connect": "github_connect_timeout",
                    "tls": "github_tls_timeout", "headers": "github_headers_timeout",
                    "body": "github_read_timeout"}[stage]
            raise GitHubError(code, stage=stage, status=status) from None
        except http.client.IncompleteRead:
            raise GitHubError("github_incomplete_response", stage=stage, status=status) from None
        except (OSError, http.client.HTTPException, ValueError):
            raise GitHubError("github_connection_failed", stage=stage, status=status) from None
        finally:
            if connection:
                connection.close()
            if secured:
                secured.close()
            if raw:
                raw.close()


class GitHubClient:
    def __init__(self, *, token=None, transport=None):
        if token is not None and (
            not isinstance(token, str)
            or not token
            or any(not 33 <= ord(char) <= 126 for char in token)
        ):
            raise GitHubError("invalid_selected_credential")
        self._token = token
        self._transport = transport or DirectTransport()
        self.authentication_status = "configured_unverified" if token else "anonymous"

    def __repr__(self):
        return f"GitHubClient(mode={'configured' if self._token else 'anonymous'})"

    def get(self, route, budget, *, raw=False, public=False):
        kind = validate_route(route)
        if raw and kind != "blobs":
            raise GitHubError("raw_route_rejected")
        budget.check()
        if budget.requests >= budget.max_requests:
            raise GitHubError("network_request_limit")
        headers = {
            "Accept": "application/vnd.github.raw+json"
            if raw
            else "application/vnd.github+json",
            "Accept-Encoding": "gzip" if kind == "trees" else "identity",
            "User-Agent": "RepoAgent-N3-Probe",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        authenticated = bool(self._token and not public)
        if authenticated and self.authentication_status == "invalid":
            raise GitHubError("credential_rejected")
        if authenticated:
            headers["Authorization"] = "Bearer " + self._token
        cap = 4 * 1024 * 1024 if kind == "trees" else (256 * 1024 if kind == "commit" else 1024 * 1024)
        cap = min(cap, budget.max_bytes - budget.bytes)
        event = {
            "kind": kind,
            "mode": "authenticated" if authenticated else "anonymous",
            "status": None,
            "bytes": 0,
            "error": None,
        }
        start, before = time.monotonic(), budget.bytes
        budget.requests += 1
        try:
            status, info, data = self._transport.request(
                route,
                headers,
                cap,
                min(budget.max_seconds - budget.seconds, budget.remaining()),
                budget,
            )
            event["status"] = status
            if isinstance(info.get("_wire_bytes"), int):
                event["wire_bytes"] = info["_wire_bytes"]
            # 测试传输也必须遵守实际传输的上限与取消契约。
            if len(data) > cap:
                raise GitHubError("response_too_large")
            for key in ("x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"):
                if re.fullmatch(r"[0-9]{1,16}", info.get(key, "")):
                    event[key] = info[key]
            if 300 <= status < 400:
                # A包先采取更严格的零跳转策略；不自动转发认证头。
                raise GitHubError("redirect_requires_review")
            if status == 401:
                if authenticated:
                    self.authentication_status = "invalid"
                raise GitHubError(
                    "credential_rejected"
                    if authenticated
                    else "authentication_required"
                )
            if status in (403, 429):
                limited = (
                    status == 429
                    or info.get("x-ratelimit-remaining") == "0"
                    or "retry-after" in info
                )
                if authenticated:
                    self.authentication_status = "limited" if limited else "denied"
                raise GitHubError("rate_limited" if limited else "access_denied")
            if status == 404:
                raise GitHubError("resource_not_visible")
            if status != 200:
                raise GitHubError("github_response_failed")
            if authenticated:
                self.authentication_status = "valid"
            budget.check()
            if budget.seconds + time.monotonic() - start >= budget.max_seconds:
                raise GitHubError("network_time_limit")
            if raw:
                return data
            try:
                result = json.loads(data)
            except (ValueError, UnicodeError):
                raise GitHubError("invalid_github_json") from None
            if kind == "commit":
                if not isinstance(result, list) or len(result) != 1:
                    raise GitHubError("commit_identity_failed")
                result = result[0]
            if not isinstance(result, dict):
                raise GitHubError("invalid_github_json")
            return result
        except GitHubError as error:
            event["error"] = error.code
            if error.stage in {"dns", "connect", "tls", "headers", "body"}:
                event["stage"] = error.stage
            if isinstance(error.status, int) and 100 <= error.status <= 599:
                event["status"] = error.status
            raise
        finally:
            elapsed = time.monotonic() - start
            budget.seconds += elapsed
            event.update(bytes=budget.bytes - before, seconds=round(elapsed, 4))
            budget.events.append(event)

    def public_repository(self, name, budget, expected_id=None):
        data = self.get("/repos/" + name, budget, public=True)
        canonical = data.get("full_name")
        if data.get("private") is not False or data.get("visibility") != "public":
            raise GitHubError("public_visibility_not_confirmed")
        if (
            type(data.get("id")) is not int
            or data["id"] <= 0
            or (expected_id is not None and data["id"] != expected_id)
        ):
            raise GitHubError("repository_identity_mismatch")
        if (
            not isinstance(canonical, str)
            or repository_name("https://github.com/" + canonical).lower()
            != name.lower()
        ):
            raise GitHubError("repository_identity_mismatch")
        return {
            "id": data["id"],
            "full_name": canonical,
            "default_branch": data.get("default_branch"),
        }


def verify_catalog(data, tree):
    """拒绝截断/不安全/冲突路径；元数据不包含正文，不沿链接读取。"""
    if (
        not isinstance(data, dict)
        or data.get("sha") != tree
        or data.get("truncated") is not False
    ):
        raise GitHubError("tree_identity_or_completeness_failed")
    entries = data.get("tree")
    if not isinstance(entries, list) or len(entries) > 10000:
        raise GitHubError("tree_entry_limit")
    seen, clean = set(), []
    for item in entries:
        try:
            if not isinstance(item, dict):
                raise ValueError()
            path = safe_relative(item["path"])
            folded = unicodedata.normalize("NFC", path).casefold()
            if folded in seen or any(ord(c) == 127 for c in path):
                raise ValueError()
            seen.add(folded)
            mode, kind, oid = item["mode"], item["type"], item["sha"]
            if (mode, kind) not in {
                ("040000", "tree"),
                ("100644", "blob"),
                ("100755", "blob"),
                ("120000", "blob"),
                ("160000", "commit"),
            } or not OID.fullmatch(oid):
                raise ValueError()
            size = item.get("size", 0)
            if type(size) is not int or size < 0:
                raise ValueError()
            clean.append(
                {"path": path, "mode": mode, "type": kind, "sha": oid, "size": size}
            )
        except (KeyError, TypeError, ValueError):
            raise GitHubError("invalid_tree_entry") from None
    by_path = {entry["path"]: entry for entry in clean}
    for entry in clean:
        parents = entry["path"].split("/")[:-1]
        for i in range(1, len(parents) + 1):
            parent = by_path.get("/".join(parents[:i]))
            if not parent or parent["mode"] != "040000":
                raise GitHubError("invalid_tree_hierarchy")
    return sorted(clean, key=lambda e: e["path"])


def verify_blob(data, entry):
    if entry["mode"] not in {"100644", "100755"} or entry["type"] != "blob":
        raise GitHubError("non_regular_blob")
    oid = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
    if len(data) != entry["size"] or oid != entry["sha"]:
        raise GitHubError("blob_integrity_failed")
    if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise GitHubError("lfs_object_not_supported")
    return hashlib.sha256(data).hexdigest()
