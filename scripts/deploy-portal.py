#!/usr/bin/env python3
"""Create the AWS-managed AgentCore Identity CONSENT PORTAL for the gateway.

Why this is a script and not CloudFormation: as of 2026-09-25 there is no
AWS::BedrockAgentCore::ConsentPortal resource type (DescribeType ->
TypeNotFoundException). Everything else the portal needs IS in CloudFormation
(templates/02-gateway-identity.yaml): the Entra primary-IdP credential
provider, the portal execution role, the gateway, and the RESPONSE interceptor.
This script only does the piece CFN cannot, then feeds its results back:

  1. Reads the gateway stack outputs (gateway id, IdP provider ARN, role ARN).
  2. Creates the consent portal (idempotent by name), polls until ACTIVE *and*
     portalUrl is non-empty — the URL is what everything downstream needs.
  3. Registers <portalUrl>/connect/callback as the gateway workload identity's
     AllowedResourceOauth2ReturnUrl. Without this AgentCore Identity rejects the
     3LO ("Callback URL ... is not registered for AgentIdentity", 400). No CFN
     property exists for it and the portal does not do it itself.
  4. Writes PORTAL_ID / PORTAL_ARN / PORTAL_URL / PORTAL_CALLBACK_URL /
     PORTAL_CONNECT_RETURN_URL to .env for the next deploy steps. The gateway
     stack's second pass uses PORTAL_ARN to pin the execution role's trust
     (aws:SourceArn) to this portal, so CloudFormation owns that policy.
  5. Prints the ONE manual Entra step: register <portalUrl>/callback (no
     trailing slash) as a Web redirect URI on the Entra app.

End users never touch AWS: they open the portal URL, sign in with Entra, click
Connect on GitHub, approve — the portal calls CompleteResourceTokenAuth for them.

Pattern: awslabs/amazon-bedrock-agentcore-samples
  01-features/05-authenticate-and-authorize/07-consent-portal-auth-code-flow-targets
  (deploy/02_create_portal.py, IDP_SETUP_ENTRA.md). API parameter names are taken
  from that sample, which is verified against a live account; the local SDK here
  is too old to expose the model, so the script enforces the floor up front.

Requires boto3/botocore >= 1.43.88 (consent-portal ops were added there):
    pip install -U 'boto3>=1.43.88' 'botocore>=1.43.88'

Run from the repo root, after the gateway stack is deployed:
    python3 scripts/deploy-portal.py
Env (from .env): AWS_PROFILE, AWS_REGION, PROJECT_NAME, ENTRA_API_APP_ID
Optional: PORTAL_NAME, PORTAL_SCOPES (default: openid <api://app/scope>)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# Consent-portal operations landed in this botocore release. On an older SDK
# they fail with "'...' object has no attribute 'create_consent_portal'", which
# reads like a missing feature rather than a stale SDK — so fail clearly first.
MIN_BOTO = (1, 43, 88)

TERMINAL = ("FAILED", "UPDATE_FAILED", "DELETING")
MAX_PORTAL_NAME_LEN = 50


# ── env helpers ────────────────────────────────────────────────────────────────


def check_boto_version() -> None:
    have = tuple(int(x) for x in boto3.__version__.split(".")[:3])
    if have < MIN_BOTO:
        floor = ".".join(map(str, MIN_BOTO))
        sys.exit(
            f"ERROR: boto3 {boto3.__version__} is too old for the consent-portal APIs "
            f"(need >= {floor}).\n"
            f"       pip install -U 'boto3>={floor}' 'botocore>={floor}'"
        )


def load_env() -> None:
    """Read .env (export KEY=val or KEY=val) without clobbering real exports."""
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def must_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ERROR: {name} is not set. Export it or add it to .env.")
    return v


def save_env(**kv: str) -> None:
    """Upsert KEY=value lines in .env as `export KEY=value`, preserving the rest."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    remaining = dict(kv)
    out: list[str] = []
    for raw in lines:
        s = raw.strip()
        body = s[len("export "):] if s.startswith("export ") else s
        if "=" in body and not s.startswith("#"):
            key = body.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"export {key}={remaining.pop(key)}")
                continue
        out.append(raw)
    if remaining:
        out.append("")
        out.append("# Consent portal (written by scripts/deploy-portal.py)")
        out.extend(f"export {k}={v}" for k, v in remaining.items())
    ENV_PATH.write_text("\n".join(out) + "\n")
    try:
        ENV_PATH.chmod(0o600)  # .env holds ARNs of secrets; keep it owner-only
    except OSError:
        pass
    for k, v in kv.items():
        os.environ[k] = v


# ── AWS helpers ────────────────────────────────────────────────────────────────


def stack_outputs(cfn, stack: str) -> dict[str, str]:
    try:
        st = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    except ClientError as e:
        sys.exit(f"ERROR: cannot read stack {stack}: {e.response['Error']['Message']}\n"
                 f"       Deploy templates/02-gateway-identity.yaml first.")
    return {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}


def find_portal_by_name(control, name: str) -> dict | None:
    """Paged — a truncated scan would silently create a duplicate portal."""
    kwargs: dict = {"maxResults": 50}
    while True:
        page = control.list_consent_portals(**kwargs)
        for item in page.get("consentPortals", []):
            if item.get("name") == name:
                return item
        token = page.get("nextToken")
        if not token:
            return None
        kwargs["nextToken"] = token


def create_portal_with_retry(control, **kwargs):
    """A role created seconds ago may not be assumable yet; the API reports that
    as a ValidationException about the role, not a retryable throttle."""
    for attempt in range(6):
        try:
            return control.create_consent_portal(**kwargs)
        except ClientError as e:
            err = e.response["Error"]
            msg = err.get("Message", "").lower()
            retryable = err["Code"] == "ValidationException" and ("role" in msg or "assume" in msg)
            if not retryable or attempt == 5:
                raise
            print(f"    execution role not assumable yet, retrying: {err.get('Message')}")
            time.sleep(10)
    raise AssertionError("unreachable")


def wait_for_active(control, portal_id: str) -> str:
    """Poll until ACTIVE with a portalUrl. Waiting on the URL, not just ACTIVE,
    because the URL is what every later step needs."""
    print("  waiting for ACTIVE…")
    last = None
    for _ in range(30):
        p = control.get_consent_portal(consentPortalIdentifier=portal_id)
        status = p["status"]
        if status != last:
            print(f"    status: {status}")
            last = status
        url = p.get("portalUrl")
        if status == "ACTIVE" and url:
            return url
        if status in TERMINAL:
            reason = p.get("statusReason")
            sys.exit(f"ERROR: portal reached terminal status {status}" + (f"\n  reason: {reason}" if reason else ""))
        time.sleep(10)
    sys.exit(
        "ERROR: portal did not become ACTIVE with a portalUrl in time.\n"
        f"  check: aws bedrock-agentcore-control get-consent-portal --consent-portal-identifier {portal_id}"
    )


def register_return_url(control, gateway_id: str, return_url: str) -> None:
    """Allow-list the portal's return URL on the gateway's workload identity.

    The workload identity is created on our behalf by the Gateway and is named
    after the gateway id. Idempotent: a no-op if the list already equals the
    desired value.
    """
    current = control.get_workload_identity(name=gateway_id).get("allowedResourceOauth2ReturnUrls", [])
    if current == [return_url]:
        print(f"  • Return URL already registered on {gateway_id}")
        return
    if current:
        print(f"  • Replacing stale return URL(s): {current}")
    control.update_workload_identity(name=gateway_id, allowedResourceOauth2ReturnUrls=[return_url])


# ── main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    check_boto_version()
    load_env()

    region = must_env("AWS_REGION")
    project = os.environ.get("PROJECT_NAME", "github-mcp-ecs")
    gateway_stack = f"{project}-gateway"

    # Audience must be the BARE app GUID: v2 Entra tokens put the application id
    # in `aud`; the api://<GUID> identifier URI never appears there.
    audience = must_env("ENTRA_API_APP_ID")

    # Scopes: openid is mandatory (the portal always requests it), and the
    # resource scope must target the SAME api://<GUID> audience the gateway
    # validates, so the Entra `sub` (pairwise on aud) matches between portal
    # sign-in and gateway call. Default mirrors kiro/mcp.json's mcp.invoke scope.
    scopes = os.environ.get("PORTAL_SCOPES", f"openid api://{audience}/mcp.invoke").split()
    if "openid" not in scopes:
        scopes.insert(0, "openid")

    name = os.environ.get("PORTAL_NAME") or f"{project}-consent-portal"
    if not 1 <= len(name) <= MAX_PORTAL_NAME_LEN:
        sys.exit(f"ERROR: portal name must be 1-{MAX_PORTAL_NAME_LEN} chars; got {name!r} ({len(name)})")

    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    control = session.client("bedrock-agentcore-control")

    print(f"--- Reading {gateway_stack} outputs ---")
    out = stack_outputs(cfn, gateway_stack)
    try:
        gateway_id = out["GatewayIdentifier"]
        idp_arn = out["EntraIdpProviderArn"]
        role_arn = out["PortalExecutionRoleArn"]
    except KeyError as k:
        sys.exit(f"ERROR: stack output {k} missing — redeploy the gateway stack from the current template.")
    print(f"  gateway:  {gateway_id}")
    print(f"  idp arn:  {idp_arn}")
    print(f"  role arn: {role_arn}")

    print("\n--- Consent portal ---")
    print(f"  name:     {name}")
    print(f"  scopes:   {' '.join(scopes)}")
    print(f"  audience: {audience}")
    existing = find_portal_by_name(control, name)
    if existing:
        portal_id = existing["consentPortalId"]
        print(f"  • Reusing existing portal: {portal_id}")
        p = control.get_consent_portal(consentPortalIdentifier=portal_id)
        portal_arn = p["consentPortalArn"]
        portal_url = p.get("portalUrl")
        if not portal_url or p["status"] != "ACTIVE":
            portal_url = wait_for_active(control, portal_id)
    else:
        resp = create_portal_with_retry(
            control,
            name=name,
            executionRoleArn=role_arn,
            idpConfig={
                "credentialProviderArn": idp_arn,
                "scopes": scopes,
                "audience": audience,
            },
            # Exactly one source, immutable for the portal's life.
            sources=[{"identifier": gateway_id, "type": "agentcore-gateway"}],
            description=f"GitHub MCP consent portal backed by Entra ID — {project}",
        )
        portal_id = resp["consentPortalId"]
        portal_arn = resp["consentPortalArn"]
        print(f"  ✓ Created. ID: {portal_id}")
        portal_url = resp.get("portalUrl")
        if not portal_url or resp.get("status") != "ACTIVE":
            portal_url = wait_for_active(control, portal_id)

    # portalUrl's scheme is not guaranteed (seen both with and without https://).
    # Normalize to a bare host, then derive both URLs with the scheme attached.
    bare = portal_url.removeprefix("https://").rstrip("/")
    portal_https = f"https://{bare}"
    callback_url = f"{portal_https}/callback"            # Entra login callback (register on the app)
    connect_return_url = f"{portal_https}/connect/callback"  # target DefaultReturnUrl

    # AgentCore Identity refuses to start a 3LO whose return URL is not on the
    # gateway workload identity's allow-list — the tools/call fails with
    # "Callback URL ... is not registered for AgentIdentity <gateway-id>" (400),
    # which the client sees as a generic internal error. There is no CFN property
    # for this list and the portal does NOT register itself, so do it here.
    # update-workload-identity REPLACES the list: the portal's /connect/callback
    # is the only return URL used, so it is set to exactly that.
    register_return_url(control, gateway_id, connect_return_url)
    print(f"  ✓ Registered return URL on workload identity {gateway_id}")

    save_env(
        PORTAL_ID=portal_id,
        PORTAL_ARN=portal_arn,
        PORTAL_URL=portal_https,
        PORTAL_CALLBACK_URL=callback_url,
        PORTAL_CONNECT_RETURN_URL=connect_return_url,
    )
    print(f"\n  portal url: {portal_https}")
    print("  Saved to .env: PORTAL_ID, PORTAL_ARN, PORTAL_URL, PORTAL_CALLBACK_URL, PORTAL_CONNECT_RETURN_URL")

    print()
    print("=" * 72)
    print("  MANUAL — do this now, before users sign in to the portal:")
    print()
    print("  Register the portal's login callback as a *Web* redirect URI on the")
    print(f"  Entra app {audience}. Enter it EXACTLY, with NO trailing slash —")
    print("  a trailing slash makes Entra reject it as unregistered and the failure")
    print("  surfaces as a generic /?error=login_failed.")
    print()
    print(f"      {callback_url}")
    print()
    print("  Entra admin center → App registrations → the app → Authentication →")
    print("  Web → Add URI.   Or with the Azure CLI (--web-redirect-uris REPLACES")
    print("  the whole array, so pass every existing Web URI too):")
    print(f"      az ad app update --id {audience} --web-redirect-uris <existing…> {callback_url}")
    print()
    print("  This is separate from the publicClient (localhost) redirect Kiro's")
    print("  mcp-remote uses; the two arrays coexist.")
    print("=" * 72)
    print()
    print("Verify the login leg: open", portal_https)
    print("  Sign in with an Entra user. An EMPTY Connections page is the success")
    print("  condition at this point — the GitHub target is not attached yet.")
    print()
    print("Next: deploy.sh continues with the target stack, passing")
    print("  PortalConnectReturnUrl=$PORTAL_CONNECT_RETURN_URL, then redeploys the")
    print("  gateway stack with PortalUrl=$PORTAL_URL and PortalArn=$PORTAL_ARN to attach")
    print("  the interceptor and pin the portal role's trust.")


if __name__ == "__main__":
    main()
