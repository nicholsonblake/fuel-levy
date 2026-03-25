#!/usr/bin/env python3
"""
Power BI MCP Server
Lets Claude query your Power BI datasets, list reports, and explore data.
"""

import os
import json
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Config ───────────────────────────────────────────────────────────────────
TENANT_ID = os.environ["AZURE_TENANT_ID"]
CLIENT_ID = os.environ["AZURE_CLIENT_ID"]
CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]
PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

mcp = FastMCP(
    "powerbi",
    instructions=(
        "You have access to Power BI. Use list_workspaces to find workspaces, "
        "list_reports / list_datasets to browse content, get_tables to see what "
        "data is available, then run_dax_query to answer questions with data."
    ),
)

# ── Auth ─────────────────────────────────────────────────────────────────────
_token_cache: dict = {}


async def _get_token() -> str:
    """Get an Azure AD access token (cached until expiry)."""
    import time

    if _token_cache.get("expires_at", 0) > time.time() + 60:
        return _token_cache["access_token"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": "https://analysis.windows.net/powerbi/api/.default",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data["expires_in"]
        return data["access_token"]


async def _pbi_get(path: str) -> dict:
    """Make an authenticated GET to the Power BI REST API."""
    token = await _get_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PBI_BASE}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


async def _pbi_post(path: str, body: dict) -> dict:
    """Make an authenticated POST to the Power BI REST API."""
    token = await _get_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PBI_BASE}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()


# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_workspaces() -> str:
    """List all Power BI workspaces (groups) you have access to."""
    data = await _pbi_get("groups")
    workspaces = [
        {"id": g["id"], "name": g["name"]} for g in data.get("value", [])
    ]
    return json.dumps(workspaces, indent=2)


@mcp.tool()
async def list_reports(workspace_id: str) -> str:
    """List all reports in a workspace.

    Args:
        workspace_id: The workspace/group ID (get from list_workspaces).
    """
    data = await _pbi_get(f"groups/{workspace_id}/reports")
    reports = [
        {"id": r["id"], "name": r["name"], "datasetId": r.get("datasetId")}
        for r in data.get("value", [])
    ]
    return json.dumps(reports, indent=2)


@mcp.tool()
async def list_datasets(workspace_id: str) -> str:
    """List all datasets in a workspace.

    Args:
        workspace_id: The workspace/group ID.
    """
    data = await _pbi_get(f"groups/{workspace_id}/datasets")
    datasets = [
        {"id": d["id"], "name": d["name"]} for d in data.get("value", [])
    ]
    return json.dumps(datasets, indent=2)


@mcp.tool()
async def get_tables(workspace_id: str, dataset_id: str) -> str:
    """Get all tables and their columns in a dataset. Use this to understand
    what data is available before writing a DAX query.

    Args:
        workspace_id: The workspace/group ID.
        dataset_id: The dataset ID (get from list_datasets or list_reports).
    """
    data = await _pbi_get(
        f"groups/{workspace_id}/datasets/{dataset_id}/tables"
    )
    tables = []
    for t in data.get("value", []):
        cols = [
            {"name": c["name"], "dataType": c["dataType"]}
            for c in t.get("columns", [])
        ]
        tables.append({"name": t["name"], "columns": cols})
    return json.dumps(tables, indent=2)


@mcp.tool()
async def run_dax_query(workspace_id: str, dataset_id: str, dax: str) -> str:
    """Execute a DAX query against a Power BI dataset and return results.

    Use get_tables first to understand the schema. Write read-only EVALUATE
    queries — for example:
        EVALUATE TOPN(10, 'Sales', 'Sales'[Amount], DESC)

    Args:
        workspace_id: The workspace/group ID.
        dataset_id: The dataset ID.
        dax: The DAX query string (must start with EVALUATE).
    """
    body = {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }
    data = await _pbi_post(
        f"groups/{workspace_id}/datasets/{dataset_id}/executeQueries", body
    )
    results = data.get("results", [])
    if not results:
        return "No results returned."

    first = results[0]
    if "error" in first:
        return f"DAX Error: {json.dumps(first['error'], indent=2)}"

    rows = first.get("tables", [{}])[0].get("rows", [])
    return json.dumps(rows, indent=2)


@mcp.tool()
async def refresh_dataset(workspace_id: str, dataset_id: str) -> str:
    """Trigger a refresh for a Power BI dataset.

    Args:
        workspace_id: The workspace/group ID.
        dataset_id: The dataset ID.
    """
    token = await _get_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/refreshes",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
    return "Dataset refresh triggered successfully."


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
