from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def write_dashboard(destination: Path, summary: dict[str, Any]) -> None:
    """Persist bounded, self-contained dashboard artifacts for every attempt."""
    encoded = html.escape(json.dumps(summary, indent=2, default=str))
    metrics = summary.get("interactivity_and_throughput", {})
    rows = "".join(
        "<tr><th>"
        + html.escape(str(name))
        + "</th><td><code>"
        + html.escape(json.dumps(value, default=str, separators=(",", ":")))
        + "</code></td></tr>"
        for name, value in sorted(metrics.items())
    )
    document = (
        "<!doctype html><meta charset='utf-8'><title>AgentX benchmark dashboard</title>"
        "<style>body{font:14px system-ui;margin:2rem;max-width:1000px}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #bbb;padding:.5rem;text-align:left}"
        "pre{white-space:pre-wrap}</style><h1>AgentX benchmark dashboard</h1>"
        "<h2>Interactivity and throughput</h2><table>"
        + rows
        + "</table><h2>Run and monitoring metadata</h2><pre>"
        + encoded
        + "</pre>"
    )
    (destination / "dashboard.html").write_text(document, encoding="utf-8")
    (destination / "interactivity_vs_throughput.html").write_text(
        document.replace("benchmark dashboard", "interactivity and throughput"),
        encoding="utf-8",
    )


def write_sweep_dashboard(destination: Path, report: dict[str, Any]) -> None:
    """Write a bounded cross-concurrency chart without external dependencies."""
    points = []
    for item in report.get("per_concurrency", []):
        metrics = item.get("interactivity_and_throughput", {})
        throughput = _average(metrics.get("request_throughput"))
        latency = _average(
            metrics.get("inter_token_latency")
            or metrics.get("request_latency")
            or metrics.get("time_to_first_token")
        )
        points.append(
            {
                "concurrency": item.get("concurrency"),
                "status": item.get("status"),
                "throughput": throughput,
                "latency": latency,
                "metrics": metrics,
            }
        )
    encoded = json.dumps(points, default=str, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    rows = "".join(
        "<tr><td>"
        + html.escape(str(point["concurrency"]))
        + "</td><td>"
        + html.escape(str(point["status"]))
        + "</td><td>"
        + html.escape(str(point["throughput"]))
        + "</td><td>"
        + html.escape(str(point["latency"]))
        + "</td></tr>"
        for point in points
    )
    document = f"""<!doctype html><meta charset="utf-8">
<title>AgentX interactivity vs throughput</title>
<style>body{{font:14px system-ui;margin:2rem;max-width:1100px}}canvas{{border:1px solid #bbb}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #bbb;padding:.5rem;text-align:left}}</style>
<h1>AgentX interactivity vs throughput</h1>
<p>Each point is one requested concurrency. X is request throughput; Y is latency.</p>
<canvas id="chart" width="900" height="480"></canvas>
<table><thead><tr><th>Concurrency</th><th>Status</th><th>Request throughput</th><th>Latency</th></tr></thead>
<tbody>{rows}</tbody></table><script>
const points={encoded}.filter(p=>Number.isFinite(p.throughput)&&Number.isFinite(p.latency));
const c=document.getElementById('chart'),x=c.getContext('2d'),pad=55;
const xmax=Math.max(1,...points.map(p=>p.throughput)),ymax=Math.max(1,...points.map(p=>p.latency));
x.clearRect(0,0,c.width,c.height);x.strokeStyle='#555';x.beginPath();x.moveTo(pad,10);x.lineTo(pad,c.height-pad);x.lineTo(c.width-10,c.height-pad);x.stroke();
x.fillStyle='#111';x.fillText('latency',8,20);x.fillText('request throughput',c.width-130,c.height-15);
for(const p of points){{const px=pad+p.throughput/xmax*(c.width-pad-20),py=c.height-pad-p.latency/ymax*(c.height-pad-20);x.beginPath();x.fillStyle='#2563eb';x.arc(px,py,6,0,Math.PI*2);x.fill();x.fillStyle='#111';x.fillText('c'+p.concurrency,px+8,py-8);}}
</script>"""
    (destination / "interactivity_vs_throughput.html").write_text(
        document, encoding="utf-8"
    )


def _average(value: Any) -> float | None:
    candidate = value.get("avg") if isinstance(value, dict) else value
    if isinstance(candidate, bool):
        return None
    try:
        return float(candidate)
    except (TypeError, ValueError):
        return None
