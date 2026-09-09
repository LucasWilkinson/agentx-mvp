#!/usr/bin/env python3
"""Plot every completed B200 NVFP4 prefiller sweep point."""

from __future__ import annotations

import html
import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "results/.artifacts/reproductions/glm53-nvfp4-pr52705/20260903T000000Z-70a04c5142"
OUT = ROOT / "results/.artifacts/plots/b200-glm53-nvfp4-prefiller-pareto"
SERIES = {
    "PCP8 + EP8": ("nvfp4-pcp8-ep8", "#059669"),
    "DEP8 + EP8": ("nvfp4-dp8-ep8", "#d97706"),
    "TP8 + EP8": ("nvfp4-tp8-ep8", "#2563eb"),
}


def load(run_name: str) -> list[tuple[int, float, float]]:
    root = RUN / run_name / "points" / run_name
    points = []
    for path in sorted(root.glob("parallel_*/benchmark_summary.json")):
        data = json.loads(path.read_text())
        if data["Failed Requests"] or data["Success Requests"] != data["Total Requests"]:
            continue
        points.append(
            (
                int(data["Concurrency"]),
                1000.0 / float(data["TPOT (ms)"]),
                float(data["Total Throughput (tok/s)"]) / 16.0,
            )
        )
    return sorted(points)


def main() -> None:
    series = {
        name: (color, load(run_name))
        for name, (run_name, color) in SERIES.items()
        if load(run_name)
    }
    points = [point for _, values in series.values() for point in values]
    if not points:
        raise SystemExit("No complete NVFP4 points found")

    width, height = 1200, 790
    left, right, top, bottom = 105, 1130, 155, 650
    xs = [point[1] for point in points]
    ys = [point[2] for point in points]
    x_min = 5 * int((min(xs) - 5) // 5)
    x_max = 5 * int((max(xs) + 10) // 5)
    y_max = 1000 * (int(max(ys) // 1000) + 2)

    def sx(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * (right - left)

    def sy(value: float) -> float:
        return bottom - value / y_max * (bottom - top)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        "<style>text{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;fill:#172033}.grid{stroke:#dbe3ec}.axis{stroke:#64748b;stroke-width:1.5}.tick{font-size:13px;fill:#526174}.label{font-size:16px;font-weight:600}.title{font-size:27px;font-weight:750}.subtitle,.note{font-size:14px;fill:#64748b}.legend,.point{font-size:14px;font-weight:700}</style>",
        '<text x="105" y="42" class="title">GLM-5.3 NVFP4 on B200 — prefiller Pareto</text>',
        '<text x="105" y="68" class="subtitle">LMSYS OpenHands: 13 turns, long-context prefix growth, fixed 220-token outputs; vLLM PR #52705 DeepGEMM MegaMoE</text>',
        '<text x="1130" y="42" text-anchor="end" font-size="18" font-weight="700" fill="#059669">better ↗</text>',
    ]
    for index, (name, (color, _)) in enumerate(series.items()):
        x = 105 + index * 260
        out += [
            f'<line x1="{x}" y1="108" x2="{x + 34}" y2="108" stroke="{color}" stroke-width="4"/>',
            f'<circle cx="{x + 17}" cy="108" r="6" fill="white" stroke="{color}" stroke-width="3"/>',
            f'<text x="{x + 44}" y="113" class="legend">{html.escape(name)}</text>',
        ]
    for y in range(0, y_max + 1, 1000):
        py = sy(y)
        out += [
            f'<line x1="{left}" y1="{py:.1f}" x2="{right}" y2="{py:.1f}" class="grid"/>',
            f'<text x="{left - 12}" y="{py + 5:.1f}" text-anchor="end" class="tick">{y:,}</text>',
        ]
    for x in range(x_min, x_max + 1, 5):
        px = sx(x)
        out += [
            f'<line x1="{px:.1f}" y1="{top}" x2="{px:.1f}" y2="{bottom}" class="grid"/>',
            f'<text x="{px:.1f}" y="{bottom + 27}" text-anchor="middle" class="tick">{x}</text>',
        ]
    out += [
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" class="axis"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" class="axis"/>',
        f'<text x="{(left + right) / 2}" y="712" text-anchor="middle" class="label">Interactivity: 1000 / mean TPOT (output tok/s/user) →</text>',
        f'<text x="28" y="{(top + bottom) / 2}" text-anchor="middle" transform="rotate(-90 28 {(top + bottom) / 2})" class="label">Total-token throughput per GPU (tok/s/GPU) →</text>',
    ]
    for name, (color, values) in series.items():
        coords = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for _, x, y in values)
        out.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="3" stroke-dasharray="6 5" opacity=".65"/>')
        for concurrency, x, y in values:
            px, py = sx(x), sy(y)
            out += [
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7" fill="white" stroke="{color}" stroke-width="3"/>',
                f'<text x="{px + 10:.1f}" y="{py - 10:.1f}" class="point" fill="{color}">c{concurrency}</text>',
            ]
    out += [
        '<text x="105" y="755" class="note">Throughput includes prompt and output tokens, divided by all 16 serving GPUs (8 prefill + 8 decode). Upper-right is better.</text>',
        '<text x="105" y="778" class="note">Only complete, 100%-successful points are plotted. TP8 is added automatically as its points finish.</text>',
        "</svg>",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    svg = OUT.with_suffix(".svg")
    png = OUT.with_suffix(".png")
    svg.write_text("\n".join(out) + "\n")
    converter = shutil.which("rsvg-convert")
    if converter:
        subprocess.run([converter, "-w", "1800", "-h", "1185", "-o", png, svg], check=True)
    print(png if png.exists() else svg)


if __name__ == "__main__":
    main()
