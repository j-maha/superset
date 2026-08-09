---
name: bigquery-oauth-impersonation-review
description: Review Superset BigQuery OAuth impersonation changes for identity propagation, credential safety, and alternate execution paths.
---

# BigQuery OAuth impersonation review guidance

Source: PR #3 (`feat(bigquery): add OAuth2 user impersonation support`).

## Audit every execution boundary

A per-user identity model is incomplete if it only covers the primary query path. Review every alternate path that can create a client or execute a query:

- SQL Lab synchronous and Celery execution
- scheduled reports and background tasks
- cache-key generation and cache warmup, including WebDriver strategies
- catalog/schema permission synchronization
- DataFrame/file uploads
- connection testing and OAuth redirect handling

Each path must either propagate the effective user and credentials or explicitly skip/restrict the operation. User-scoped impersonated results may remain cacheable, but they must not be treated as shared-cache results.

## Keep credentials out of connection identity

Do not put access tokens in SQLAlchemy URLs, engine representations, cache keys, or logs. Prefer a closure or connection argument that supplies the token to a private engine. Private per-user engines must bypass the process-wide engine cache and be disposed at context exit.

## Use typed authentication failures

Do not detect an authentication condition by comparing `str(exception)`. Raise a dedicated exception from the dialect and detect it with `isinstance` in the engine spec. This keeps connection-test handling independent of driver wording and makes regressions explicit.

## Respect client and token lifecycles

Keep the active BigQuery client/engine context open until operations such as `pandas_gbq.to_gbq()` have submitted their work. Reuse the engine spec's client factory rather than duplicating credential construction. Resolve the current OAuth access token before engine creation; the shared OAuth utility refreshes expired stored tokens when a refresh token is available.

## Test feature boundaries, not just helpers

Regression tests should verify:

- impersonated dashboards are excluded from every shared cache-warmup strategy;
- impersonated uploads pass the user BigQuery client to pandas-gbq;
- missing impersonation tokens raise the typed authentication exception;
- saved and unsaved connection tests take the correct OAuth path;
- asynchronous execution retains the persisted query user's identity;
- cache keys differ between impersonated users.

## Review clarity matters

Name flags after the policy they enable (for example, `allow_unsaved_oauth2`), explain intentional nested error handling, and use the shared error-type enum in frontend code instead of repeating string literals. User-facing OAuth messages should describe authorization generally rather than implying that it can happen only in SQL Lab.

## Avoid unsafe prototype shortcuts

Do not copy approaches that put tokens in URLs, switch the default BigQuery driver globally, add unused dependencies, use legacy token endpoints, or leave debug output in production code. An opt-in dialect extension with token-free transport is safer until equivalent support exists upstream.
