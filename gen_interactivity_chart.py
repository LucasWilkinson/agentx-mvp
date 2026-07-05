#!/usr/bin/env python3
"""
Scan results/ for pN_dM deployment configs, extract aiperf metrics,
and generate an interactive interactivity-vs-throughput HTML chart.

Usage:
    python3 gen_interactivity_chart.py [results_dir]

Defaults to ./results if no argument given. Output: interactivity_vs_throughput.html
in the same directory as this script (or parent of results_dir).
"""

import base64
import json
import os
import re
import sys
from pathlib import Path

GPUS_PER_NODE = 8

COLORS = [
    '#f97316', '#22d3ee', '#a78bfa', '#34d399', '#f472b6',
    '#facc15', '#fb923c', '#38bdf8', '#c084fc', '#4ade80',
]


def discover_configs(results_dir):
    """Auto-discover deployment configs and concurrency levels from folder structure.

    Expected layout:
        results_dir/
            results_p1_d1/
                results_p1_d1_c1/profile_export_aiperf.json
                results_p1_d1_c4/...
            results_p1_d2/
                ...
    """
    configs = {}
    config_pattern = re.compile(r'^results_(p(\d+)_d(\d+))$')

    for entry in sorted(os.listdir(results_dir)):
        m = config_pattern.match(entry)
        if not m:
            continue
        config_name = m.group(1)
        n_prefill = int(m.group(2))
        n_decode = int(m.group(3))
        decode_gpus = n_decode * GPUS_PER_NODE

        config_dir = os.path.join(results_dir, entry)
        if not os.path.isdir(config_dir):
            continue

        conc_pattern = re.compile(rf'^results_{re.escape(config_name)}_c(\d+)$')
        runs = {}
        for sub in sorted(os.listdir(config_dir)):
            cm = conc_pattern.match(sub)
            if not cm:
                continue
            c_val = int(cm.group(1))
            json_path = os.path.join(config_dir, sub, 'profile_export_aiperf.json')
            if not os.path.isfile(json_path):
                continue
            with open(json_path) as f:
                d = json.load(f)

            runs[c_val] = {
                'out_tps': d['output_token_throughput']['avg'],
                'itl_avg': d['inter_token_latency']['avg'],
                'itl_p50': d['inter_token_latency']['p50'],
                'itl_p99': d['inter_token_latency']['p99'],
                'otpu': d['output_token_throughput_per_user']['avg'],
                'ttft_avg': d['time_to_first_token']['avg'],
                'ttft_p50': d['time_to_first_token']['p50'],
                'ttft_p99': d['time_to_first_token']['p99'],
                'ttft_min': d['time_to_first_token']['min'],
                'ttft_max': d['time_to_first_token']['max'],
            }

        if not runs:
            continue

        version = ''
        ver_path = os.path.join(config_dir, 'vllm_version.txt')
        if os.path.isfile(ver_path):
            with open(ver_path) as vf:
                version = vf.read().strip()

        yamls = {}
        for yname in sorted(os.listdir(config_dir)):
            if yname.endswith('.yaml') or yname.endswith('.yml'):
                ypath = os.path.join(config_dir, yname)
                if os.path.isfile(ypath):
                    with open(ypath) as yf:
                        yamls[yname] = yf.read()

        configs[config_name] = {
            'label': f'{n_prefill}P {n_decode}D',
            'decode_gpus': decode_gpus,
            'pods': f'{n_prefill} prefill + {n_decode} decode',
            'runs': dict(sorted(runs.items())),
            'version': version,
            'yamls': yamls,
        }

    return configs


def highlight_yaml(text):
    import html as htmlmod
    lines = text.split('\n')
    out = []
    for line in lines:
        escaped = htmlmod.escape(line)
        if not escaped.strip():
            out.append(escaped)
            continue
        if escaped.lstrip().startswith('#'):
            out.append(re.sub(r'(#.*)', r'<span class="yc">\1</span>', escaped))
            continue
        m = re.match(r'^(\s*)(- )?([A-Za-z_][\w./\-]*):(.*)$', escaped)
        if m:
            indent, dash, key, rest = m.groups()
            dash = dash or ''
            rest = rest.strip()
            if rest.startswith('#'):
                rest = f' <span class="yc">{rest}</span>'
            elif rest.startswith('"') or rest.startswith('&#x27;') or rest.startswith("'"):
                rest = f' <span class="ys">{rest}</span>'
            elif re.match(r'^-?\d[\d.]*$', rest):
                rest = f' <span class="yn">{rest}</span>'
            elif rest in ('true', 'false', 'null', 'True', 'False', 'None', '""', "''"):
                rest = f' <span class="yn">{rest}</span>'
            elif rest == '|' or rest == '>':
                rest = f' <span class="yv">{rest}</span>'
            elif rest:
                rest = f' <span class="yv">{rest}</span>'
            out.append(f'{indent}{dash}<span class="yk">{key}</span>:{rest}')
        else:
            out.append(escaped)
    return '\n'.join(out)


def generate_html(configs, output_path, results_dir):
    color_map = {}
    for i, cfg in enumerate(sorted(configs.keys())):
        color_map[cfg] = COLORS[i % len(COLORS)]

    configs_js = {}
    data_js = {}
    concurrencies = set()
    for cfg, meta in configs.items():
        configs_js[cfg] = {
            'label': meta['label'],
            'decodeGPUs': meta['decode_gpus'],
            'pods': meta['pods'],
        }
        data_js[cfg] = {}
        for c_val, metrics in meta['runs'].items():
            key = f'c{c_val}'
            concurrencies.add(c_val)
            data_js[cfg][key] = metrics

    sorted_conc = sorted(concurrencies)
    conc_list_js = [f'c{c}' for c in sorted_conc]
    c_labels_js = {f'c{c}': c for c in sorted_conc}

    versions = set(meta['version'] for meta in configs.values() if meta['version'])
    version_str = ', '.join(sorted(versions)) if versions else 'unknown'

    results_dir = os.path.abspath(results_dir)
    embedded_dashboards = {}
    for cfg in configs:
        for c_val in configs[cfg]['runs']:
            key = f'c{c_val}'
            dash_path = os.path.join(results_dir, f'results_{cfg}', f'results_{cfg}_{key}', 'dashboard.html')
            if os.path.isfile(dash_path):
                with open(dash_path, 'rb') as df:
                    embedded_dashboards[f'{cfg}_{key}'] = base64.b64encode(df.read()).decode('ascii')

    yaml_parts = []
    configs_with_yamls = {cfg: meta for cfg, meta in sorted(configs.items()) if meta.get('yamls')}
    if configs_with_yamls:
        yaml_parts.append('<div class="row-header" onclick="this.classList.toggle(\'collapsed\');'
                          'this.nextElementSibling.classList.toggle(\'hidden\')">'
                          '<span class="arrow">&#9660;</span> Pod Specs (YAML)</div>')
        yaml_parts.append('<div style="padding: 0 4px;">')
        for cfg in sorted(configs_with_yamls):
            meta = configs_with_yamls[cfg]
            color = color_map.get(cfg, '#d8d9da')
            for yname, ycontent in sorted(meta['yamls'].items()):
                uid = f'yaml-{cfg}-{yname.replace(".", "-")}'
                label = f'{meta["label"]} — {yname}'
                highlighted = highlight_yaml(ycontent)
                yaml_parts.append(
                    f'<div class="yaml-section">'
                    f'<button class="yaml-toggle" style="border-color:{color};color:{color}" '
                    f'onclick="document.getElementById(\'{uid}\').classList.toggle(\'open\')">{label}</button>'
                    f'<div class="yaml-block" id="{uid}"><pre>{highlighted}</pre></div>'
                    f'</div>'
                )
        yaml_parts.append('</div>')
    yaml_sections_html = '\n'.join(yaml_parts)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>GLM-5.2-FP8 — Interactivity vs Throughput — vLLM {version_str}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #111217; color: #d8d9da; font-family: Inter, -apple-system, sans-serif; padding: 16px; }}
  h1 {{ font-size: 20px; font-weight: 500; margin-bottom: 4px; }}
  .subtitle {{ font-size: 13px; color: #8e8e8e; margin-bottom: 20px; }}
  .row-header {{ font-size: 15px; font-weight: 500; color: #d8d9da; padding: 10px 0 6px 4px;
                 border-bottom: 1px solid #2a2a2e; margin: 16px 0 8px 0; cursor: pointer; user-select: none; }}
  .row-header:hover {{ color: #fff; }}
  .row-header .arrow {{ display: inline-block; width: 16px; transition: transform .15s; }}
  .row-header.collapsed .arrow {{ transform: rotate(-90deg); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(580px, 1fr)); gap: 8px; }}
  .panel {{ background: #181b1f; border: 1px solid #2a2a2e; border-radius: 4px; padding: 0;
             overflow: hidden; resize: both; min-width: 400px; min-height: 300px; }}
  .panel::-webkit-resizable {{ background: transparent; }}
  .panel-title {{ font-size: 13px; font-weight: 500; padding: 8px 12px; color: #d8d9da; }}
  .panel .plot {{ width: 100%; height: calc(100% - 36px); min-height: 250px; }}
  .panel .plot .nsewdrag {{ cursor: pointer !important; }}
  .summary {{ background: #181b1f; border: 1px solid #2a2a2e; border-radius: 4px; padding: 16px; margin-bottom: 16px; }}
  .summary table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .summary th {{ text-align: left; padding: 6px 8px; color: #8e8e8e; border-bottom: 1px solid #2a2a2e; font-weight: 500; }}
  .summary td {{ padding: 6px 8px; border-bottom: 1px solid #1e1e22; }}
  .summary tr:hover {{ background: #1e2127; }}
  .highlight {{ color: #58a6ff; font-weight: 500; }}
  .hidden {{ display: none; }}
  .yaml-section {{ margin-bottom: 8px; }}
  .yaml-toggle {{ background: none; border: 1px solid #2a2a2e; color: #8e8e8e; border-radius: 4px;
                   padding: 6px 14px; cursor: pointer; font-size: 12px; font-family: inherit; }}
  .yaml-toggle:hover {{ color: #d8d9da; border-color: #3a3a3e; }}
  .yaml-block {{ background: #0d1117; border: 1px solid #2a2a2e; border-radius: 4px; padding: 16px;
                  margin-top: 8px; overflow-x: auto; display: none; }}
  .yaml-block.open {{ display: block; }}
  .yaml-block pre {{ font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace; font-size: 12px;
                      line-height: 1.5; color: #d8d9da; white-space: pre; margin: 0; }}
  .yaml-block .yk {{ color: #7ee787; }}
  .yaml-block .yv {{ color: #d2a8ff; }}
  .yaml-block .ys {{ color: #a5d6ff; }}
  .yaml-block .yc {{ color: #8b949e; font-style: italic; }}
  .yaml-block .yn {{ color: #79c0ff; }}
  .side-panel {{ position: fixed; top: 0; right: 0; width: 55vw; height: 100vh; background: #111217;
                 border-left: 2px solid #2a2a2e; z-index: 1000; transform: translateX(100%);
                 transition: transform .25s ease; display: flex; flex-direction: column; }}
  .side-panel.open {{ transform: translateX(0); }}
  .side-panel-header {{ display: flex; align-items: center; justify-content: space-between;
                        padding: 10px 16px; border-bottom: 1px solid #2a2a2e; flex-shrink: 0; }}
  .side-panel-header span {{ font-size: 14px; font-weight: 500; color: #d8d9da; }}
  .side-panel-close {{ background: none; border: 1px solid #3a3a3e; color: #d8d9da; border-radius: 4px;
                       padding: 4px 12px; cursor: pointer; font-size: 13px; }}
  .side-panel-close:hover {{ background: #2a2a2e; }}
  .side-panel iframe {{ flex: 1; border: none; width: 100%; }}
  .side-panel-resize {{ position: absolute; left: -4px; top: 0; width: 8px; height: 100%;
                        cursor: col-resize; z-index: 1001; }}
  .side-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 999;
                   display: none; cursor: pointer; }}
  .side-overlay.open {{ display: block; }}
</style>
</head>
<body>
<h1>GLM-5.2-FP8 Disaggregated Serving — Interactivity vs Throughput</h1>
<div class="subtitle">vLLM {version_str} &middot; {len(configs)} deployment configs &middot; {len(sorted_conc)} concurrency levels</div>

<div id="root"></div>
<div class="side-overlay" id="overlay"></div>
<div class="side-panel" id="sidePanel">
  <div class="side-panel-resize" id="sidePanelResize"></div>
  <div class="side-panel-header">
    <span id="sidePanelTitle"></span>
    <button class="side-panel-close" id="sidePanelClose">Close</button>
  </div>
  <iframe id="sidePanelFrame"></iframe>
</div>

<script>
const LAYOUT_DEFAULTS = {{
  paper_bgcolor: '#181b1f',
  plot_bgcolor: '#181b1f',
  font: {{ family: 'Inter, -apple-system, sans-serif', size: 12, color: '#d8d9da' }},
  margin: {{ t: 40, r: 30, b: 60, l: 70 }},
  xaxis: {{ gridcolor: '#2a2a2e', zerolinecolor: '#2a2a2e', linecolor: '#2a2a2e' }},
  yaxis: {{ gridcolor: '#2a2a2e', zerolinecolor: '#2a2a2e', linecolor: '#2a2a2e' }},
  legend: {{ bgcolor: 'rgba(0,0,0,0)', font: {{ size: 11 }} }},
  hoverlabel: {{ bgcolor: '#23262b', bordercolor: '#3a3a3e', font: {{ size: 12, color: '#ffffff' }} }},
  hovermode: 'closest',
}};

const COLORS = {json.dumps(color_map)};
const CONFIGS = {json.dumps(configs_js)};
const CONCURRENCIES = {json.dumps(conc_list_js)};
const C_LABELS = {json.dumps(c_labels_js)};
const DATA = {json.dumps(data_js)};
const DASHBOARDS = {json.dumps(embedded_dashboards)};
const CONFIG_KEYS = Object.keys(CONFIGS);

const root = document.getElementById('root');
const sidePanel = document.getElementById('sidePanel');
const sidePanelFrame = document.getElementById('sidePanelFrame');
const sidePanelTitle = document.getElementById('sidePanelTitle');
const overlay = document.getElementById('overlay');

const blobCache = {{}};

// Pre-decode all dashboards in the background so clicks are instant
setTimeout(() => {{
  for (const [key, b64] of Object.entries(DASHBOARDS)) {{
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    blobCache[key] = URL.createObjectURL(new Blob([bytes], {{ type: 'text/html;charset=utf-8' }}));
  }}
}}, 100);

function openDashboard(cfg, conc) {{
  const key = cfg + '_' + conc;
  if (!DASHBOARDS[key]) return;
  if (!blobCache[key]) {{
    const bin = atob(DASHBOARDS[key]);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    blobCache[key] = URL.createObjectURL(new Blob([bytes], {{ type: 'text/html;charset=utf-8' }}));
  }}
  sidePanelTitle.textContent = `${{CONFIGS[cfg].label}} @ c${{C_LABELS[conc]}} — Dashboard`;
  sidePanelFrame.src = blobCache[key];
  sidePanel.classList.add('open');
  overlay.classList.add('open');
}}

function closeDashboard() {{
  sidePanel.classList.remove('open');
  overlay.classList.remove('open');
  sidePanelFrame.src = 'about:blank';
}}

document.getElementById('sidePanelClose').addEventListener('click', closeDashboard);
overlay.addEventListener('click', closeDashboard);
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeDashboard(); }});

(function() {{
  const resizer = document.getElementById('sidePanelResize');
  const dragOverlay = document.createElement('div');
  dragOverlay.style.cssText = 'position:fixed;inset:0;z-index:10000;cursor:col-resize;display:none;';
  document.body.appendChild(dragOverlay);

  function stopDrag() {{
    dragOverlay.style.display = 'none';
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', stopDrag);
    dragOverlay.removeEventListener('mousemove', onMove);
    dragOverlay.removeEventListener('mouseup', stopDrag);
  }}
  function onMove(e) {{
    const w = window.innerWidth - e.clientX;
    sidePanel.style.width = Math.max(300, Math.min(w, window.innerWidth - 100)) + 'px';
  }}
  resizer.addEventListener('mousedown', e => {{
    e.preventDefault();
    dragOverlay.style.display = 'block';
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', stopDrag);
    dragOverlay.addEventListener('mousemove', onMove);
    dragOverlay.addEventListener('mouseup', stopDrag);
  }});
}})();

function attachClickHandler(plotEl) {{
  plotEl.on('plotly_click', function(eventData) {{
    const pt = eventData.points[0];
    const cfg = CONFIG_KEYS[pt.curveNumber];
    const validConcs = CONCURRENCIES.filter(c => DATA[cfg] && DATA[cfg][c]);
    const conc = validConcs[pt.pointIndex];
    if (cfg && conc) openDashboard(cfg, conc);
  }});
}}

function makeSection(title, id) {{
  const hdr = document.createElement('div');
  hdr.className = 'row-header';
  hdr.innerHTML = `<span class="arrow">&#9660;</span> ${{title}}`;
  const wrap = document.createElement('div');
  wrap.className = 'grid';
  wrap.id = id;
  hdr.addEventListener('click', () => {{
    hdr.classList.toggle('collapsed');
    wrap.classList.toggle('hidden');
  }});
  root.appendChild(hdr);
  root.appendChild(wrap);
  return wrap;
}}

function makePanel(parent, title, cls) {{
  const panel = document.createElement('div');
  panel.className = 'panel' + (cls ? ' ' + cls : '');
  panel.style.height = '500px';
  const t = document.createElement('div');
  t.className = 'panel-title';
  t.textContent = title;
  const plot = document.createElement('div');
  plot.className = 'plot';
  panel.appendChild(t);
  panel.appendChild(plot);
  parent.appendChild(panel);
  new ResizeObserver(() => Plotly.Plots.resize(plot)).observe(panel);
  return plot;
}}

function hoverText(config, c, d, normalized) {{
  const norm = (d.out_tps / CONFIGS[config].decodeGPUs).toFixed(1);
  return `<b>${{CONFIGS[config].label}} @ c${{C_LABELS[c]}}</b><br>` +
    `Output: ${{d.out_tps.toFixed(1)}} tok/s` + (normalized ? ` (${{norm}} tok/s/GPU)` : '') + `<br>` +
    `ITL p50: ${{d.itl_p50.toFixed(1)}} ms<br>` +
    `ITL p99: ${{d.itl_p99.toFixed(1)}} ms<br>` +
    `Per-user: ${{d.otpu.toFixed(1)}} tok/s/user<br>` +
    `TTFT avg: ${{(d.ttft_avg/1000).toFixed(1)}}s · p50: ${{(d.ttft_p50/1000).toFixed(1)}}s · p99: ${{(d.ttft_p99/1000).toFixed(1)}}s`;
}}

function buildTraces(configs, concurrencies, data, xFn, yFn, normalized) {{
  return Object.entries(configs).map(([cfg, meta]) => {{
    const validConcs = concurrencies.filter(c => data[cfg] && data[cfg][c]);
    const points = validConcs.map(c => data[cfg][c]);
    return {{
      x: points.map(xFn(meta)),
      y: points.map(yFn(meta)),
      text: validConcs.map((c, i) => hoverText(cfg, c, points[i], normalized)),
      hoverinfo: 'text',
      mode: 'lines+markers+text',
      textposition: 'top center',
      textfont: {{ size: 10, color: COLORS[cfg] }},
      texttemplate: validConcs.map(c => `c${{C_LABELS[c]}}`),
      name: `${{meta.label}} (${{meta.decodeGPUs}} decode GPUs)`,
      line: {{ color: COLORS[cfg], width: 2.5 }},
      marker: {{ size: 10, color: COLORS[cfg] }},
    }};
  }});
}}

// ── Hint ──
const hint = document.createElement('div');
hint.style.cssText = 'color:#ffffff; font-size:15px; padding:12px 4px 8px;';
hint.textContent = 'Click any data point to open its Prometheus dashboard.';
root.appendChild(hint);

// ── Section 1: Per-User Throughput (X) vs Normalized Output Throughput / Decode GPU (Y) ──
const sec1 = makeSection('Output Throughput / Decode GPU vs Per-User Throughput', 'sec-norm');

(function() {{
  const el = makePanel(sec1, 'Normalized Output Throughput vs Per-User Throughput', 'wide');
  const traces = buildTraces(CONFIGS, CONCURRENCIES, DATA,
    _    => p => p.otpu,
    meta => p => p.out_tps / meta.decodeGPUs,
    true);
  Plotly.newPlot(el, traces, {{
    ...LAYOUT_DEFAULTS,
    title: {{ text: 'Output Throughput / Decode GPU vs Per-User Throughput', font: {{ size: 14, color: '#d8d9da' }} }},
    xaxis: {{ ...LAYOUT_DEFAULTS.xaxis, title: {{ text: 'Output Token Throughput Per User (tok/s/user)', font: {{ size: 12 }} }} }},
    yaxis: {{ ...LAYOUT_DEFAULTS.yaxis, title: {{ text: 'Output Token Throughput / Decode GPU (tok/s/GPU)', font: {{ size: 12 }} }}, rangemode: 'tozero' }},
    legend: {{ ...LAYOUT_DEFAULTS.legend, x: 0.99, y: 0.99, xanchor: 'right', yanchor: 'top' }},
  }}, {{ responsive: true, edits: {{ legendPosition: true }} }});
  attachClickHandler(el);
}})();

// ── Section 2: Summary table ──
const sec2 = makeSection('Data Summary', 'sec-table');
(function() {{
  const div = document.createElement('div');
  div.className = 'summary';
  let html = '<table><tr><th>Config</th><th>Concurrency</th><th>Decode GPUs</th><th>Output tok/s</th><th>tok/s/GPU</th><th>ITL p50 (ms)</th><th>ITL p99 (ms)</th><th>Per-user tok/s</th><th>TTFT avg (s)</th><th>TTFT p50 (s)</th><th>TTFT p99 (s)</th><th>TTFT min (s)</th><th>TTFT max (s)</th></tr>';
  for (const [cfg, meta] of Object.entries(CONFIGS)) {{
    for (const c of CONCURRENCIES) {{
      if (!DATA[cfg] || !DATA[cfg][c]) continue;
      const d = DATA[cfg][c];
      const norm = (d.out_tps / meta.decodeGPUs).toFixed(1);
      html += `<tr>` +
        `<td style="color:${{COLORS[cfg]}};font-weight:500">${{meta.label}}</td>` +
        `<td>${{C_LABELS[c]}}</td>` +
        `<td>${{meta.decodeGPUs}}</td>` +
        `<td>${{d.out_tps.toFixed(1)}}</td>` +
        `<td class="highlight">${{norm}}</td>` +
        `<td>${{d.itl_p50.toFixed(1)}}</td>` +
        `<td>${{d.itl_p99.toFixed(1)}}</td>` +
        `<td>${{d.otpu.toFixed(1)}}</td>` +
        `<td>${{(d.ttft_avg/1000).toFixed(1)}}</td>` +
        `<td>${{(d.ttft_p50/1000).toFixed(1)}}</td>` +
        `<td>${{(d.ttft_p99/1000).toFixed(1)}}</td>` +
        `<td>${{(d.ttft_min/1000).toFixed(1)}}</td>` +
        `<td>${{(d.ttft_max/1000).toFixed(1)}}</td>` +
        `</tr>`;
    }}
  }}
  html += '</table>';
  div.innerHTML = html;
  sec2.appendChild(div);
}})();
</script>

{yaml_sections_html}

</body>
</html>"""

    with open(output_path, 'w') as f:
        f.write(html)
    return output_path


def main():
    if len(sys.argv) > 1:
        results_dir = sys.argv[1]
    else:
        results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')

    if not os.path.isdir(results_dir):
        print(f"Error: {results_dir} not found", file=sys.stderr)
        sys.exit(1)

    configs = discover_configs(results_dir)
    if not configs:
        print(f"Error: no results_pN_dM/ directories found in {results_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.dirname(os.path.abspath(results_dir))
    output_path = os.path.join(output_dir, 'interactivity_vs_throughput.html')

    generate_html(configs, output_path, results_dir)

    print(f"Discovered {len(configs)} configs:")
    for cfg, meta in sorted(configs.items()):
        concs = sorted(meta['runs'].keys())
        print(f"  {cfg}: {meta['label']} ({meta['decode_gpus']} decode GPUs) — c{',c'.join(str(c) for c in concs)}")
    print(f"\nGenerated: {output_path}")


if __name__ == '__main__':
    main()
