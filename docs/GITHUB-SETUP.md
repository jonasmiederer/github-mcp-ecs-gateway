# GitHub OAuth App setup (downstream / outbound)

The gateway obtains a **per-user GitHub token** with the Authorization Code flow
(3LO). AgentCore Identity is the OAuth client to GitHub, so it needs a GitHub
**OAuth App** (confidential: client ID + client secret, no PKCE requirement).

## 1. Register the OAuth App

GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App**:

- Application name: anything users will recognise on GitHub's authorize page.
- Homepage URL: anything (e.g. `https://example.com`).
- **Authorization callback URL:** the callback of the GitHub credential provider
  in AgentCore Identity. It only exists after the first deploy, so put a
  placeholder first and replace it afterwards with the provider's `callbackUrl`:

  ```bash
  aws bedrock-agentcore-control get-oauth2-credential-provider \
    --name github-mcp-provider --query callbackUrl --output text --region <region>
  # https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<id>
  ```

  An OAuth App accepts several callback URLs, so one app can serve more than one
  provider.

Record the **Client ID** → `GITHUB_CLIENT_ID`. Generate a **client secret**.

## 2. Store the secret in Secrets Manager (never in the template)

The credential provider reads it with `JsonKey: client_secret`, so store JSON:

```bash
aws secretsmanager create-secret \
  --name github-mcp/oauth-client-secret \
  --secret-string '{"client_secret":"<the-github-client-secret>"}' \
  --region <region>
```

Record the ARN → `GITHUB_CLIENT_SECRET_ARN`. The `OAuth2CredentialProvider`
references it with `ClientSecretSource: EXTERNAL`.

## 3. Scope

One scope set for the whole gateway target (`GITHUB_SCOPE`, default
`repo`), used by all three tools (`get_authenticated_user`, `list_repositories`,
`get_repository`). The gateway can't vary GitHub scopes per tool.
