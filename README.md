# ZeppGPT

ZeppGPT is an unofficial, read-only MCP server for accessing workout data from
a Zepp account. It provides normalized workout summaries, sensor samples,
routes, laps, splits, pool lengths and sets, pauses, and rowing intervals.

The server only performs HTTP `GET` requests against approved Zepp or Huami
hosts. It does not modify workouts or other account data.

> [!WARNING]
> Zepp does not provide a supported public API for this integration. Endpoints,
> authentication, and payload formats may change. Use it only with your own
> account and review Zepp's terms before use.

## Features

- Five structured, read-only MCP tools.
- Stable workout IDs that include both Zepp `trackid` and `source`.
- Normalized running, walking, hiking, pool-swimming, and rowing data.
- Decoded route, heart-rate, distance, speed, gait, split, pause, and lap data.
- Bounded responses and coordinate-free queries when route data is unnecessary.
- Credential redaction and strict host allowlisting.
- Fully synthetic offline test fixtures.

## Requirements

- Python 3.11 or newer.
- [`uv`](https://docs.astral.sh/uv/) for installation and execution.
- A Zepp account and a valid `apptoken` from your own authenticated session.

## Install

```bash
git clone https://github.com/jcappaert/ZeppGPT.git
cd ZeppGPT
uv sync
```

## Configure Zepp authentication

ZeppGPT uses the same `apptoken` header and numeric user ID as the Zepp web or
mobile service. To find them, sign in to Zepp, open the browser developer tools,
and inspect an authenticated request to a host such as
`api-mifit-de2.zepp.com` or `api-mifit.huami.com`.

Copy the example environment file:

```bash
cp .env.example .env
```

Set these values in `.env`:

```dotenv
ZEPP_APP_TOKEN=your-token
ZEPP_USER_ID=your-numeric-user-id
ZEPP_API_HOST=https://your-regional-zepp-host
```

Never put credentials in command-line arguments, MCP configuration, issues, or
committed files. Tokens can expire and must then be replaced locally.

Verify the configuration without a network request:

```bash
uv run zeppgpt doctor
```

Optionally test read-only API access:

```bash
uv run zeppgpt probe --limit 10 --details 2
```

## Run the MCP server

Start a localhost-only Streamable HTTP server:

```bash
uv run zepp-mcp
```

The default endpoint is `http://127.0.0.1:8000/mcp`.

For an MCP client that launches local stdio servers:

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

The server reads credentials from the project `.env`; no token belongs in the
MCP client configuration. See [docs/MCP_SERVER.md](docs/MCP_SERVER.md) for
transport, tool, and security details.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `list_workouts` | List workouts with date and sport filters |
| `get_workout` | Get normalized metadata for one workout |
| `get_latest_workout` | Get the newest workout, optionally by sport |
| `get_workout_samples` | Get bounded and projected route or sensor samples |
| `get_workout_laps` | Get splits, lengths, sets, intervals, and pauses |

All tools advertise read-only, non-destructive, idempotent behavior.

## Local inspection

The development inspector helps investigate schema changes without altering MCP
tool responses:

```bash
uv run zeppgpt inspect-workout --track-id TRACK_ID --source SOURCE
```

`--include-raw` can expose sensitive health and location data. Use it only
locally and never attach its output to a public issue.

## Security and privacy

- `.env`, `.secrets/`, `diagnostics/`, `data/`, and common traffic-capture
  formats are ignored by Git.
- Tokens are only sent to HTTPS origins ending in `zepp.com` or `huami.com`.
- The HTTP server refuses unauthenticated non-loopback binds.
- Normal MCP subdivision output omits raw positional records.
- Diagnostic exports can contain workout and GPS data even after credentials
  are removed. Do not publish them.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and guidance on
keeping account data out of public reports.

## Development

Run the complete offline suite:

```bash
uv run python -m unittest discover -s tests -v
uv run --with ruff ruff check zeppgpt tests
```

The normalized model and timing behavior are documented in
[docs/NORMALIZED_MODEL.md](docs/NORMALIZED_MODEL.md).

## License

[MIT](LICENSE)
