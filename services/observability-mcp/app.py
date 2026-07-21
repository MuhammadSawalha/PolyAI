import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
load_dotenv()

from fastmcp import FastMCP

from s3 import download_gzip_json_lines, list_common_prefixes, list_log_objects

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

mcp = FastMCP("observability")


def _prometheus_url(environment: str) -> str:
    urls = {
        "dev": os.environ.get("DEV_PROMETHEUS_URL"),
        "prod": os.environ.get("PROD_PROMETHEUS_URL"),
    }
    url = urls.get(environment)
    if not url:
        raise ValueError(f"Unknown environment '{environment}', expected 'dev' or 'prod'")
    return url.rstrip("/")


# Matches an object key like logs/agent/2026/07/20/143005.gz - the fluent-bit
# s3_key_format is /logs/$TAG[1]/%Y/%m/%d/%H%M%S.gz, where $TAG[1] is the
# container_name captured by the rewrite_tag filter's rule (docker.* ->
# service.<container_name>), so the top-level path segment is a clean,
# deliberate container identifier - not an opaque token to substring-match.
_KEY_PATTERN = re.compile(r"logs/(?P<token>[^/]+)/(\d{4})/(\d{2})/(\d{2})/(?P<hms>\d{6})\.gz$")


def _key_timestamp(key: str) -> Optional[datetime]:
    m = _KEY_PATTERN.match(key)
    if not m:
        return None
    year, month, day_num = int(m.group(2)), int(m.group(3)), int(m.group(4))
    hms = m.group("hms")
    hour, minute, second = int(hms[0:2]), int(hms[2:4]), int(hms[4:6])
    return datetime(year, month, day_num, hour, minute, second, tzinfo=timezone.utc)


@mcp.tool()
def list_shipping_containers(environment: str) -> dict:
    """List the containers that have shipped logs to S3 for the given environment ('dev' or 'prod'). Returns {"containers": [str, ...]}."""
    prefixes = list_common_prefixes(environment, "logs/")
    containers = sorted(p.removeprefix("logs/").rstrip("/") for p in prefixes)
    return {"containers": containers}


@mcp.tool()
def get_container_logs(environment: str, container: str, minutes: int = 5) -> dict:
    """Fetch log lines shipped to S3 for a container in the given environment over the last `minutes` minutes. Returns {"records": [{"time", "stream", "log", "host"}, ...]}."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    records = []
    for key in list_log_objects(environment, f"logs/{container}/"):
        ts = _key_timestamp(key)
        if ts is None or not (start <= ts <= end + timedelta(minutes=1)):
            continue
        for record in download_gzip_json_lines(environment, key):
            records.append(record)
    records.sort(key=lambda r: r.get("time", ""))
    return {"records": records}


@mcp.tool()
def query_prometheus(environment: str, promql: str, minutes: int = 10) -> dict:
    """Run a PromQL range query against the given environment's ('dev' or 'prod') Prometheus over the last `minutes` minutes. Returns Prometheus's raw result data (a list of {metric, values} series)."""
    base_url = _prometheus_url(environment)
    end = time.time()
    start = end - minutes * 60
    step = max(15, int(minutes * 60 / 120))
    resp = httpx.get(
        f"{base_url}/api/v1/query_range",
        params={"query": promql, "start": start, "end": end, "step": step},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    return {"result": payload["data"]["result"]}


@mcp.tool()
def get_node_cpu_usage(environment: str, minutes: int = 10) -> dict:
    """Get overall CPU usage percentage (from node-exporter) for the given environment's EC2 instance over the last `minutes` minutes. Returns Prometheus's raw result data."""
    promql = '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
    return query_prometheus(environment, promql, minutes)


if __name__ == "__main__":  # pragma: no cover
    mcp.run(transport="stdio")
