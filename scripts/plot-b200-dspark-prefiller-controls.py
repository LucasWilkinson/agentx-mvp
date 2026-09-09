#!/usr/bin/env python3
"""Plot B200 DSpark prefiller controls from EvalScope summary JSON files."""

from __future__ import annotations

import argparse
import csv
import html
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Point:
    concurrency: int
    interactivity: float
    throughput_per_gpu: float
    decoded_per_iter: float
    valid: bool
    source: Path


def load_points(root: Path) -> tuple[list[Point], list[Point]]:
    by_concurrency: dict[int, Point] = {}
    invalid: dict[int, Point] = {}
    for path in sorted(root.glob("**/benchmark_summary.json")):
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if float(value.get("TPOT (ms)", 0.0)) <= 0:
            continue
        point = Point(
            concurrency=int(value["Concurrency"]),
            interactivity=1000.0 / float(value["TPOT (ms)"]),
            throughput_per_gpu=float(value["Total Throughput (tok/s)"]) / 16.0,
            decoded_per_iter=float(value.get("Decoded Tok/Iter", 0.0)),
            valid=(
                int(value["Failed Requests"]) == 0
                and int(value["Success Requests"]) == int(value["Total Requests"])
                and float(value["Avg Output Tokens"]) > 0
            ),
            source=path,
        )
        target = by_concurrency if point.valid else invalid
        target[point.concurrency] = point
    return (
        sorted(by_concurrency.values(), key=lambda p: p.concurrency),
        sorted(invalid.values(), key=lambda p: p.concurrency),
    )


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render(series: list[dict[str, object]], invalid: list[Point]) -> str:
    width, height = 1320, 820
    left, right, top, bottom = 110, 1275, 175, 680
    all_points = [p for item in series for p in item["points"]]
    x_values = [p.interactivity for p in all_points]
    y_values = [p.throughput_per_gpu for p in all_points]
    x_min, x_max = min(x_values) - 15, max(x_values) + 15
    y_min, y_max = 0, ((int(max(y_values)) // 2000) + 2) * 2000

    def sx(v: float) -> float:
        return left + (v - x_min) / (x_max - x_min) * (right - left)

    def sy(v: float) -> float:
        return bottom - (v - y_min) / (y_max - y_min) * (bottom - top)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<style>text{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;fill:#172033}.grid{stroke:#dbe3ec;stroke-width:1}.axis{stroke:#64748b;stroke-width:1.5}.tick{font-size:13px;fill:#526174}.label{font-size:16px;font-weight:650}.point{font-size:12px;font-weight:700}.title{font-size:27px;font-weight:750}.subtitle{font-size:14px;fill:#64748b}.legend{font-size:14px;font-weight:650}.note{font-size:12px;fill:#64748b}</style>',
        '<text x="110" y="40" class="title">GLM-5.3 on B200 — DSpark7 adaptive prefiller Pareto</text>',
        '<text x="110" y="66" class="subtitle">LMSYS OpenHands cold-cache workload · 512 GiB native KV offload per role · vLLM 56c8609f68</text>',
        '<text x="1275" y="40" text-anchor="end" font-size="18" font-weight="700" fill="#059669">better ↗</text>',
    ]
    legend_positions = [(110, 105), (415, 105), (735, 105), (110, 135)]
    for item, (x, y) in zip(series, legend_positions):
        color = item["color"]
        out += [
            f'<line x1="{x}" y1="{y}" x2="{x+32}" y2="{y}" stroke="{color}" stroke-width="4"/>',
            f'<circle cx="{x+16}" cy="{y}" r="6" fill="white" stroke="{color}" stroke-width="3"/>',
            f'<text x="{x+42}" y="{y+5}" class="legend">{esc(item["name"])}</text>',
        ]
    for y in range(0, y_max + 1, 2000):
        py = sy(y)
        out += [
            f'<line x1="{left}" y1="{py:.1f}" x2="{right}" y2="{py:.1f}" class="grid"/>',
            f'<text x="{left-12}" y="{py+5:.1f}" text-anchor="end" class="tick">{y:,}</text>',
        ]
    x_tick = ((int(x_min) + 24) // 25) * 25
    while x_tick <= x_max:
        px = sx(x_tick)
        out += [
            f'<line x1="{px:.1f}" y1="{top}" x2="{px:.1f}" y2="{bottom}" class="grid"/>',
            f'<text x="{px:.1f}" y="{bottom+28}" text-anchor="middle" class="tick">{x_tick}</text>',
        ]
        x_tick += 25
    out += [
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" class="axis"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" class="axis"/>',
        f'<text x="{(left+right)/2:.1f}" y="{bottom+64}" text-anchor="middle" class="label">Interactivity: 1000 / mean TPOT (output tok/s/user) →</text>',
        f'<text x="28" y="{(top+bottom)/2:.1f}" text-anchor="middle" transform="rotate(-90 28 {(top+bottom)/2:.1f})" class="label">Total-token throughput per GPU (tok/s/GPU) →</text>',
    ]
    offsets = [(-8, -10), (8, 18), (8, -10), (8, -10)]
    for item, (dx, dy) in zip(series, offsets):
        color = item["color"]
        points = item["points"]
        coords = " ".join(f"{sx(p.interactivity):.1f},{sy(p.throughput_per_gpu):.1f}" for p in points)
        out.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2" stroke-dasharray="5 4" opacity="0.65"/>')
        for p in points:
            px, py = sx(p.interactivity), sy(p.throughput_per_gpu)
            out += [
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7" fill="white" stroke="{color}" stroke-width="3"/>',
                f'<text x="{px+dx:.1f}" y="{py+dy:.1f}" text-anchor="{"end" if dx < 0 else "start"}" class="point" fill="{color}">c{p.concurrency}</text>',
            ]
    out += [
        '<text x="110" y="780" class="note">Throughput includes prompt and output tokens divided by all 16 serving GPUs. DEP8 uses an 8K prefill batch; other arms use 32K.</text>',
        '<text x="110" y="802" class="note">TEP8 is topology-matched to a TP8 decoder. DEP8 c16 is excluded because both serving spot nodes were evicted during the attempt.</text>',
        '</svg>',
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pcp-root", type=Path, required=True)
    parser.add_argument("--controls-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    specs = [
        ("PCP8 → TP8 decoder", "#2563eb", args.pcp_root / "tp8/cold-points"),
        ("PCP8 → DP8 decoder", "#7c3aed", args.pcp_root / "dp8/cold-points"),
        ("DEP8 → DP8 decoder (8K batch)", "#d97706", args.controls_root / "dp8-prefiller/cold-points"),
        ("TEP8 → TP8 decoder", "#059669", args.controls_root / "tp8-prefiller/cold-points"),
    ]
    series = []
    all_invalid = []
    for name, color, root in specs:
        points, invalid = load_points(root)
        series.append({"name": name, "color": color, "points": points})
        if name.startswith("DEP8"):
            all_invalid.extend(p for p in invalid if p.concurrency == 16)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    svg = args.output_dir / "pareto.svg"
    png = args.output_dir / "pareto.png"
    metrics = args.output_dir / "metrics.csv"
    svg.write_text(render(series, all_invalid))
    with metrics.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["series", "concurrency", "interactivity_tok_s_user", "total_tok_s_gpu", "decoded_tok_iter", "valid", "source"])
        for item in series:
            for p in item["points"]:
                writer.writerow([item["name"], p.concurrency, p.interactivity, p.throughput_per_gpu, p.decoded_per_iter, True, p.source])
        for p in all_invalid:
            writer.writerow(["DEP8 → DP8 decoder (8K batch)", p.concurrency, p.interactivity, p.throughput_per_gpu, p.decoded_per_iter, False, p.source])
    if converter := shutil.which("rsvg-convert"):
        subprocess.run([converter, "-w", "1980", "-h", "1230", "-o", png, svg], check=True)
    print(png if png.exists() else svg)


if __name__ == "__main__":
    main()
