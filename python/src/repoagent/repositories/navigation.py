"""有界目录线索：不读正文、不调用模型，不把候选路径当成源码证据。"""

import json
import re
from pathlib import PurePosixPath

from repoagent.repositories.sources import GitHubSource


def catalog_hints(sources, question, max_bytes=4096):
    terms = set(re.findall(r"[a-z0-9]+", question.casefold()))
    # 面向已确认的中文定位/流程问题；只是路径线索，语义关联仍须读取验证。
    for words, values in (
        (("登录", "认证"), ("login", "auth")),
        (("管理员",), ("admin",)),
        (("用户",), ("user", "member")),
        (("入口", "控制器", "请求"), ("controller", "route", "api")),
        (("配置", "地址"), ("config", "env", "http")),
        (("流程",), ("service",)),
        (("异常", "失败", "错误"), ("exception", "error")),
    ):
        if any(word in question for word in words):
            terms.update(values)
    remotes = [
        (name, source)
        for name, source in sorted(sources.items())
        if isinstance(source, GitHubSource)
    ]
    result = []
    per_repository = max_bytes // max(1, len(remotes))
    for name, source in remotes:
        groups = [[] for _ in range(5)]
        for e in source.store.entries(source.version):
            if e["policy"] != "allowed":
                continue
            path = PurePosixPath(e["path"])
            parts, stem = set(path.parts), path.stem.casefold()
            score = sum(4 * (t in stem) + (t in e["path"].casefold()) for t in terms)
            group = 4
            if parts & {"controller", "controllers", "routes", "router", "handlers"}:
                group = 0
            elif parts & {"api", "apis"}:
                group = 1
            elif (
                path.name.startswith((".env", "application"))
                or "config" in stem
                or path.name in {"README.md", "package.json", "pom.xml"}
            ):
                group = 2
            elif parts & {"service", "services", "store", "stores"}:
                group = 3
            groups[group].append((-score, len(path.parts), e["path"]))
        groups = [sorted(group)[:3] for group in groups]
        hint = {
            "repo_id": name,
            "catalog_only": True,
            "cached_files": source.descriptor().coverage["cached"],
            "paths": [],
        }
        for offset in range(3):
            for group in groups:
                if offset < len(group):
                    hint["paths"].append(group[offset][2])
                    if (
                        len(json.dumps(hint, ensure_ascii=False).encode())
                        > per_repository
                    ):
                        hint["paths"].pop()
        result.append(hint)
    # 额外列表/标点开销也受总上限限制，不能扩大模型输入预算。
    while len(json.dumps(result, ensure_ascii=False).encode()) > max_bytes:
        longest = max(result, key=lambda h: len(h["paths"]))
        if not longest["paths"]:
            return []
        longest["paths"].pop()
    return result
