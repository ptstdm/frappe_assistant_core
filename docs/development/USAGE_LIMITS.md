# Usage Limits

Per-user limits on MCP tool calls, so that one user, or a client stuck in a retry loop, cannot
tie up the web workers of a server that other sites share. They apply to every MCP client:
Claude, ChatGPT, and FAC Chat (which reaches the site over the same endpoint).

## Settings

**Assistant Core Settings → Security → Usage Limits**. The whole section is off until
**Enable Usage Limits** is ticked.

| Field | Default | What it limits |
|---|---|---|
| Usage Limit Mode | Block | **Block** refuses the call. **Log only** lets it run and only records it. |
| Max Calls per User per Day | 3000 | tool calls a user starts per day, site time |
| Max Calls per User per Minute | 60 | bursts |
| Max Tool Seconds per User per Day | 7200 | total time the user's tool calls ran today |
| Max Concurrent Calls per User | 6 | the user's calls running at the same moment |
| Max Concurrent Calls per Site | 12 | all users' calls running at the same moment |

A limit left at 0 or blank uses its default. This is the same rule as the code execution
limits. Single DocType fields read back as 0 on sites that already had the settings row, so 0
cannot mean "no limit". Saving the form writes the default into a blank field, so the form
shows the limit that applies.

Administrator is never limited.

## How a call is checked

`mcp/server.py` → `_handle_tools_call` calls `utils/usage_limits.before_call()` once the tool
is known to exist, so a mistyped tool name costs nothing. `after_call()` runs in the `finally`.

- **Counting.** One Redis pipeline per call increments the day, minute, per-user in-flight and
  per-site in-flight counters, and reads the day's tool seconds.
  - Keys are made with `frappe.cache.make_key`, so each site counts on its own.
  - The in-flight keys expire after 300 s. A worker killed mid-call therefore cannot hold a
    slot for ever.
- **Refusing.** A refused call gives its in-flight slots back at once. The client gets an MCP
  error that says which limit was hit and when it resets.
- **Fails open.** If Redis is unreachable, or the request has no Frappe session, the call runs.
  The limiter only logs a warning to the `usage_limits` logger.
- **Attempts count.** Counters count attempts, refused ones included, so a client that keeps
  retrying stays refused.

## What is recorded

- Each limit hit writes an **Assistant Audit Log** row with status **Rate Limited**.
  - `error_type` names the limit: `calls_per_day`, `calls_per_minute`, `tool_seconds_per_day`,
    `concurrent_per_user` or `concurrent_per_site`.
  - In Log only mode the message starts "Logged only, the call ran".
- At most one row is written per user and limit per minute. A retry loop therefore adds no
  database writes.
- Calls that ran keep their usual Success / Error / Timeout / Permission Denied rows. Usage
  meters can count calls that ran as rows whose status is not Rate Limited.

## Tests

`frappe_assistant_core/tests/test_usage_limits.py` drives `_handle_tools_call` as a client
call would.
- `usage_limits.get_limits` is patched with `autospec`, so no test changes the site's settings.
- `usage_limits.SITE` gets a per-test name, so the site's real in-flight count is never touched.
