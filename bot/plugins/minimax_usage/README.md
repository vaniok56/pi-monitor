# minimax_usage

Shows MiniMax Coding Plan token usage: current-window and weekly remaining %,
reset countdowns, per usage bucket (`general`, `video`, ...).

Hits `platform.minimax.io`'s console API directly — the public/documented
MiniMax API does not expose Coding Plan quota. This is the same endpoint the
platform web console itself calls.

## Auth — session cookie, not an API key

This endpoint authenticates via browser session cookie + group id, **not** a
regular MiniMax API key. There is no known stable-key alternative for it.

To get the values:
1. Log into https://platform.minimax.io in a browser.
2. Open DevTools → Network tab → visit `console/usage`.
3. Find any XHR/fetch request to `platform.minimax.io`.
4. From its request headers, copy:
   - `Cookie` header → the `_token=<value>` part (a JWT — do not include the
     rest of the cookie string, only `_token`'s value)
   - `X-Group-Id` header value (this one is stable, doesn't expire)

Store them:
```bash
secrets-cli put MINIMAX_SESSION_TOKEN   # paste the _token value
secrets-cli put MINIMAX_GROUP_ID        # paste the X-Group-Id value
```
Then materialize into `.env` (`MINIMAX_SESSION_TOKEN=...` / `MINIMAX_GROUP_ID=...`)
and `docker compose up -d --build pi-control-bot`.

## The cookie expires

The `_token` JWT has a real expiry (observed ~2-3 weeks). When it lapses, the
button will show "⚠️ Session expired" with the refresh instructions inline —
repeat the steps above to get a fresh `_token` and re-run `secrets-cli put`
+ redeploy. `MINIMAX_GROUP_ID` does not need refreshing.

## Config

```yaml
minimax_usage:
  session_token_env: "MINIMAX_SESSION_TOKEN"
  group_id_env: "MINIMAX_GROUP_ID"
  timeout: 15
```

## Button

`🤖 MiniMax Usage` — window + weekly remaining %, reset countdowns, per bucket.
