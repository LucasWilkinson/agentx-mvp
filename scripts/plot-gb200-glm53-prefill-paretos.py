#!/usr/bin/env python3
"""Plot the clean GB200 GLM-5.3 prefiller Pareto comparison."""

from __future__ import annotations

import html
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results/.artifacts/lmsys-glm-agentic"
OUTPUTS = ROOT / "results/.artifacts/plots/gb200-glm53-prefill-pareto"
SVG_OUT = OUTPUTS.with_suffix(".svg")
PNG_OUT = OUTPUTS.with_suffix(".png")
HTML_OUT = OUTPUTS.with_suffix(".html")


@dataclass(frozen=True)
class Point:
    concurrency: int
    interactivity: float
    throughput_per_chip: float


SERIES = {
    "PCP8 + EP8 (64K batch)": {
        "color": "#059669",
        "directories": {
            1: ("pcp8-ep8-mtp3-graph-mbt65536-no-weight-offload-incluster-r6", "parallel_1_number_4"),
            2: ("pcp8-ep8-mtp3-graph-mbt65536-no-weight-offload-incluster-r6", "parallel_2_number_8"),
            4: ("pcp8-ep8-mtp3-graph-mbt65536-no-weight-offload-incluster-r6", "parallel_4_number_8"),
            8: ("pcp8-ep8-mtp3-graph-mbt65536-no-weight-offload-incluster-r6", "parallel_8_number_16"),
        },
    },
    "PCP8 + DCP8 + EP8 (64K, A2A)": {
        "color": "#d97706",
        "directories": {
            1: ("pcp8-dcp8-ep8-a2a-mtp3-graph-mbt65536-no-weight-offload-incluster-r7", "parallel_1_number_4"),
            2: ("pcp8-dcp8-ep8-a2a-mtp3-graph-mbt65536-no-weight-offload-incluster-r7", "parallel_2_number_8"),
            4: ("pcp8-dcp8-ep8-a2a-mtp3-graph-mbt65536-no-weight-offload-incluster-r7", "parallel_4_number_8"),
            8: ("pcp8-dcp8-ep8-a2a-mtp3-graph-mbt65536-no-weight-offload-incluster-r7", "parallel_8_number_16"),
        },
    },
    "TP8 + EP8": {
        "color": "#2563eb",
        "directories": {
            1: ("tp8-ep8-mtp3-graph-no-weight-offload-incluster-r5", "parallel_1_number_4"),
            2: ("tp8-ep8-mtp3-graph-no-weight-offload-incluster-r5", "parallel_2_number_8"),
            4: ("tp8-ep8-mtp3-graph-no-weight-offload-incluster-r5", "parallel_4_number_8"),
            8: ("tp8-ep8-mtp3-graph-no-weight-offload-incluster-r5", "parallel_8_number_16"),
        },
    },
}


def load_point(concurrency: int, run: str, point_dir: str) -> Point:
    directory = RESULTS / run / point_dir
    summary = json.loads((directory / "benchmark_summary.json").read_text())
    if summary["Failed Requests"] or summary["Success Requests"] != summary["Total Requests"]:
        raise RuntimeError(f"Incomplete benchmark data in {directory}")
    return Point(
        concurrency=concurrency,
        interactivity=1000.0 / float(summary["TPOT (ms)"]),
        throughput_per_chip=float(summary["Total Throughput (tok/s)"]) / 16.0,
    )


def pareto(points: list[Point]) -> list[Point]:
    result = []
    for point in points:
        dominated = any(
            other.interactivity >= point.interactivity
            and other.throughput_per_chip >= point.throughput_per_chip
            and (
                other.interactivity > point.interactivity
                or other.throughput_per_chip > point.throughput_per_chip
            )
            for other in points
            if other is not point
        )
        if not dominated:
            result.append(point)
    return sorted(result, key=lambda p: p.interactivity)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render(series: dict[str, dict[str, object]]) -> str:
    width, height = 1200, 790
    left, right, top, bottom = 105, 1130, 155, 650
    all_points = [point for config in series.values() for point in config["points"]]
    global_frontier = set(pareto(all_points))
    raw_x_min = min(point.interactivity for point in all_points)
    raw_x_max = max(point.interactivity for point in all_points)
    raw_y_max = max(point.throughput_per_chip for point in all_points)
    x_step = 5
    x_min = int((raw_x_min - 5) // x_step) * x_step
    x_max = int((raw_x_max + 10) // x_step) * x_step
    y_min, y_step = 0.0, 1000
    y_max = int(raw_y_max // y_step + 2) * y_step

    def sx(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * (right - left)

    def sy(value: float) -> float:
        return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        "<style>",
        "text{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;fill:#172033}",
        ".grid{stroke:#dbe3ec;stroke-width:1}",
        ".axis{stroke:#64748b;stroke-width:1.5}",
        ".tick{font-size:13px;fill:#526174}",
        ".label{font-size:16px;font-weight:600}",
        ".point-label{font-size:13px;font-weight:700}",
        ".title{font-size:27px;font-weight:750}",
        ".subtitle{font-size:14px;fill:#64748b}",
        ".panel-title{font-size:18px;font-weight:700}",
        ".legend{font-size:14px;font-weight:600}",
        ".note{font-size:13px;fill:#64748b}",
        "</style>",
        '<text x="105" y="42" class="title">GLM-5.3 on GB200 — clean prefiller Pareto</text>',
        '<text x="105" y="68" class="subtitle">LMSYS OpenHands: 13 turns, 74,173-token cold turn, prefix growth, fixed 220-token outputs; no model-weight offload</text>',
        '<text x="1130" y="42" text-anchor="end" font-size="18" font-weight="700" style="fill:#059669">better ↗</text>',
    ]

    legend_positions = [(105, 108), (420, 108), (805, 108)]
    for (name, config), (legend_x, legend_y) in zip(series.items(), legend_positions):
        color = str(config["color"])
        out.extend(
            [
                f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 34}" y2="{legend_y}" stroke="{color}" stroke-width="4"/>',
                f'<circle cx="{legend_x + 17}" cy="{legend_y}" r="6" fill="white" stroke="{color}" stroke-width="3"/>',
                f'<text x="{legend_x + 44}" y="{legend_y + 5}" class="legend">{esc(name)}</text>',
            ]
        )
    out.extend(
        [
            '<circle cx="110" cy="135" r="6" fill="#f8fafc" stroke="#94a3b8" stroke-width="2"/>',
            '<path d="M 105 130 L 115 140 M 115 130 L 105 140" stroke="#64748b" stroke-width="2"/>',
            '<text x="125" y="140" class="note">globally dominated</text>',
        ]
    )

    for y in range(int(y_min), int(y_max) + 1, y_step):
        py = sy(float(y))
        out.extend([
            f'<line x1="{left}" y1="{py:.1f}" x2="{right}" y2="{py:.1f}" class="grid"/>',
            f'<text x="{left - 12}" y="{py + 5:.1f}" text-anchor="end" class="tick">{y:,}</text>',
        ])
    x = x_min
    while x <= x_max + 1e-9:
        px = sx(x)
        out.extend([
            f'<line x1="{px:.1f}" y1="{top}" x2="{px:.1f}" y2="{bottom}" class="grid"/>',
            f'<text x="{px:.1f}" y="{bottom + 27}" text-anchor="middle" class="tick">{x:.0f}</text>',
        ])
        x += x_step
    out.extend([
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" class="axis"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" class="axis"/>',
        f'<text x="{(left + right) / 2:.1f}" y="{bottom + 62}" text-anchor="middle" class="label">Interactivity: 1000 / mean TPOT (output tok/s/user) →</text>',
        f'<text x="28" y="{(top + bottom) / 2:.1f}" text-anchor="middle" transform="rotate(-90 28 {(top + bottom) / 2:.1f})" class="label">Total-token throughput per GPU (tok/s/GPU) →</text>',
    ])

    label_offsets = {
        "PCP8 + EP8 (64K batch)": (-9, -10),
        "PCP8 + DCP8 + EP8 (64K, A2A)": (-9, 19),
        "TP8 + EP8": (9, -10),
    }
    for name, config in series.items():
        color = str(config["color"])
        points = list(config["points"])
        points_by_c = sorted(points, key=lambda p: p.concurrency)
        frontier = sorted(
            (point for point in points if point in global_frontier),
            key=lambda point: point.interactivity,
        )
        frontier_set = set(frontier)
        sweep_path = " ".join(
            f"{sx(point.interactivity):.1f},{sy(point.throughput_per_chip):.1f}"
            for point in points_by_c
        )
        frontier_path = " ".join(
            f"{sx(point.interactivity):.1f},{sy(point.throughput_per_chip):.1f}"
            for point in frontier
        )
        out.extend([
            f'<polyline points="{sweep_path}" fill="none" stroke="{color}" stroke-width="1.5" stroke-dasharray="5 5" opacity="0.38"/>',
            f'<polyline points="{frontier_path}" fill="none" stroke="{color}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>',
        ])
        dx, dy = label_offsets[name]
        for point in points_by_c:
            px = sx(point.interactivity)
            py = sy(point.throughput_per_chip)
            if point in frontier_set:
                out.append(
                    f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7" fill="white" stroke="{color}" stroke-width="3"/>'
                )
            else:
                out.extend([
                    f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7" fill="#f8fafc" stroke="{color}" stroke-width="2" opacity="0.7"/>',
                    f'<path d="M {px - 5:.1f} {py - 5:.1f} L {px + 5:.1f} {py + 5:.1f} M {px + 5:.1f} {py - 5:.1f} L {px - 5:.1f} {py + 5:.1f}" stroke="{color}" stroke-width="2" opacity="0.75"/>',
                ])
            anchor = "end" if dx < 0 else "start"
            out.append(
                f'<text x="{px + dx:.1f}" y="{py + dy:.1f}" text-anchor="{anchor}" class="point-label" style="fill:{color}">c{point.concurrency}</text>'
            )

    out.extend(
        [
            '<text x="105" y="755" class="note">Throughput includes prompt and output tokens, divided by all 16 serving GPUs (8 prefill + 8 decode). Upper-right is better.</text>',
            '<text x="105" y="778" class="note">All arms use MTP3, graph-enabled decoders, FlashInfer TRT-LLM MoE, FP8 KV, and resident weights. PCP arms use a 64K prefill batch; TP uses 32K.</text>',
            "</svg>",
        ]
    )
    return "\n".join(out) + "\n"


def main() -> None:
    loaded: dict[str, dict[str, object]] = {}
    for name, config in SERIES.items():
        points = [
            load_point(concurrency, *location)
            for concurrency, location in config["directories"].items()
        ]
        loaded[name] = {"color": config["color"], "points": points}
    svg = render(loaded)
    SVG_OUT.write_text(svg)
    HTML_OUT.write_text(
        "<!doctype html>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        "<title>GB200 GLM-5.3 prefiller Pareto</title>\n"
        "<style>html,body{margin:0;background:#fff}svg{display:block;width:100%;height:auto}</style>\n"
        + svg
    )
    converter = shutil.which("rsvg-convert")
    if converter:
        subprocess.run(
            [converter, "-w", "1800", "-h", "1185", "-o", PNG_OUT, SVG_OUT],
            check=True,
        )
    print(SVG_OUT)
    print(HTML_OUT)
    if PNG_OUT.exists():
        print(PNG_OUT)


if __name__ == "__main__":
    main()
