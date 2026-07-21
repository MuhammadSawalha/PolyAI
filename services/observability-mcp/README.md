# observability-mcp

A local MCP server (stdio transport) exposing tools to query container logs (shipped to S3 by
Fluent Bit, see `../../fluent-bit.conf`) and metrics (from the old EC2 hosts' Prometheus) for
the `dev` and `prod` environments, directly from VS Code Copilot Chat.

Unlike the other `services/*` MCP/app servers in this repo, this one is **not containerized or
deployed** - `.vscode/mcp.json` launches it as a local subprocess (`python app.py`) on your own
machine, so it needs your local Python environment and AWS credentials (not an EC2 instance
profile) to reach S3.

## Setup

```bash
pip install -r requirements.txt
```

Make sure your local AWS credentials (`aws configure`, or `~/.aws/credentials`) can read
`sawalha-polyai-logs-dev` / `sawalha-polyai-logs-prod`.

`.vscode/mcp.json` (repo root) already registers this server with Copilot Chat with the right
env vars - see `.env.example` here for what each one means. Reload VS Code / reopen Copilot Chat
in agent mode after the server or its env vars change.

## Tools

- `list_shipping_containers(environment, days=1)` - which containers have shipped logs recently.
- `get_container_logs(environment, container, minutes=5)` - fetch recent log lines for a container.
- `query_prometheus(environment, promql, minutes=10)` - run an arbitrary PromQL range query.
- `get_node_cpu_usage(environment, minutes=10)` - convenience wrapper for host CPU usage.
