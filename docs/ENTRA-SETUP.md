# Entra ID setup (inbound authorizer) — SINGLE-APP design

The Gateway's inbound gate is an Entra `customJWTAuthorizer`. You are using the
**single-app** variant: **one** app registration acts as BOTH the audience (the
API the Gateway validates tokens for) AND the Kiro client. Microsoft documents
this for a strict 1:1 client↔API pairing ("Use of a single application" in the
OBO flow doc). It is fine for a PoC; the two-app split is the future-proof choice
once a second client appears. With one app, the token's
`aud` and `azp` are the **same GUID**, so the `azp` check is redundant.

An Entra **app registration is an identity record** (client ID, permissions,
whether it holds a secret) — not software. The code that authenticates *as* it
runs elsewhere (Kiro on the laptop).

## Concrete values (this deployment)

Replace the placeholders below with the values from your own Entra tenant.

| | Value |
|---|---|
| Tenant ID | `<YOUR_TENANT_ID>` |
| App (client) ID — `ENTRA_API_APP_ID` **and** Kiro client | `<YOUR_APP_ID>` |
| Application ID URI | `api://<YOUR_APP_ID>` |
| Inbound scope | `api://<YOUR_APP_ID>/mcp.invoke` |
| Discovery URL | `https://login.microsoftonline.com/<YOUR_TENANT_ID>/v2.0/.well-known/openid-configuration` |

## Configure the single app registration

On app `<YOUR_APP_ID>`:

1. **Expose an API → Application ID URI:** set to
   `api://<YOUR_APP_ID>` (accept default).
2. **Expose an API → Add a scope:**
   - Scope name: `mcp.invoke`
   - Who can consent: Admins and users
   - State: Enabled
3. **Authentication:**
   - Add a **Mobile and desktop / public client** redirect URI:
     `http://localhost:7788/oauth/callback` (matches the mcp-remote port).
   - **Allow public client flows: Yes** (Kiro is a public client, auth-code + PKCE).
   - After the first deploy, add a **Web** redirect URI for the consent portal's
     sign-in: `<portal-url>/callback`, exactly, with no trailing slash
     (`PORTAL_CALLBACK_URL` in `.env`). Both redirect lists coexist on the app.
4. **Expose an API → Authorized client applications → Add client application:**
   add the app's **own** client ID (`<YOUR_APP_ID>`) and tick `mcp.invoke`. In the
   single-app design the client and the API are the same object, so it
   pre-authorizes itself and `mcp-remote`'s `prompt=consent` becomes a no-op.
5. **API permissions → Add a permission → My APIs → (this same app) →
   Delegated → `mcp.invoke` → Add → Grant admin consent.**
6. **Manifest:** `"requestedAccessTokenVersion": 2` (v2 tokens: `aud` = app GUID,
   issuer = the v2 endpoint the gateway and portal validate against).
7. **Certificates & secrets → New client secret** for the consent portal, which
   signs users in server-side as this app. Store it in Secrets Manager as JSON:

   ```bash
   aws secretsmanager create-secret --name github-mcp/entra-portal-client-secret \
     --secret-string '{"client_secret":"<value>"}' --region <region>
   ```

   The ARN goes into `ENTRA_PORTAL_CLIENT_SECRET_ARN`. To rotate it, put a new
   value into the same secret; the portal reads it at every sign-in start.

## Tenant-wide admin consent (if Assignment required = Yes)

```
https://login.microsoftonline.com/<YOUR_TENANT_ID>/adminconsent?client_id=<YOUR_APP_ID>
```

## Known Entra + Gateway workaround (already in the Kiro config)

Entra rejects the RFC 8707 `resource` param (AADSTS9010010) and couples scope
advertisement + enforcement. Connect Kiro via the `mcp-remote` stdio proxy with
`--disable-resource-parameter` and static client info/metadata — already set in
`../kiro/mcp.json.example`. Do NOT try to fix AADSTS9010010 by editing the Gateway
or Kiro native config; only the proxy path works. Clear `~/.mcp-auth` between
attempts.

## Values handed to `scripts/deploy.sh` (via `.env`)

```bash
export ENTRA_TENANT_ID=<YOUR_TENANT_ID>
export ENTRA_API_APP_ID=<YOUR_APP_ID>
export ENTRA_PORTAL_CLIENT_SECRET_ARN=arn:aws:secretsmanager:<region>:<account>:secret:github-mcp/entra-portal-client-secret-XXXXXX
```
