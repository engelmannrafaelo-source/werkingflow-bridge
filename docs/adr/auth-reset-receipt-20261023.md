# Password-reset completion identity

After consuming a valid password-reset token and committing the new credential,
`POST /v1/auth/reset-password-with-token` returns the user's UUID in
`X-Reset-User-ID`. Status 204 and the empty body are unchanged. Invalid tokens
still receive the same generic 400 and no identity header. Responses are no-store.
The token row is locked while consuming it, so concurrent requests cannot both
consume the same reset token.

App servers use this trusted response identity to invalidate independently minted
app JWTs; deleting Bridge sessions alone cannot revoke those JWTs. Deploy this
backward-compatible Bridge change before app clients require the identity header.
This receipt does not itself broadcast revocation to other products.
