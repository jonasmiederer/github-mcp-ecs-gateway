#!/usr/bin/env bash
# Deploy the GitHub MCP chain: Kiro -> AgentCore Gateway (Entra inbound) ->
# gateway-brokered GitHub OAuth (Authorization Code / 3LO) -> private MCP on ECS.
# Per-user consent is handled by the AWS-MANAGED AgentCore Identity CONSENT
# PORTAL; there is no self-hosted callback. See docs/HOW-IT-WORKS.md.
#
# End users never need AWS access: they open the portal URL, sign in with Entra,
# click Connect on GitHub, approve — the portal calls CompleteResourceTokenAuth
# for them. A RESPONSE interceptor on the gateway rewrites the -32042 elicitation
# URL to the portal, so Kiro's "Open URL" prompt lands users on the portal.
#
# Pattern: awslabs/amazon-bedrock-agentcore-samples
#   01-features/05-authenticate-and-authorize/07-consent-portal-auth-code-flow-targets
# and the AWS blog "Manage end-user OAuth consent for AI agents with Amazon
# Bedrock AgentCore".
#
# Sequence (order matters — each step needs the previous one's output):
#   1/6  Image from ../mcp_server, tagged src-<hash of the source>;
#        skipped when that tag is already in ECR.
#   2/6  Gateway stack, FIRST pass: gateway, Entra primary-IdP provider, GitHub
#        outbound provider, portal execution role, interceptor Lambda, gateway
#        APPLICATION_LOGS. On a fresh deployment PortalUrl/PortalArn are empty
#        (interceptor not attached, wildcard portal trust); on an existing one
#        the stack's current values are reused, so the pass changes nothing.
#   3/6  Consent portal via boto3 (no CFN type exists yet) — its source is the
#        gateway from 2/6. Writes PORTAL_* to .env and registers
#        <portalUrl>/connect/callback as the workload identity's
#        AllowedResourceOauth2ReturnUrl (required, or the 3LO is refused).
#   4/6  Network + private ECS stack (01-network-ecs.yaml).
#   5/6  Target stack — DefaultReturnUrl = <portalUrl>/connect/callback from 3/6.
#   6/6  Gateway stack, SECOND pass with PortalUrl + PortalArn set: attaches the
#        RESPONSE interceptor and pins the portal role's trust to this portal.
#        Plain in-place update; the gateway is NOT replaced.
#
# Re-running against an existing deployment with unchanged files is a no-op:
# no image build, empty changesets, the portal is reused by name.
#
# Stacks: ${PROJECT}-gateway, ${PROJECT}-network, ${PROJECT}-target.
#
# NOTE: `aws cloudformation deploy` is gated for agents — run this yourself.
# Prereqs in .env / docs/GITHUB-SETUP.md / docs/ENTRA-SETUP.md.
#
# Requires: boto3/botocore >= 1.43.88 (consent-portal APIs). Check:
#   python3 -c 'import boto3;print(boto3.__version__)'
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT_DIR/.env" ] && . "$ROOT_DIR/.env"

: "${AWS_PROFILE:?export AWS_PROFILE}"
REGION="${AWS_REGION:-eu-central-1}"
export AWS_REGION="$REGION"
PROJECT="${PROJECT_NAME:-github-mcp-ecs}"
CONTAINER_CLI="${CONTAINER_CLI:-podman}"
GITHUB_SCOPE="${GITHUB_SCOPE:-repo}"

# GitHub OAuth App + Entra (see docs/GITHUB-SETUP.md, docs/ENTRA-SETUP.md).
: "${GITHUB_CLIENT_ID:?}"; : "${GITHUB_CLIENT_SECRET_ARN:?}"
: "${ENTRA_TENANT_ID:?}"; : "${ENTRA_API_APP_ID:?}"
SECRET_JSON_KEY="${GITHUB_CLIENT_SECRET_JSON_KEY:-client_secret}"

# Consent portal login: the portal signs users in SERVER-SIDE as the Entra app
# and exchanges the auth code with a CLIENT SECRET (the Kiro/mcp-remote public
# PKCE client does not use one). Create a client secret on the Entra app
# (Certificates & secrets) and store it in Secrets Manager as
# {"client_secret":"..."}; put its ARN here.
: "${ENTRA_PORTAL_CLIENT_SECRET_ARN:?export ENTRA_PORTAL_CLIENT_SECRET_ARN (Secrets Manager ARN of an Entra app client secret — see docs/ENTRA-SETUP.md)}"
ENTRA_SECRET_JSON_KEY="${ENTRA_PORTAL_CLIENT_SECRET_JSON_KEY:-client_secret}"

# Private DNS name of the MCP (internal ALB) + a PUBLIC ACM cert for it.
: "${MCP_DOMAIN_NAME:?export MCP_DOMAIN_NAME (private DNS name of the MCP endpoint)}"
: "${ACM_CERT_ARN:?export ACM_CERT_ARN (public cert for MCP_DOMAIN_NAME)}"

# Stack/resource prefix of the deployed chain.
PROVIDER="${GITHUB_PROVIDER_NAME:-github-mcp-provider}"
IDP_PROVIDER="${ENTRA_IDP_PROVIDER_NAME:-github-mcp-portal-idp}"
INTERCEPTOR_MODE="${INTERCEPTOR_MODE:-REWRITE}"   # REWRITE | LOG_ONLY

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ECR_REPO="${PROJECT}-mcp"
# Content-addressed tag: a hash of the server source (Dockerfile, requirements,
# server.py). An unchanged source maps to the same tag, so a re-run neither
# rebuilds nor touches the ECS service; a changed source gets a new tag, a new
# task definition revision, and a rolling deployment. (A fixed :latest tag
# would never roll out a code change, because CloudFormation sees no change.)
SRC_HASH="$(cd "$ROOT_DIR/mcp_server" \
  && find . -type f ! -name '.DS_Store' ! -name '*.pyc' ! -path '*/__pycache__/*' | LC_ALL=C sort \
  | xargs shasum -a 256 | shasum -a 256 | cut -c1-12)"
IMAGE_TAG="src-${SRC_HASH}"
IMAGE_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"

out() { aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }

# Current value of a stack parameter; empty if the stack or parameter does not exist.
param() {
  local v
  v=$(aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
        --query "Stacks[0].Parameters[?ParameterKey=='$2'].ParameterValue" --output text 2>/dev/null) || v=""
  [ "$v" = "None" ] && v=""
  printf '%s' "$v"
}

clean_dead() {
  local s="$1" st
  st=$(aws cloudformation describe-stacks --stack-name "$s" --region "$REGION" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)
  if [ "$st" = "ROLLBACK_COMPLETE" ] || [ "$st" = "REVIEW_IN_PROGRESS" ] || [ "$st" = "CREATE_FAILED" ]; then
    echo "   ($s is $st — deleting before deploy)"
    aws cloudformation delete-stack --stack-name "$s" --region "$REGION"
    aws cloudformation wait stack-delete-complete --stack-name "$s" --region "$REGION"
  fi
}

# The gateway stack is deployed twice (before and after the portal exists); keep
# the parameter list in one place so both passes stay identical except for the
# two portal parameters.
deploy_gateway_stack() {
  local portal_url="$1" portal_arn="$2"
  clean_dead "${PROJECT}-gateway"
  # CAPABILITY_NAMED_IAM: PortalExecutionRole has an explicit RoleName.
  aws cloudformation deploy \
    --template-file "$ROOT_DIR/templates/02-gateway-identity.yaml" \
    --stack-name "${PROJECT}-gateway" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --region "$REGION" \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
      ProjectName="$PROJECT" \
      GitHubProviderName="$PROVIDER" \
      GitHubClientId="$GITHUB_CLIENT_ID" GitHubClientSecretArn="$GITHUB_CLIENT_SECRET_ARN" \
      GitHubClientSecretJsonKey="$SECRET_JSON_KEY" \
      EntraTenantId="$ENTRA_TENANT_ID" EntraApiAppId="$ENTRA_API_APP_ID" \
      EntraIdpProviderName="$IDP_PROVIDER" \
      EntraPortalClientSecretArn="$ENTRA_PORTAL_CLIENT_SECRET_ARN" \
      EntraPortalClientSecretJsonKey="$ENTRA_SECRET_JSON_KEY" \
      InterceptorMode="$INTERCEPTOR_MODE" \
      PortalUrl="$portal_url" PortalArn="$portal_arn"
}

echo "== 1/6  Image $ECR_REPO:$IMAGE_TAG =="
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$ECR_REPO" --region "$REGION" >/dev/null
if aws ecr describe-images --repository-name "$ECR_REPO" --image-ids imageTag="$IMAGE_TAG" \
     --region "$REGION" >/dev/null 2>&1; then
  echo "   already in ECR (source unchanged) - skipping build"
else
  aws ecr get-login-password --region "$REGION" \
    | "$CONTAINER_CLI" login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
  "$CONTAINER_CLI" build --platform linux/amd64 -t "$IMAGE_URI" "$ROOT_DIR/mcp_server"
  "$CONTAINER_CLI" push "$IMAGE_URI"
fi

echo "== 2/6  Gateway + identity stack, first pass =="
# Fresh deployment: no portal yet, so no interceptor and a wildcard portal trust.
# Existing deployment: keep the portal values the stack already has, so this
# pass is a no-op instead of detaching the interceptor until step 6.
deploy_gateway_stack "$(param "${PROJECT}-gateway" PortalUrl)" "$(param "${PROJECT}-gateway" PortalArn)"
GW_ID="$(out "${PROJECT}-gateway" GatewayIdentifier)"
PROVIDER_ARN="$(out "${PROJECT}-gateway" GitHubProviderArn)"
echo "   GatewayIdentifier=$GW_ID"
echo "   GitHubProviderArn=$PROVIDER_ARN"

echo "== 3/6  Consent portal (boto3 — no CloudFormation type yet) =="
# Creates (or reuses) the portal with the gateway as its source, waits for
# ACTIVE, registers the return URL, and writes PORTAL_* to .env.
python3 "$ROOT_DIR/scripts/deploy-portal.py"
# Pick up PORTAL_URL / PORTAL_ARN / PORTAL_CONNECT_RETURN_URL the script just wrote.
. "$ROOT_DIR/.env"
: "${PORTAL_URL:?deploy-portal.py did not write PORTAL_URL}"
: "${PORTAL_ARN:?deploy-portal.py did not write PORTAL_ARN}"
: "${PORTAL_CONNECT_RETURN_URL:?deploy-portal.py did not write PORTAL_CONNECT_RETURN_URL}"
echo "   PortalUrl=$PORTAL_URL"

echo "== 4/6  Network + private ECS stack =="
clean_dead "${PROJECT}-network"
aws cloudformation deploy \
  --template-file "$ROOT_DIR/templates/01-network-ecs.yaml" \
  --stack-name "${PROJECT}-network" \
  --capabilities CAPABILITY_IAM --region "$REGION" \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    ProjectName="$PROJECT" ImageUri="$IMAGE_URI" \
    McpDomainName="$MCP_DOMAIN_NAME" AcmCertificateArn="$ACM_CERT_ARN"
VPC="$(out "${PROJECT}-network" VpcId)"
SUBNETS="$(out "${PROJECT}-network" PrivateSubnets)"
LATTICE_SG="$(out "${PROJECT}-network" LatticeSecurityGroupId)"
MCP_URL="$(out "${PROJECT}-network" McpPrivateEndpoint)"

echo "== 5/6  Target stack (gateway-brokered 3LO, return URL = portal) =="
clean_dead "${PROJECT}-target"
aws cloudformation deploy \
  --template-file "$ROOT_DIR/templates/03-mcp-target.yaml" \
  --stack-name "${PROJECT}-target" \
  --region "$REGION" \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    GatewayIdentifier="$GW_ID" McpPrivateEndpoint="$MCP_URL" \
    VpcId="$VPC" PrivateSubnets="$SUBNETS" LatticeSecurityGroupId="$LATTICE_SG" \
    GitHubProviderArn="$PROVIDER_ARN" \
    PortalConnectReturnUrl="$PORTAL_CONNECT_RETURN_URL" \
    GitHubScope="$GITHUB_SCOPE"

echo "== 6/6  Gateway stack, second pass - attach the interceptor, pin the portal trust =="
deploy_gateway_stack "$PORTAL_URL" "$PORTAL_ARN"

echo
echo "== MANUAL — Entra app registration ($ENTRA_API_APP_ID) =="
echo "  1. Authentication → Web → add redirect URI (EXACT, no trailing slash):"
echo "       ${PORTAL_URL}/callback"
echo "     This is the portal's own login callback. Separate from the publicClient"
echo "     localhost redirect that Kiro's mcp-remote uses; the two coexist."
echo "  2. Confirm the app issues v2 access tokens (api.requestedAccessTokenVersion=2)"
echo "     and that api://${ENTRA_API_APP_ID}/mcp.invoke is an exposed scope with"
echo "     admin consent — the portal requests exactly that scope so its Entra 'sub'"
echo "     matches the one the gateway sees (sub is pairwise on aud)."
echo
echo "Gateway $GW_ID hosts target 'GithubMcp' (scope: $GITHUB_SCOPE)."
echo "Gateway URL:           $(out "${PROJECT}-gateway" GatewayUrl)"
echo "Consent portal URL:    $PORTAL_URL   (share this with users; interceptor mode: $INTERCEPTOR_MODE)"
echo
echo "User flow: open the portal → sign in with Entra → Connect GitHub → approve →"
echo "  back in Kiro, run a GitHub tool. Or just run the tool first: Kiro's Open URL"
echo "  prompt now points at the portal instead of the raw identity URL."
echo "Done."
