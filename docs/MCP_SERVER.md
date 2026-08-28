# MCP server

ZeppGPT exposes normalized workout data to MCP clients without giving the
client direct access to the Zepp credential. The configured token is used only
for server-side HTTPS requests to allowlisted Zepp and Huami origins.

## Install and verify

From the repository directory:

```bash
uv sync
uv run zeppgpt doctor
```

Credentials are loaded from the ignored `.env` file described in the main
README.

## Streamable HTTP

```bash
uv run zepp-mcp
```

The default endpoint is `http://127.0.0.1:8000/mcp`. Choose another local port
with `--port`:

```bash
uv run zepp-mcp --port 8765
```

The HTTP transport does not implement client-facing authentication, so the
server binds to loopback by default and rejects non-loopback hosts. Do not
expose it directly to a LAN or the public internet. Use stdio or an
authenticated tunnel/proxy supplied by your MCP client when remote access is
needed.

## Stdio

MCP clients that manage local processes can launch the server directly:

```json
{
  "mcpServers": {
    "zepp": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/ZeppGPT",
        "run",
        "zepp-mcp",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

The child process reads `.env` from the project directory. Do not copy the Zepp
token into this JSON.

## Tools and limits

| Tool | Purpose | Limit |
| --- | --- | --- |
| `list_workouts` | List recent workouts with optional date and sport filters | 100 workouts |
| `get_workout` | Return normalized workout metadata | 250,000 serialized characters before raw metadata is omitted |
| `get_latest_workout` | Return the newest workout, optionally filtered by sport | Same as `get_workout` |
| `get_workout_samples` | Return filtered and evenly downsampled sensor or route points | 500 points |
| `get_workout_laps` | Return semantic splits, lengths, sets, intervals, and pauses | 200 records |

Every tool is annotated as read-only, non-destructive, idempotent, and
closed-world. Tool output includes structured content and an SDK-generated text
representation.

Raw 15- and 70-column positional records are not returned by normal MCP calls.
They are available only through the local `zeppgpt inspect-workout
--include-raw` workflow.

## Test with MCP Inspector

Start the HTTP server and list its tools:

```bash
npx -y @modelcontextprotocol/inspector@latest --cli \
  http://127.0.0.1:8000/mcp --method tools/list --format json
```

For the Inspector web UI, run:

```bash
npx -y @modelcontextprotocol/inspector@latest
```

Connect to `http://127.0.0.1:8000/mcp` using Streamable HTTP. Call
`list_workouts` first, then use a returned `workout_id` with the other tools.

## Error behavior

Authentication, rate-limit, network, missing-workout, input, and schema errors
become concise MCP tool errors. Upstream response bodies and credentials are
not reflected into tool results. Unexpected exceptions are converted by the
MCP SDK to generic failures.

## Limitations

- Zepp authentication uses an unofficial `apptoken` flow and has no automatic
  refresh.
- Lookup for an uncached workout ID is bounded to 500 workouts and 10 pages.
- Unsupported sport IDs remain `unknown` while preserving their numeric ID.
- Unrecognized detail fields remain bounded under `raw_metadata`.
- Some aggregate Zepp timing fields cannot be mapped safely to wall-clock
  offsets and are intentionally returned as null.
