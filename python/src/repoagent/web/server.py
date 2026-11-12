"""标准库本地HTTP薄层；固定loopback、同源校验、会话令牌及静态资源白名单。"""

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from repoagent.repositories.github import GitHubError
from repoagent.web.service import ServiceError


def create_server(service, port=8765):
    token = secrets.token_hex(32)
    # 接口和资源按启动版本固定。修改磁盘文件不影响已运行实例。
    static = files("repoagent.web").joinpath("static")
    resources = {
        "/app.js": ("app-session.js", "text/javascript; charset=utf-8"),
        "/style.css": ("style-session.css", "text/css; charset=utf-8"),
        "/marked.umd.js": ("marked.umd.js", "text/javascript; charset=utf-8"),
        "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
        "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
        "/stream.js": ("stream.js", "text/javascript; charset=utf-8"),
        "/inspector.js": ("inspector.js", "text/javascript; charset=utf-8"),
        "/library.js": ("library.js", "text/javascript; charset=utf-8"),
    }
    cached = {
        route: (static.joinpath(name).read_bytes(), mime)
        for route, (name, mime) in resources.items()
    }
    homepage = (
        static.joinpath("index-session.html")
        .read_bytes()
        .replace(b"__SESSION_TOKEN__", token.encode())
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # URL/请求内容不写终端访问日志。

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def reply(self, status, data, content_type="application/json; charset=utf-8"):
            # 所有提前拒绝分支都消费有界短请求体，避免Windows关闭连接时RST吞掉响应。
            if self.command == "POST" and not getattr(self, "body_consumed", False):
                self.discard_body()
            raw = (
                json.dumps(data, ensure_ascii=False).encode()
                if isinstance(data, (dict, list))
                else data
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(raw)

        def discard_body(self):
            # 有界丢弃已声明的短请求体，避免Windows关闭未消费请求时RST吞掉403。
            # 不解析内容；未知长度/分块/过大请求不读取，仍直接关闭。
            if not self.headers.get("Transfer-Encoding"):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if 0 < size <= 96 * 1024:
                        self.rfile.read(size)
                except (ValueError, OSError, TimeoutError):
                    pass
            self.body_consumed = True
            self.close_connection = True

        def reject_access(self, code, post):
            if post:
                self.discard_body()
            return self.reply(403, {"error": code})

        def handle_request(self, post=False):
            self.body_consumed = False
            host = f"127.0.0.1:{self.server.server_port}"
            if (
                self.headers.get("Host") != host
                or self.headers.get("Origin", f"http://{host}") != f"http://{host}"
            ):
                return self.reject_access("origin_or_host_rejected", post)
            try:
                url = urlsplit(self.path)
                if url.path.startswith("/api/"):
                    supplied = self.headers.get("X-RepoAgent-Token", "")
                    if not supplied.isascii() or not secrets.compare_digest(
                        supplied, token
                    ):
                        return self.reject_access("session_token_required", post)
                if post:
                    if (
                        self.headers.get("Content-Type", "").split(";")[0]
                        != "application/json"
                    ):
                        raise ServiceError("json_required", 415)
                    if self.headers.get("Transfer-Encoding"):
                        raise ServiceError("transfer_encoding_not_supported")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 96 * 1024:
                        raise ServiceError("request_too_large_or_empty", 413)
                    body = self.rfile.read(size)
                    self.body_consumed = True
                    payload = json.loads(body)
                    if url.path == "/api/tasks":
                        return self.reply(202, service.submit(payload))
                    if url.path == "/api/runtime-settings":
                        return self.reply(200, service.runtime_settings(payload))
                    if url.path == "/api/model-source":
                        return self.reply(200, service.set_model_source(payload))
                    if url.path == "/api/sessions":
                        return self.reply(201, service.create_session(payload))
                    if url.path == "/api/organization":
                        return self.reply(200, service.manage_organization(payload))
                    if url.path == "/api/repositories":
                        return self.reply(
                            202, service.submit_repository(payload)
                        )
                    parts = url.path.strip("/").split("/")
                    if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "resume":
                        if payload != {}:
                            raise ServiceError("resume_requires_empty_object")
                        return self.reply(202, service.resume(parts[2]))
                    if (
                        len(parts) == 5
                        and parts[:3] == ["api", "repositories", "jobs"]
                        and parts[4] == "cancel"
                    ):
                        if payload != {}:
                            raise ServiceError("cancel_requires_empty_object")
                        return self.reply(
                            200, service.repository_manager.cancel(parts[3])
                        )
                    if (
                        len(parts) == 4
                        and parts[:2] == ["api", "tasks"]
                        and parts[3] == "cancel"
                    ):
                        if payload != {}:
                            raise ServiceError("cancel_requires_empty_object")
                        return self.reply(200, service.cancel(parts[2]))
                else:
                    parts = url.path.strip("/").split("/")
                    if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "events":
                        job = service.get(parts[2])
                        q = parse_qs(url.query, strict_parsing=True)
                        if set(q) - {"after"} or any(len(v) != 1 for v in q.values()):
                            raise ServiceError("invalid_stream_cursor")
                        after = int(q.get("after", ["0"])[0])
                        if after < 0:
                            raise ServiceError("invalid_stream_cursor")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        if job["state"] != "running":
                            self.wfile.write((json.dumps(service.streams.snapshot(parts[2]) | {"terminal": True}) + "\n").encode())
                            return
                        try:
                            for event in service.streams.subscribe(parts[2], after):
                                self.wfile.write((json.dumps(event, ensure_ascii=True) + "\n").encode())
                                self.wfile.flush()
                        except (OSError, TimeoutError):
                            pass
                        return
                    if url.path == "/":
                        return self.reply(200, homepage, "text/html; charset=utf-8")
                    if url.path in cached:
                        raw, mime = cached[url.path]
                        return self.reply(200, raw, mime)
                    if url.path == "/api/config":
                        return self.reply(200, service.configuration())
                    if url.path == "/api/organization":
                        q = parse_qs(url.query, strict_parsing=True, keep_blank_values=True)
                        if "kind" not in q or set(q) - {"kind", "state", "before", "limit"} or any(len(v) != 1 for v in q.values()):
                            raise ServiceError("invalid_organization_filter")
                        filters = {k: int(v[0]) if k in {"before", "limit"} else v[0] for k, v in q.items()}
                        return self.reply(200, service.organization(**filters))
                    if url.path == "/api/tasks":
                        q = parse_qs(
                            url.query, strict_parsing=True, keep_blank_values=True
                        )
                        if q and q != {"summary": ["1"]}:
                            raise ServiceError("invalid_task_list_query")
                        return self.reply(200, service.list_jobs(summary=bool(q)))
                    parts = url.path.strip("/").split("/")
                    if len(parts) == 4 and parts[:3] == ["api", "repositories", "jobs"]:
                        return self.reply(200, service.repository_manager.get(parts[3]))
                    if (
                        len(parts) == 4
                        and parts[:2] == ["api", "repositories"]
                        and parts[3] == "directory"
                    ):
                        q = parse_qs(
                            url.query, strict_parsing=True, keep_blank_values=True
                        )
                        if set(q) - {"path", "cursor"} or any(
                            len(v) != 1 for v in q.values()
                        ):
                            raise ServiceError("invalid_directory_query")
                        return self.reply(
                            200,
                            service.repository_directory(
                                parts[2],
                                q.get("path", [""])[0],
                                int(q.get("cursor", ["0"])[0]),
                            ),
                        )
                    if url.path == "/api/sessions" or (
                        len(parts) == 4
                        and parts[:2] == ["api", "sessions"]
                        and parts[3] == "turns"
                    ):
                        q = parse_qs(
                            url.query, strict_parsing=True, keep_blank_values=True
                        )
                        if set(q) - {"before", "limit"} or any(
                            len(v) != 1 for v in q.values()
                        ):
                            raise ServiceError("invalid_history_page")
                        page = {k: int(v[0]) for k, v in q.items()}
                        data = (
                            service.sessions(**page)
                            if url.path == "/api/sessions"
                            else service.turns(parts[2], **page)
                        )
                        return self.reply(200, data)
                    if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
                        return self.reply(200, service.get(parts[2]))
                    if (
                        len(parts) == 5
                        and parts[:2] == ["api", "tasks"]
                        and parts[3] == "source"
                    ):
                        q = parse_qs(url.query, strict_parsing=True)
                        if set(q) - {"line", "column", "context"} or any(
                            len(v) != 1 for v in q.values()
                        ):
                            raise ServiceError("invalid_cursor")
                        if q.get("context", ["0"])[0] not in {"0", "1"}:
                            raise ServiceError("invalid_cursor")
                        start = int(q["line"][0]) if "line" in q else None
                        return self.reply(
                            200,
                            service.source(
                                parts[2],
                                int(parts[4]),
                                start,
                                int(q.get("column", ["0"])[0]),
                                q.get("context", ["0"])[0] == "1",
                            ),
                        )
                self.reply(404, {"error": "not_found"})
            except ServiceError as error:
                self.reply(error.status, {"error": error.code})
            except GitHubError as error:
                self.reply(400, {"error": error.code})
            except (ValueError, OSError, ValidationError):
                self.reply(400, {"error": "invalid_request_or_unavailable_source"})
            except Exception:
                self.reply(500, {"error": "local_service_error"})
            finally:
                # 不复用非法/未消费完请求体的连接，避免请求边界歧义。
                self.close_connection = True

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request(post=True)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
