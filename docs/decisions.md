# Decisions

## 2026-11-26 CLI device sessions

The CLI keeps the credential issued by the device grant in the user's private configuration file. The console
exchanges that credential for API JWTs. Each SDK client keeps the JWT in memory and renews it against a monotonic
clock. The stored API URL and the HTTP client's same-origin check constrain where the access token can be sent.
The project header is sent only with a CLI JWT. Explicit keys and environment keys retain precedence.

Local credential edits are serialized and replacements are atomic. Signing in again saves the replacement with the old credential marked for revocation, then revokes the old
session. The next CLI command can finish that revocation if interrupted. Signing out uses the console first, retaining the local credential if the request fails and can be retried.
An API key would outlive the login and could not be revoked from the device list. The console and API must both
support the device exchange before users can sign in with it.
