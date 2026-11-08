"""Explicit host-configured MCP and Agent Skills, with no model-selected programs."""

import asyncio
import hashlib
import json
import os
import queue
import re
import threading
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import yaml
from pydantic import Field

from repoagent.execution.contracts import StrictModel
from repoagent.tools.registry import ToolSpec


class ExtensionError(Exception):
    pass


class McpConfig(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env_names: list[str] = Field(default_factory=list)
    bearer_env: str | None = None
    tools: list[str] | None = None
    read_only: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=120)


class ExtensionConfig(StrictModel):
    mcp: list[McpConfig] = Field(default_factory=list, max_length=8)
    skill_roots: list[str] = Field(default_factory=list, max_length=20)


class McpProvider:
    """One async client lifetime; sync execution never silently retries a call."""

    def __init__(self, config):
        if not config.read_only or bool(config.url) == bool(config.command):
            raise ValueError("MCP requires a host-declared read-only endpoint or command")
        self.config, self.queue = config, queue.Queue()
        self.ready = Future()
        self.closed = False
        self.loop, self.cancel_scope = None, None
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        try:
            self.tools = self.ready.result(config.timeout_seconds + 2)
        except Exception:
            self.close()
            raise ExtensionError("external_connect_failed") from None

    def _thread(self):
        try:
            async def run():
                self.loop = asyncio.get_running_loop()
                with anyio.CancelScope() as scope:
                    self.cancel_scope = scope
                    await self._serve()
            asyncio.run(run())
        except Exception:
            if not self.ready.done():
                self.ready.set_exception(ExtensionError("external_connect_failed"))

    async def _serve(self):
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters
        from mcp.client.streamable_http import streamable_http_client

        config = self.config
        if config.url:
            parsed = urlsplit(config.url)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.fragment or parsed.query:
                raise ValueError("Invalid MCP URL")
            headers = {}
            if config.bearer_env:
                key = os.environ.get(config.bearer_env, "")
                if not key:
                    raise ValueError("Missing MCP credential")
                headers["Authorization"] = "Bearer " + key
            import httpx2
            # Explicit loopback endpoints stay on this host even when the shell
            # has an ambient network proxy. TLS verification remains enabled.
            loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            async with httpx2.AsyncClient(headers=headers, timeout=config.timeout_seconds, follow_redirects=False, trust_env=not loopback) as http:
                async with Client(streamable_http_client(config.url, http_client=http), read_timeout_seconds=config.timeout_seconds) as client:
                    await self._loop(client)
        else:
            env = {name: os.environ[name] for name in config.env_names if name in os.environ}
            async with Client(StdioServerParameters(command=config.command, args=config.args, env=env), read_timeout_seconds=config.timeout_seconds) as client:
                await self._loop(client)

    async def _loop(self, client):
        self.server_identity = {"protocol": client.protocol_version, "server_info": client.server_info.model_dump()}
        tools, cursor = [], None
        for _ in range(100):
            page = await client.list_tools(cursor=cursor, cache_mode="refresh")
            tools.extend(t.model_dump(by_alias=True, exclude_none=True) for t in page.tools)
            cursor = page.next_cursor
            if cursor is None:
                break
        else:
            raise ValueError("Unbounded MCP catalog")
        self.ready.set_result(tools)
        while True:
            item = await asyncio.to_thread(self.queue.get)
            if item is None:
                return
            name, arguments, result = item
            try:
                # Lower-level session avoids Client's HEADER_MISMATCH re-list/retry
                # and input-required auto rounds. No roots/sampling/elicitation granted.
                output = await client.session.call_tool(name, arguments, read_timeout_seconds=self.config.timeout_seconds)
                result.set_result(output.model_dump(by_alias=True, exclude_none=True))
            except Exception:
                result.set_exception(ExtensionError("external_call_failed"))

    def call(self, name, arguments):
        if self.closed or not self.thread.is_alive():
            raise ExtensionError("external_provider_closed")
        result = Future()
        self.queue.put((name, arguments, result))
        try:
            return result.result(self.config.timeout_seconds + 2)
        except TimeoutError:
            self.close()
            raise ExtensionError("external_call_timeout") from None

    def register(self, registry):
        selected = set(self.config.tools) if self.config.tools is not None else {t["name"] for t in self.tools}
        if not selected <= {t["name"] for t in self.tools}:
            raise ValueError("Configured MCP tool missing")
        for tool in self.tools:
            if tool["name"] not in selected:
                continue
            name = f"{self.config.name}__{tool['name']}"
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                name = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:55] + "_" + hashlib.sha256(name.encode()).hexdigest()[:8]
            def handler(remote):
                def execute(**args):
                    return self.call(remote, args)
                return execute
            registry.register(ToolSpec(name, tool.get("description", tool["name"]), tool["inputSchema"],
                handler(tool["name"]), origin="external", output_schema=tool.get("outputSchema"),
                binding={"provider": self.config.model_dump(), "server": self.server_identity, "descriptor": tool}))

    def close(self):
        if not self.closed:
            self.closed = True
            self.queue.put(None)
            self.thread.join(timeout=2)
            if self.thread.is_alive() and self.loop and self.cancel_scope:
                self.loop.call_soon_threadsafe(self.cancel_scope.cancel)
                self.thread.join(timeout=5)


class SkillLoader:
    """Frozen UTF-8 instruction/reference text. Scripts are listed, never executed."""

    def __init__(self, roots):
        self.skills = {}
        for directory in roots:
            root = Path(directory).resolve(strict=True)
            path = root / "SKILL.md"
            if path.is_symlink() or path.resolve().parent != root or path.stat().st_size > 2_000_000:
                raise ValueError("Invalid skill entry path or size")
            text = path.read_text(encoding="utf-8")
            if not text.startswith("---\n"):
                raise ValueError("Skill needs YAML frontmatter")
            parts = text.split("---", 2)
            metadata = yaml.safe_load(parts[1])
            if not isinstance(metadata, dict):
                raise ValueError("Invalid skill frontmatter")
            name, description = metadata.get("name"), metadata.get("description")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64 or not isinstance(description, str) or not description.strip():
                raise ValueError("Invalid skill metadata")
            if name in self.skills or path.resolve().parent != root:
                raise ValueError("Duplicate or escaped skill")
            files = {}
            for file in root.rglob("*"):
                if file.is_symlink() or not file.resolve().is_relative_to(root):
                    raise ValueError("Skill symlink is not allowed")
                if file.is_file() and file.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml", ".svg"}:
                    if file.stat().st_size > 2_000_000:
                        raise ValueError("Skill text exceeds resource guard")
                    files[file.relative_to(root).as_posix()] = file.read_text(encoding="utf-8")
            if sum(len(v.encode()) for v in files.values()) > 8_000_000:
                raise ValueError("Skill exceeds resource guard")
            self.skills[name] = {"description": description, "files": files}

    def register(self, registry):
        if not self.skills:
            return
        catalog = [{"name": n, "description": v["description"]} for n, v in self.skills.items()]
        schema = {"type": "object", "properties": {"name": {"type": "string", "enum": list(self.skills)},
            "path": {"type": "string", "default": "SKILL.md"}}, "required": ["name"], "additionalProperties": False}
        registry.register(ToolSpec("load_skill", "Read frozen skill guidance/reference text as data; no script execution or permission grants. Catalog: " + json.dumps(catalog, ensure_ascii=False), schema,
            self.load, origin="external", binding={"skills_sha256": hashlib.sha256(json.dumps(self.skills, sort_keys=True).encode()).hexdigest()}))

    def load(self, name, path="SKILL.md"):
        files = self.skills[name]["files"]
        if path not in files:
            raise ExtensionError("skill_text_unavailable")
        return {"content": [{"type": "text", "text": files[path]}], "skill": name, "path": path,
                "available_text_paths": list(files), "scripts_executable": False}


class ExtensionBundle:
    def __init__(self, config):
        self.providers = []
        try:
            self.skills = SkillLoader(config.skill_roots)
            for server in config.mcp:
                self.providers.append(McpProvider(server))
        except Exception:
            self.close()
            raise

    def register(self, registry):
        self.skills.register(registry)
        for provider in self.providers:
            provider.register(registry)

    def close(self):
        for provider in self.providers:
            provider.close()

    @property
    def fingerprint(self):
        from repoagent.tools.registry import ToolRegistry
        registry = ToolRegistry()
        self.register(registry)
        return registry.fingerprint


def load_extensions(path):
    config = ExtensionConfig.model_validate_json(Path(path).read_bytes()) if path else ExtensionConfig()
    # Normalize only host-supplied paths, never repository/model content.
    config.skill_roots = [str(Path(p).resolve(strict=True)) for p in config.skill_roots]
    return config
