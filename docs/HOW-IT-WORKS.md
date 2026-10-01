# GitHub MCP behind Amazon Bedrock AgentCore Gateway, with Microsoft Entra ID as the only identity provider

## Overview

```
Kiro (mcp-remote) --Entra token--> AgentCore Gateway --GitHub token, via VPC Lattice--> internal ALB --> MCP server (ECS Fargate) --> api.github.com
                                        |      ^
                                        v      |  per-user GitHub token
                              AgentCore Identity (token vault)
                                               ^
                       AgentCore consent portal: user signs in with Entra, clicks "Connect" on GitHub
```

The user signs in twice, once per purpose:

- **Entra** proves who they are.
- **GitHub** grants what the agent may do.

AgentCore Identity links the two per user. The MCP server has no identity code and stores no credentials.

## The identities involved

- **The user in Entra ID.** A single app registration plays three roles:
  - the API the gateway protects (the token audience),
  - the public client Kiro signs in with (PKCE),
  - the confidential client the consent portal signs in with (client secret).
- **The gateway's workload identity.** AgentCore creates this automatically for the gateway. GitHub tokens are stored per workload identity and per user.
- **The user's GitHub account.** The user authorizes a GitHub OAuth App. AgentCore Identity holds that app's client secret (in Secrets Manager) and exchanges the authorization code for a token.

## How a request flows

### 1. Kiro connects (inbound authentication)

- Kiro starts the gateway connection through `mcp-remote`. It signs the user in to Entra in the browser (authorization code with PKCE) and requests the scope `api://<app-id>/mcp.invoke`.
- Every MCP request carries the Entra access token. The gateway's JWT authorizer checks:
  - the signature and issuer, using Entra's v2 discovery document;
  - the audience, which must be the app ID.
- `tools/list` works right away. The tool schemas are defined on the gateway target, so listing tools doesn't need a GitHub token.

### 2. First tool call: consent required

- On `tools/call`, the gateway exchanges the user's Entra token for a workload access token and asks AgentCore Identity for that user's GitHub token. There isn't one yet.
- The gateway replies with an MCP URL elicitation (JSON-RPC error `-32042`, MCP protocol version 2025-11-25) that carries an authorization link.
- A small Lambda that intercepts gateway responses replaces that link with the consent portal URL. Kiro shows the link to the user. This pattern comes from the awslabs `amazon-bedrock-agentcore-samples` consent-portal sample.

### 3. Consent portal: connect GitHub (session binding)

1. The user opens the portal and signs in with Entra. This is usually silent, because they're already signed in in that browser. The portal requests the same scope and audience that Kiro does.
2. The Connections page lists GitHub. The user clicks Connect and approves on GitHub's authorize page (scope `repo`).
3. GitHub redirects to AgentCore Identity's callback, which exchanges the code for a GitHub token.
4. AgentCore Identity sends the browser back to the portal's `/connect/callback`.
5. The portal calls `CompleteResourceTokenAuth` with the user's Entra token. AgentCore Identity checks that this user is the one who started the authorization. Only then does it store the GitHub token for that user.

### 4. Tool calls from then on

- The user retries in Kiro. The gateway finds the stored token and calls the MCP server with `Authorization: Bearer <GitHub token>`.
- The path to the MCP server is private:
  - AgentCore creates a VPC Lattice resource gateway in the customer's private subnets. This is configured as the target's private endpoint.
  - The MCP hostname resolves only in a private hosted zone, to an internal ALB.
  - The ALB serves HTTPS with a public ACM certificate, because the gateway only accepts publicly trusted certificates.
  - The ALB forwards to the Fargate task. Security groups allow only Lattice to the ALB, and the ALB to the task.
- The MCP server calls `api.github.com` with the token, going out through a NAT gateway. GitHub enforces what the user is allowed to access.
- Later calls reuse the stored token without prompting the user again.

## Why the portal and the gateway see the same user

AgentCore Identity identifies a user by the token's issuer and `sub` claim. In Entra, `sub` is pairwise: the same person gets a different value for each application. Kiro and the portal both get their tokens from the same app registration for the same `api://<app-id>/mcp.invoke` scope, so `sub` is identical in both. That's why the gateway finds the consent the user gave in the portal. If the portal requested a token for a different app or API, the user would look like a different person, and the gateway would keep asking for consent.

## Security properties

- **Single entry point.** The gateway is the only way in, and it needs a valid Entra token for this app. The MCP server can't be reached from the internet: it has no public DNS name, sits behind an internal ALB and is locked down by the security-group chain.
- **Per-user GitHub tokens.** Tokens are stored in AgentCore Identity's token vault. They never reach Kiro, and the MCP server only sees a token for the duration of one request.
- **Session binding.** Consent is always bound to the person who is signed in. Anyone who opens the portal link signs in as themselves, so no one can attach their GitHub account to someone else's identity.
- **Client secrets.** The GitHub OAuth App secret and the Entra client secret are in Secrets Manager. Only AgentCore Identity and the portal's execution role can read them, and that role's trust policy is pinned to this one portal.
- **Audit.** CloudTrail records portal sign-ins, token fetches and session binding. Gateway application logs go to CloudWatch.

## Configuration checklist

### Entra app registration

- Under Expose an API, set the identifier `api://<app-id>`, add the scope `mcp.invoke`, and set the access token version to 2.
- Add redirect URIs:
  - Mobile and desktop: the localhost URL that `mcp-remote` uses (port 7788 in the config below).
  - Web: `<portal-url>/callback`.
- Create a client secret for the portal and store it in Secrets Manager.

### GitHub OAuth App

- Set the callback URL to the GitHub credential provider's AgentCore Identity callback, `https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<id>`.

### AgentCore

- **Gateway:** MCP protocol version 2025-11-25. Use the `CUSTOM_JWT` authorizer with the Entra v2 discovery URL and allowed audience `<app-id>` (the bare GUID, not `api://...`). Leave allowed scopes empty; see the first limit below.
- **Two OAuth2 credential providers (`CustomOauth2`):**
  - GitHub, for outbound calls.
  - Entra, for the portal's sign-in, using the v2 discovery URL.
- **Gateway target:**
  - an MCP server behind a private endpoint (VPC, subnets, security group);
  - inline tool schema;
  - OAuth with grant type `AUTHORIZATION_CODE`, scope `repo`, and default return URL `<portal-url>/connect/callback`.
- **Gateway workload identity:** add `<portal-url>/connect/callback` to its allowed return URLs. This is an API call; there's no CloudFormation property for it.
- **Consent portal:**
  - created via API, since there's no CloudFormation type yet;
  - IdP: the Entra provider, with scopes `openid api://<app-id>/mcp.invoke` and audience `<app-id>`;
  - source: this gateway.
- **Response interceptor Lambda:** rewrites the consent link to point at the portal.

### Kiro `mcp.json`

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": [
        "mcp-remote@latest",
        "https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp",
        "7788",
        "--disable-resource-parameter",
        "--static-oauth-client-info", "{\"client_id\":\"<app-id>\"}",
        "--static-oauth-client-metadata", "{\"scope\":\"openid profile offline_access api://<app-id>/mcp.invoke\"}"
      ]
    }
  }
}
```

## Limits and gotchas

- **The gateway can't enforce Entra scopes.** Entra needs the full scope URI in the sign-in request, but the token's `scp` claim contains only the short name. So setting allowed scopes on the gateway either rejects every token or breaks sign-in. The gateway checks the audience only. To control who can use it, turn on "Assignment required" on the Entra enterprise app.
- **Kiro's built-in remote-MCP sign-in doesn't work with Entra here.** It sends the RFC 8707 `resource` parameter, and Entra rejects it with `AADSTS9010010`. Running through `mcp-remote --disable-resource-parameter` avoids this. Pin an `mcp-remote` version for production.
- **One GitHub scope set per target.** Scopes can't vary per tool.
- **No way to revoke consent from the portal.** It has no Disconnect button.
- **Rotating the Entra client secret.** Update the value in Secrets Manager. The portal reads the secret each time a sign-in starts, so the new secret takes effect immediately.
