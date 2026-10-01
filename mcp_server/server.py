"""
GitHub MCP server — gateway-brokered 3LO / PASSTHROUGH to target.

This is the SIDE-BY-SIDE alternative to the Method 2 server. See
../docs/CONSENT-UX-options.md and the AWS blog:
https://aws.amazon.com/blogs/machine-learning/connecting-mcp-servers-to-amazon-bedrock-agentcore-gateway-using-authorization-code-flow/

Auth model:
  - INBOUND to the Gateway : Entra JWT (customJWTAuthorizer) — unchanged, the
    Gateway is the inbound gate.
  - OUTBOUND GitHub OAuth   : owned by the GATEWAY, not this server. The target is
    configured with an OAuth2 CredentialProvider (GrantType AUTHORIZATION_CODE).
    The Gateway runs the 3LO, elicits consent to the client (MCP elicitation),
    caches the token in the AgentCore Token Vault, and attaches the resulting
    GitHub token as `Authorization: Bearer <github-token>` on its outbound call to
    THIS server.

Consequently this server has NO AgentCore Identity code: no @requires_access_token
decorator, no workload-token context binding, no session-binding callback, no
non-blocking poller. It simply reads the bearer token the Gateway forwards and
calls the GitHub API with it. That is the whole point of this design — GitHub auth
ownership, token brokering, and consent UX all move to the Gateway.

Trade-off vs Method 2: scopes are set ONCE on the target (see
../templates/03-mcp-target.yaml), so every tool here shares one scope set
and one token — per-tool least-privilege is lost. Documented in
../docs/CONSENT-UX-options.md.

Transport: streamable-http MCP (FastMCP) on 0.0.0.0:8080, reachable only from the
Gateway via the private VPC path (internal ALB + Lattice) — never publicly.
"""

from __future__ import annotations

import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("github-mcp")

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")

mcp = FastMCP(host="0.0.0.0", port=8080, stateless_http=True)


# ── Token extraction ─────────────────────────────────────────────────────────
def _bearer_token() -> str:
    """Return the GitHub token the Gateway attached as `Authorization: Bearer`.

    The Gateway brokers the GitHub OAuth token and forwards it on the
    outbound call to this target. We do not validate it here — GitHub does. If it
    is missing, the request did not come through a correctly-configured 3LO target.
    """
    request: Request = mcp.get_context().request_context.request
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise RuntimeError(
            "No bearer token on the request. The AgentCore Gateway "
            "brokers the GitHub token (target GrantType AUTHORIZATION_CODE) and "
            "forwards it as 'Authorization: Bearer'. Check the target's "
            "CredentialProviderConfigurations."
        )
    return auth.split(" ", 1)[1].strip()


async def _github_get(path: str, token: str) -> dict | list:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{GITHUB_API}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        r.raise_for_status()
        return r.json()


# ── Tools ────────────────────────────────────────────────────────────────────
# NOTE: unlike Method 2, these tools cannot vary scope — the target carries ONE
# OAuth scope set for all of them. Keep the tool surface identical to ../mcp_server
# so the two variants are directly comparable and the same schema-upfront tool
# definitions apply.


@mcp.tool()
async def get_authenticated_user() -> dict:
    """Return the GitHub profile of the calling user."""
    user = await _github_get("/user", _bearer_token())
    return {
        "login": user.get("login"),
        "name": user.get("name"),
        "id": user.get("id"),
        "html_url": user.get("html_url"),
    }


@mcp.tool()
async def list_repositories(per_page: int = 20) -> list[dict]:
    """List repositories the calling user can access."""
    repos = await _github_get(
        f"/user/repos?per_page={min(per_page, 100)}&sort=updated", _bearer_token()
    )
    return [
        {
            "full_name": r.get("full_name"),
            "private": r.get("private"),
            "html_url": r.get("html_url"),
            "default_branch": r.get("default_branch"),
        }
        for r in repos
    ]


@mcp.tool()
async def get_repository(owner: str, repo: str) -> dict:
    """Get metadata for a single repository."""
    r = await _github_get(f"/repos/{owner}/{repo}", _bearer_token())
    return {
        "full_name": r.get("full_name"),
        "description": r.get("description"),
        "private": r.get("private"),
        "stargazers_count": r.get("stargazers_count"),
        "open_issues_count": r.get("open_issues_count"),
        "html_url": r.get("html_url"),
    }


# ── Health check for the ALB target group ────────────────────────────────────
@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request):
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("ok")


if __name__ == "__main__":
    log.info("Starting GitHub MCP server (passthrough) on 0.0.0.0:8080")
    mcp.run(transport="streamable-http")
