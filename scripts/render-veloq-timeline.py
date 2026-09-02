#!/usr/bin/env python3
"""Render two VeloQ PyTorch timeline responses as a dependency-free SVG."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


WIDTH = 1200
HEIGHT = 650
LEFT = 92
RIGHT = 34
PANEL_HEIGHT = 205


def load_timeline(path: Path) -> tuple[list[dict], int]:
    payload = json.loads(path.read_text())
    rows = payload["data"]["rows"]
    interval_ns = payload["data"]["auxiliary"]["interval_ns"]
    return rows, interval_ns


def polyline(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def render_panel(
    label: str,
    rows: list[dict],
    interval_ns: int,
    top: int,
) -> str:
    plot_width = WIDTH - LEFT - RIGHT
    bottom = top + PANEL_HEIGHT
    bucket_seconds = interval_ns / 1e9
    duration_seconds = max(bucket_seconds, len(rows) * bucket_seconds)

    def x_at(index: float) -> float:
        return LEFT + plot_width * index / max(1, len(rows))

    def y_at(percent: float) -> float:
        return bottom - PANEL_HEIGHT * min(percent, 110.0) / 110.0

    gpu = [100.0 * row["gpu_ns"] / interval_ns for row in rows]
    comm = [100.0 * row["comm_ns"] / interval_ns for row in rows]
    gpu_points = [(x_at(i + 0.5), y_at(value)) for i, value in enumerate(gpu)]
    comm_points = [(x_at(i + 0.5), y_at(value)) for i, value in enumerate(comm)]
    gpu_area = [(x_at(0), bottom), *gpu_points, (x_at(len(rows)), bottom)]

    output = [
        f'<text x="{LEFT}" y="{top - 18}" class="panel-title">{html.escape(label)}</text>',
        f'<rect x="{LEFT}" y="{top}" width="{plot_width}" height="{PANEL_HEIGHT}" class="panel"/>',
    ]
    for tick in (0, 25, 50, 75, 100):
        y = y_at(tick)
        output.append(
            f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH - RIGHT}" y2="{y:.1f}" class="grid"/>'
        )
        output.append(
            f'<text x="{LEFT - 12}" y="{y + 5:.1f}" text-anchor="end" class="tick">{tick}%</text>'
        )

    tick_count = 6
    for index in range(tick_count):
        fraction = index / (tick_count - 1)
        x = LEFT + plot_width * fraction
        seconds = duration_seconds * fraction
        output.append(f'<line x1="{x:.1f}" y1="{bottom}" x2="{x:.1f}" y2="{bottom + 6}" class="axis"/>')
        output.append(
            f'<text x="{x:.1f}" y="{bottom + 24}" text-anchor="middle" class="tick">{seconds:.0f}s</text>'
        )

    output.extend(
        [
            f'<polygon points="{polyline(gpu_area)}" class="gpu-area"/>',
            f'<polyline points="{polyline(gpu_points)}" class="gpu-line"/>',
            f'<polyline points="{polyline(comm_points)}" class="comm-line"/>',
        ]
    )
    return "\n".join(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--c1", type=Path, required=True)
    parser.add_argument("--c8", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    c1_rows, c1_interval = load_timeline(args.c1)
    c8_rows, c8_interval = load_timeline(args.c8)
    panels = [
        render_panel("Concurrency 1 (rank 0)", c1_rows, c1_interval, 125),
        render_panel("Concurrency 8 (rank 0)", c8_rows, c8_interval, 400),
    ]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">
<title>GB200 PCP8+DCP8 VeloQ timeline</title>
<desc>Rank-0 CUDA kernel time and communication kernel time per one-second VeloQ bucket.</desc>
<style>
  text {{ font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #182230; }}
  .title {{ font-size: 25px; font-weight: 700; }}
  .subtitle {{ font-size: 14px; fill: #556070; }}
  .panel-title {{ font-size: 16px; font-weight: 650; }}
  .panel {{ fill: #fbfcfe; stroke: #cbd3df; }}
  .grid {{ stroke: #dfe5ed; stroke-width: 1; }}
  .axis {{ stroke: #7d8998; stroke-width: 1; }}
  .tick {{ font-size: 12px; fill: #667181; }}
  .gpu-area {{ fill: #4169e1; fill-opacity: 0.20; }}
  .gpu-line {{ fill: none; stroke: #3159cf; stroke-width: 2.2; }}
  .comm-line {{ fill: none; stroke: #e16a2d; stroke-width: 2.4; }}
  .legend {{ font-size: 13px; }}
</style>
<rect width="100%" height="100%" fill="white"/>
<text x="{LEFT}" y="42" class="title">GB200 GLM-5.3 PCP8+DCP8 prefill</text>
<text x="{LEFT}" y="67" class="subtitle">VeloQ v0.6.3 · one-second buckets · communication is included in total CUDA kernel time</text>
<line x1="{LEFT}" y1="91" x2="{LEFT + 28}" y2="91" class="gpu-line"/>
<text x="{LEFT + 38}" y="96" class="legend">CUDA kernel time</text>
<line x1="{LEFT + 190}" y1="91" x2="{LEFT + 218}" y2="91" class="comm-line"/>
<text x="{LEFT + 228}" y="96" class="legend">DCP collective kernel time</text>
{panels[0]}
{panels[1]}
<text x="24" y="350" transform="rotate(-90 24 350)" text-anchor="middle" class="subtitle">Time per 1-second bucket</text>
</svg>
'''
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(svg)


if __name__ == "__main__":
    main()
