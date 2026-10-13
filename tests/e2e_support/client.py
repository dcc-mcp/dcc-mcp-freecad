"""Official SDK transport. No direct handler fallback or native mock dispatch."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta


def body(response, allow_error=False):
    if response.isError and not allow_error:
        raise AssertionError("MCP returned an error envelope")
    value = response.structuredContent
    if value is None:
        texts = [item.text for item in response.content if getattr(item, "type", None) == "text"]
        assert len(texts) == 1, "MCP result is not unambiguous"
        value = json.loads(texts[0])
    assert isinstance(value, dict)
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def select_tools(catalog, loaded, needed, declarations):
    """Resolve actual advertised names; require full metadata for every tool."""
    selected = {}
    for skill, logical in needed:
        anchors = [
            item
            for item in loaded
            if item.get("skill_name") == skill
            and (item["name"].endswith("__" + logical) or item["name"].endswith("/" + logical))
        ]
        assert len(anchors) == 1, "Missing or ambiguous loaded tool: " + logical
        anchor = anchors[0]
        matches = [item for item in catalog if item["name"] in (logical, anchor["name"])]
        assert len(matches) == 1, "Missing or ambiguous wire tool: " + logical
        declared = declarations[logical]
        full = declared["input_schema"]
        wire = dict(full)
        if logical == "save_copy":
            assert canonical(full.get("if")) == canonical(
                {"required": ["view"], "properties": {"view": {"enum": ["front", "top", "right"]}}}
            )
            assert canonical(full.get("then")) == canonical({"required": ["visible_objects"]})
            wire = {key: value for key, value in full.items() if key not in {"if", "then"}}
        assert canonical(anchor["inputSchema"]) == canonical(full), (
            "Full source/load schema mismatch"
        )
        assert canonical(matches[0]["inputSchema"]) == canonical(wire), "Wire schema mismatch"
        for item in (anchor, matches[0]):
            assert canonical(item["outputSchema"]) == canonical(declared["output_schema"])
        assert full["additionalProperties"] is False
        selected[logical] = (matches[0]["name"], anchor["name"], anchor["inputSchema"])
    return selected


class Client:
    def __init__(self, session, selected, record):
        self.session, self.selected, self.record = session, selected, record

    async def call(self, logical, arguments, expect_success=True):
        from jsonschema import Draft7Validator

        wire, canonical, schema = self.selected[logical]
        Draft7Validator(schema).validate(arguments)
        self.record("request", logical, arguments)
        deadline = time.monotonic() + 90
        result = body(
            await self.session.call_tool(
                wire, arguments, read_timeout_seconds=timedelta(seconds=90)
            ),
            allow_error=not expect_success,
        )
        if result.get("core_job_id"):
            job = result["core_job_id"]
            assert result.get("job_id_owner") == "core"
            assert result.get("core_poll") == {
                "owner": "core",
                "tool": "jobs_get_status",
                "arguments": {"job_id": job, "include_result": True},
            }
            while True:
                assert time.monotonic() < deadline, "Core job exceeded its test deadline"
                result = body(
                    await self.session.call_tool(
                        "jobs_get_status",
                        {"job_id": job, "include_result": True},
                        read_timeout_seconds=timedelta(seconds=30),
                    )
                )
                assert result.get("job_id", job) == job
                if result.get("status") in ("pending", "queued", "running"):
                    await asyncio.sleep(0.25)
                    continue
                assert result.get("tool") in (wire, canonical)
                assert result.get("status") in ("completed", "failed")
                assert result["status"] == "completed", "Core infrastructure job failed"
                assert result.get("error") is None, "Core infrastructure reported an error"
                result = result["result"]
                break
        assert isinstance(result.get("success"), bool), "Malformed skill completion"
        success = result.get("success") is True and result.get("error") is None
        self.record(
            "result",
            logical,
            {
                "success": success,
                "verified_checks": result.get("context", {}).get("verified_checks", []),
            },
        )
        assert success is expect_success, "Unexpected typed-tool completion: " + logical
        return result.get("context", {}) if success else result


async def session_flow(url, needed, record, scenario):
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2] / "src/dcc_mcp_freecad/skills"
    declarations = {}
    for skill, _ in needed:
        for item in yaml.safe_load((root / skill / "tools.yaml").read_text())["tools"]:
            declarations[item["name"]] = item
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import PaginatedRequestParams

    async def catalog(session):
        tools, cursor = [], None
        for _ in range(8):
            page = await session.list_tools(
                params=PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            tools.extend(item.model_dump(mode="json", by_alias=True) for item in page.tools)
            cursor = page.nextCursor
            if not cursor:
                return tools
        raise AssertionError("Unexpected discovery pagination")

    async with httpx.AsyncClient(
        timeout=90, trust_env=False, transport=httpx.AsyncHTTPTransport(retries=0)
    ) as http:
        async with streamable_http_client(url, http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                record("initialize", "mcp", {"protocol_version": initialized.protocolVersion})
                await catalog(session)
                loaded = []
                for skill in ("freecad-session", "freecad-modeling"):
                    if skill not in {name for name, _ in needed}:
                        continue
                    reply = body(await session.call_tool("load_skill", {"skill_name": skill}))
                    assert reply.get("loaded") is True and reply.get("partial") is False
                    loaded.extend(reply["tools"])
                selected = select_tools(await catalog(session), loaded, needed, declarations)
                record("discovery", "mcp", {"tools": sorted(selected)})
                return await scenario(Client(session, selected, record))
