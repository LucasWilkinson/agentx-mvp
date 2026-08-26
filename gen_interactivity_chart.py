#!/usr/bin/env python3
"""
Scan results/ for pN_dM deployment configs, extract aiperf metrics,
and generate an interactive interactivity-vs-throughput HTML chart.

Usage:
    python3 gen_interactivity_chart.py [results_dir]

Defaults to ./results if no argument is given. Output:
<results_dir>/interactivity_vs_throughput.html.
"""

import base64
import csv
import json
import os
import re
import sys
from pathlib import Path

GPUS_PER_NODE = 8
STAT_KEYS = ['avg', 'min', 'p50', 'p90', 'p95', 'p99', 'max']

COLORS = [
    '#f97316', '#22d3ee', '#a78bfa', '#34d399', '#f472b6',
    '#facc15', '#fb923c', '#38bdf8', '#c084fc', '#4ade80',
]


def read_model_label(results_dir):
    candidates = [Path(results_dir) / 'model_label.txt']
    candidates.extend(sorted(Path(results_dir).glob('results_*/model_label.txt')))
    for path in candidates:
        if path.is_file():
            label = path.read_text().strip()
            if label:
                return label
    return os.environ.get('MODEL_LABEL', 'DeepSeek-V4-Pro')


def read_text_file(path, default=''):
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return default
    return text or default


def read_int_file(path, default):
    try:
        return int(read_text_file(path, str(default)))
    except ValueError:
        return default


def parse_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        value = value.strip().replace(',', '')
        if value.isdigit():
            return int(value)
    return None


def tokens_from_cache_config_labels(labels):
    if not isinstance(labels, dict):
        return None
    kv_cache_size_tokens = parse_int(labels.get('kv_cache_size_tokens'))
    if kv_cache_size_tokens is not None:
        return kv_cache_size_tokens
    num_gpu_blocks = parse_int(labels.get('num_gpu_blocks'))
    block_size = parse_int(labels.get('block_size'))
    if num_gpu_blocks is None or block_size is None:
        return None
    return num_gpu_blocks * block_size


def classify_kv_role(labels, token_value=None):
    label_text = ' '.join(str(v).lower() for v in labels.values())
    if 'prefill' in label_text:
        return 'prefill'
    if 'decode' in label_text:
        return 'decode'
    # Older AIPerf server-metrics exports collapse endpoint labels to the
    # gateway. For our WideEP runs the prefill ranks are the smaller cache
    # entries and decode ranks are the larger cache entries.
    if token_value is not None:
        return '_by_size'
    return None


def kv_cache_tokens_from_series(series):
    by_role = {'prefill': [], 'decode': []}
    by_size = []
    for item in series:
        labels = item.get('labels', {}) if isinstance(item, dict) else {}
        token_value = tokens_from_cache_config_labels(labels)
        if token_value is None:
            continue
        role = classify_kv_role(labels, token_value)
        if role in by_role:
            by_role[role].append(token_value)
        elif role == '_by_size':
            by_size.append(token_value)

    if not by_role['prefill'] and not by_role['decode'] and by_size:
        sizes = sorted(set(by_size))
        if len(sizes) >= 2:
            by_role['prefill'] = [v for v in by_size if v == sizes[0]]
            by_role['decode'] = [v for v in by_size if v == sizes[-1]]
        else:
            by_role['decode'] = by_size

    return {
        'prefill': sum(by_role['prefill']) or None,
        'decode': sum(by_role['decode']) or None,
    }


def find_kv_cache_tokens_in_json(value):
    if isinstance(value, dict):
        metric = value.get('vllm:cache_config_info')
        if isinstance(metric, dict) and isinstance(metric.get('series'), list):
            tokens = kv_cache_tokens_from_series(metric['series'])
            if tokens['prefill'] is not None or tokens['decode'] is not None:
                return tokens

        if isinstance(value.get('series'), list):
            tokens = kv_cache_tokens_from_series(value['series'])
            if tokens['prefill'] is not None or tokens['decode'] is not None:
                return tokens

        totals = {'prefill': 0, 'decode': 0}
        found = False
        for child in value.values():
            child_value = find_kv_cache_tokens_in_json(child)
            if child_value is None:
                continue
            found = True
            for role in totals:
                totals[role] += child_value.get(role) or 0
        if found:
            return {role: (total or None) for role, total in totals.items()}
    elif isinstance(value, list):
        totals = {'prefill': 0, 'decode': 0}
        found = False
        for child in value:
            child_value = find_kv_cache_tokens_in_json(child)
            if child_value is None:
                continue
            found = True
            for role in totals:
                totals[role] += child_value.get(role) or 0
        if found:
            return {role: (total or None) for role, total in totals.items()}
    return None


def read_kv_cache_tokens_from_cache_config(run_dir):
    json_path = os.path.join(run_dir, 'server_metrics_export.json')
    if os.path.isfile(json_path):
        try:
            with open(json_path) as f:
                payload = json.load(f)
                token_values = find_kv_cache_tokens_in_json(payload.get('metrics', payload))
            if token_values is not None:
                return token_values
        except (OSError, json.JSONDecodeError):
            pass

    csv_path = os.path.join(run_dir, 'server_metrics_export.csv')
    if os.path.isfile(csv_path):
        by_role = {'prefill': [], 'decode': []}
        by_size = []
        try:
            with open(csv_path, newline='') as f:
                reader = csv.reader(row for row in f if not row.startswith('#'))
                current = {}
                for row in reader:
                    if len(row) < 4:
                        continue
                    if row[1] == 'vllm:cache_config_info':
                        label_name = row[2]
                        label_value = row[3]
                    elif len(row) >= 5 and row[2] == 'vllm:cache_config_info':
                        label_name = row[3]
                        label_value = row[4]
                    else:
                        continue
                    current[label_name] = label_value
                    token_value = tokens_from_cache_config_labels(current)
                    if token_value is not None:
                        role = classify_kv_role(current, token_value)
                        if role in by_role:
                            by_role[role].append(token_value)
                        else:
                            by_size.append(token_value)
                        current = {}
            if not by_role['prefill'] and not by_role['decode'] and by_size:
                sizes = sorted(set(by_size))
                if len(sizes) >= 2:
                    by_role['prefill'] = [v for v in by_size if v == sizes[0]]
                    by_role['decode'] = [v for v in by_size if v == sizes[-1]]
                else:
                    by_role['decode'] = by_size
            if by_role['prefill'] or by_role['decode']:
                return {
                    'prefill': sum(by_role['prefill']) or None,
                    'decode': sum(by_role['decode']) or None,
                }
        except OSError:
            pass
    return None


def read_kv_cache_tokens(run_dir):
    return read_kv_cache_tokens_from_cache_config(run_dir)


# --- Fallback when AIPerf server metrics were not captured: per-config kv-cache.yaml (scripts/kv-cache-info.sh)
# or the saved pod logs, scaled by the role's data-parallel size from vllm-args.yaml. Values are totals
# across DP ranks, like the vllm:cache_config_info sum above. TP and PCP ranks hold replicated KV (PCP
# all-gathers every chunk's latent KV and writes it on every rank, vllm/v1/attention/ops/pcp.py), so no scaling.
KV_LOG_RE = re.compile(r'GPU KV cache size: ([\d,]+) tokens')
KV_YAML_RE = re.compile(r'^(prefill|decode)\S*:\s*$|^\s+kv_cache_tokens:\s*(\d+)\s*$', re.M)
DP_YAML_RE = re.compile(r'^(prefill|decode)\S*:\s*$|^\s+data-parallel-size:\s*(\d+)\s*$', re.M)


def _per_role_from_yaml(path, pattern):
    out = {}
    if not os.path.isfile(path):
        return out
    role = None
    with open(path) as f:
        for m in pattern.finditer(f.read()):
            if m.group(1):
                role = m.group(1)
            elif role and m.group(2):
                out.setdefault(role, int(m.group(2)))
    return out


def read_kv_cache_tokens_from_config_dir(config_dir):
    per_rank = _per_role_from_yaml(os.path.join(config_dir, 'kv-cache.yaml'), KV_YAML_RE)
    if not per_rank:
        logs_dir = os.path.join(config_dir, 'logs')
        if os.path.isdir(logs_dir):
            for name in sorted(os.listdir(logs_dir)):
                role = 'prefill' if 'prefill' in name else 'decode' if 'decode' in name else None
                if role is None or role in per_rank:
                    continue
                try:
                    with open(os.path.join(logs_dir, name), errors='replace') as f:
                        for line in f:
                            m = KV_LOG_RE.search(line)
                            if m:
                                per_rank[role] = int(m.group(1).replace(',', ''))
                                break
                except OSError:
                    continue
    if not per_rank:
        return None
    dp = _per_role_from_yaml(os.path.join(config_dir, 'vllm-args.yaml'), DP_YAML_RE)
    return {role: per_rank.get(role, 0) * dp.get(role, 1) or None for role in ('prefill', 'decode')}


def discover_configs(results_dir):
    """Auto-discover deployment configs and concurrency levels from folder structure.

    Expected layout:
        results_dir/
            results_<user>-wide-ep/
                results_<user>-wide-ep_c64/profile_export_aiperf.json
                results_<user>-wide-ep_c256/...
    """
    configs = {}
    metric_units = {}

    for entry in sorted(os.listdir(results_dir)):
        if not entry.startswith('results_'):
            continue
        config_dir = os.path.join(results_dir, entry)
        if not os.path.isdir(config_dir):
            continue

        config_name = entry[len('results_'):]
        decode_gpus = read_int_file(os.path.join(config_dir, 'decode_gpus.txt'), GPUS_PER_NODE)
        prefill_gpus = read_int_file(os.path.join(config_dir, 'prefill_gpus.txt'), GPUS_PER_NODE)
        default_label = config_name
        default_pods = read_text_file(os.path.join(config_dir, 'pods.txt'), config_name)

        conc_pattern = re.compile(rf'^results_{re.escape(config_name)}_c(\d+)$')
        config_kv_cache_tokens = read_kv_cache_tokens_from_config_dir(config_dir)
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

            run_data = {}
            for key, val in d.items():
                if isinstance(val, dict) and 'avg' in val:
                    run_data[key] = {s: val[s] for s in STAT_KEYS if s in val}
                    if key not in metric_units:
                        metric_units[key] = val.get('unit', '')

            kv_cache_tokens = read_kv_cache_tokens(os.path.join(config_dir, sub)) or config_kv_cache_tokens
            if kv_cache_tokens is not None:
                run_data['_kv_cache_tokens'] = kv_cache_tokens

            runs[c_val] = run_data

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

        label = read_text_file(os.path.join(config_dir, 'config_label.txt'), default_label)
        pods = read_text_file(os.path.join(config_dir, 'pods.txt'), default_pods)
        configs[config_name] = {
            'label': label,
            'decode_gpus': decode_gpus,
            'prefill_gpus': prefill_gpus,
            'pods': pods,
            'runs': dict(sorted(runs.items())),
            'version': version,
            'yamls': yamls,
        }

    return configs, metric_units


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


# ── Compare mode (pin two or more runs and overlay their Grafana panel series) ──
COMPARE_CSS = """
  .side-panel iframe.hidden { display: none; }
  .compare-view { flex: 1; overflow-y: auto; padding: 8px 12px 24px; display: none; }
  .compare-view.open { display: block; }
  .compare-view .grid { grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); }
  .compare-view .panel { height: auto; }
  .compare-view .plot { width: 100%; height: 240px; }
  .compare-view .row-header { margin: 12px 0 6px 0; font-size: 14px; }
  .compare-legend { display: flex; flex-wrap: wrap; gap: 8px 16px; padding: 6px 4px 10px; font-size: 12px; }
  .compare-legend .sw { display: inline-block; width: 22px; height: 3px; vertical-align: middle; margin-right: 6px; }
  .compare-empty { color: #8e8e8e; font-size: 13px; padding: 40px 12px; text-align: center; }
  .pin-bar { display: none; align-items: center; flex-wrap: wrap; gap: 6px; padding: 4px 4px 8px; font-size: 13px; }
  .pin-bar.open { display: flex; }
  .pin-bar .pin-label { color: #8e8e8e; margin-right: 4px; }
  .pin-chip { display: inline-flex; align-items: center; gap: 6px; border: 1px solid #3a3a3e; border-radius: 12px;
              padding: 3px 8px 3px 10px; background: #181b1f; color: #d8d9da; }
  .pin-chip .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .pin-chip .rm { background: none; border: none; color: #8e8e8e; cursor: pointer; font-size: 14px; line-height: 1; padding: 0 2px; }
  .pin-chip .rm:hover { color: #fff; }
  .pin-btn { background: none; border: 1px solid #3a3a3e; color: #d8d9da; border-radius: 4px; padding: 4px 10px;
             cursor: pointer; font-size: 12px; font-family: inherit; }
  .pin-btn:hover { background: #2a2a2e; }
  .pin-btn.active { border-color: #6ED0E0; color: #6ED0E0; }
"""

COMPARE_JS = r"""
// ── Compare mode: pin several runs and overlay their dashboard panel series ──
const sidePanelCompare = document.getElementById('sidePanelCompare');
const pinBar = document.getElementById('pinBar');
const pinChips = document.getElementById('pinChips');
const compareToggle = document.getElementById('compareToggle');
const parsedDashboards = {};
const pinned = [];            // [{cfg, conc}] in pin order
let compareMode = false;      // when on, plain clicks pin instead of opening the single dashboard
const MAX_PINNED = 4;
const DASHES = ['solid', 'dash', 'dot', 'dashdot', 'longdash', 'longdashdot'];
const COMPARE_UNIT_LABEL = { tps: 'tokens/s', reqps: 'requests/s', ops: 'ops/s', percentunit: '%', percent: '%',
                             s: 'seconds', bytes: 'bytes', Bps: 'bytes/s', watt: 'watts', short: 'count' };

function decodeDashboard(key) {
  const bin = atob(DASHBOARDS[key]);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder('utf-8').decode(bytes);
}

// Pull the `const panels = {...};` / `const rows = [...];` JSON out of an exported dashboard.html
function parseDashboard(key) {
  if (parsedDashboards[key] !== undefined) return parsedDashboards[key];
  let parsed = null;
  if (DASHBOARDS[key]) {
    const html = decodeDashboard(key);
    const pm = html.match(/^const panels = (.*);$/m);
    const rm = html.match(/^const rows = (.*);$/m);
    if (pm) {
      const panels = JSON.parse(pm[1]);
      const rows = rm ? JSON.parse(rm[1]) : [];
      let t0 = Infinity;
      for (const p of Object.values(panels))
        for (const q of p.queries) for (const s of q.series)
          if (s.values.length && s.values[0][0] < t0) t0 = s.values[0][0];
      parsed = { panels, rows, t0: isFinite(t0) ? t0 : 0 };
    }
  }
  parsedDashboards[key] = parsed;
  return parsed;
}

function compareSeriesLabel(q, s) {
  let l = q.legend;
  if (l) {
    for (const [k, v] of Object.entries(s.labels)) l = l.split('{{' + k + '}}').join(v);
    if (!l.includes('{{')) return l;
  }
  const parts = Object.entries(s.labels).filter(([k]) => k !== '__name__');
  return parts.length ? parts.map(([k, v]) => k + '=' + v).join(', ') : q.expr.slice(0, 60);
}

function mixWhite(hex, f) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) return hex;
  const c = m.slice(1).map(h => Math.round(parseInt(h, 16) * (1 - f) + 255 * f));
  return '#' + c.map(v => v.toString(16).padStart(2, '0')).join('');
}
function runKey(r) { return r.cfg + '_' + r.conc; }
function runLabel(r) { return `${CONFIGS[r.cfg].label} @ c${C_LABELS[r.conc]}`; }
function runColor(r, idx) {
  const base = COLORS[r.cfg] || '#d8d9da';
  const dup = pinned.slice(0, idx).filter(o => o.cfg === r.cfg).length;  // same config pinned twice: lighten
  return dup ? mixWhite(base, Math.min(0.6, 0.3 * dup)) : base;
}

function updatePinBar() {
  pinChips.innerHTML = '';
  pinned.forEach((r, i) => {
    const chip = document.createElement('span');
    chip.className = 'pin-chip';
    chip.innerHTML = `<span class="dot" style="background:${runColor(r, i)}"></span><span>${runLabel(r)}</span>`;
    const rm = document.createElement('button');
    rm.className = 'rm'; rm.title = 'Unpin'; rm.textContent = '×';
    rm.addEventListener('click', () => { pinned.splice(i, 1); updatePinBar(); if (sidePanelCompare.classList.contains('open')) openCompare(); });
    chip.appendChild(rm);
    pinChips.appendChild(chip);
  });
  pinBar.classList.toggle('open', pinned.length > 0 || compareMode);
}

function pinRun(cfg, conc) {
  const idx = pinned.findIndex(r => r.cfg === cfg && r.conc === conc);
  if (idx < 0) {
    if (pinned.length >= MAX_PINNED) pinned.pop();   // keep the first N-1, replace the last one
    pinned.push({ cfg, conc });
  }
  updatePinBar();
  openCompare();
}

function clearPins() {
  pinned.length = 0;
  updatePinBar();
  if (sidePanelCompare.classList.contains('open')) closeDashboard();
}

const compareObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    const div = entry.target;
    compareObserver.unobserve(div);
    const { traces, unit } = div._compare;
    const plotDiv = div.querySelector('.plot');
    const yTitle = COMPARE_UNIT_LABEL[unit] || unit || '';
    Plotly.newPlot(plotDiv, traces, {
      margin: { l: 58, r: 16, t: 4, b: 36 },
      paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
      font: { color: '#8e8e8e', size: 10 },
      xaxis: { gridcolor: '#2a2a2e', linecolor: '#2a2a2e', ticksuffix: 's',
               title: { text: 'seconds since run start', font: { size: 10 } } },
      yaxis: { gridcolor: '#2a2a2e', linecolor: '#2a2a2e', tickformat: '.3s', hoverformat: '.4g',
               title: yTitle ? { text: yTitle, font: { size: 10 } } : undefined },
      legend: { font: { size: 9 }, orientation: 'h', y: -0.35 },
      showlegend: true,
      hovermode: 'x unified',
      hoverlabel: { bgcolor: '#23262b', bordercolor: '#3a3a3e', font: { size: 11, color: '#ffffff' } },
    }, { responsive: true, displayModeBar: false });
  });
}, { root: sidePanelCompare, rootMargin: '200px' });

new ResizeObserver(() => {
  sidePanelCompare.querySelectorAll('.plot').forEach(p => { if (p._fullLayout) Plotly.Plots.resize(p); });
}).observe(sidePanelCompare);

function renderCompare() {
  sidePanelCompare.innerHTML = '';
  const runs = pinned.map((r, i) => ({ ...r, data: parseDashboard(runKey(r)), color: runColor(r, i), label: runLabel(r) }))
                     .filter(r => r.data);
  if (!runs.length) {
    sidePanelCompare.innerHTML = '<div class="compare-empty">No dashboards available for the pinned runs.</div>';
    return;
  }
  const legend = document.createElement('div');
  legend.className = 'compare-legend';
  legend.innerHTML = runs.map(r => `<span><span class="sw" style="background:${r.color}"></span>${r.label}</span>`).join('');
  sidePanelCompare.appendChild(legend);

  // Section/panel order follows the first pinned run; panels only present in other runs go at the end.
  const sections = [];
  let cur = { title: '', ids: [] };
  const seen = new Set();
  for (const item of runs[0].data.rows) {
    if (item.type === 'row') { if (cur.ids.length) sections.push(cur); cur = { title: item.title, ids: [] }; }
    else if (item.type === 'panel') { cur.ids.push(String(item.id)); seen.add(String(item.id)); }
  }
  if (cur.ids.length) sections.push(cur);
  const extra = { title: 'Other', ids: [] };
  for (const r of runs) for (const id of Object.keys(r.data.panels)) if (!seen.has(id)) { seen.add(id); extra.ids.push(id); }
  if (extra.ids.length) sections.push(extra);

  for (const sec of sections) {
    const grid = document.createElement('div');
    grid.className = 'grid';
    if (sec.title) {
      const h = document.createElement('div');
      h.className = 'row-header';
      h.innerHTML = '<span class="arrow">&#9660;</span> ' + sec.title;
      h.addEventListener('click', () => { h.classList.toggle('collapsed'); grid.classList.toggle('hidden'); });
      sidePanelCompare.appendChild(h);
    }
    sidePanelCompare.appendChild(grid);
    for (const id of sec.ids) {
      const traces = [];
      let title = null, unit = '';
      for (const r of runs) {
        const p = r.data.panels[id];
        if (!p) continue;
        if (title === null) { title = p.title; unit = p.unit; }
        let si = 0;
        for (const q of p.queries) for (const s of q.series) {
          if (!s.values.length) continue;
          traces.push({
            x: s.values.map(v => v[0] - r.data.t0),
            y: s.values.map(v => parseFloat(v[1])),
            name: r.label + ' · ' + compareSeriesLabel(q, s),
            type: 'scatter', mode: 'lines',
            line: { width: 1.5, color: r.color, dash: DASHES[si % DASHES.length] },
            hovertemplate: '%{y:.4g}<extra>%{fullData.name}</extra>',
          });
          si++;
        }
      }
      if (title === null) continue;
      const div = document.createElement('div');
      div.className = 'panel';
      div.innerHTML = '<div class="panel-title">' + title + '</div>';
      if (!traces.length) {
        div.innerHTML += '<div class="empty">No data</div>';
        grid.appendChild(div);
        continue;
      }
      const plotDiv = document.createElement('div');
      plotDiv.className = 'plot';
      div.appendChild(plotDiv);
      grid.appendChild(div);
      div._compare = { traces, unit };
      compareObserver.observe(div);
    }
  }
}

function openCompare() {
  if (!pinned.length) return;
  sidePanelTitle.textContent = 'Compare: ' + pinned.map(runLabel).join(' vs ');
  sidePanelFrame.src = 'about:blank';
  sidePanelFrame.classList.add('hidden');
  sidePanelCompare.classList.add('open');
  renderCompare();
  sidePanel.classList.add('open');
  overlay.classList.add('open');
}

compareToggle.addEventListener('click', () => {
  compareMode = !compareMode;
  compareToggle.classList.toggle('active', compareMode);
  compareToggle.textContent = compareMode ? 'Compare mode: on' : 'Compare mode: off';
  updatePinBar();
});
document.getElementById('pinOpen').addEventListener('click', openCompare);
document.getElementById('pinClear').addEventListener('click', clearPins);
"""


def generate_html(configs, output_path, results_dir, metric_units):
    model_label = read_model_label(results_dir)
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
            'prefillGPUs': meta['prefill_gpus'],
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

    metrics_js = {k: {'unit': v} for k, v in sorted(metric_units.items())}
    x_axis_metrics = [
        'output_token_throughput_per_user',
        'inter_token_latency',
        'time_to_first_token',
        'time_to_second_token',
        'request_latency',
        'effective_latency',
        'credit_to_start_latency',
        'input_sequence_length',
        'output_sequence_length',
        'tokens_in_flight',
        'effective_concurrency',
        'effective_decode_concurrency',
        'effective_prefill_concurrency',
        'request_throughput',
        'theoretical_prefix_cache_hit',
    ]
    y_axis_metrics = [
        'output_token_throughput',
        'output_token_throughput_per_user',
        'e2e_output_token_throughput',
        'input_token_throughput',
        'total_token_throughput',
        'effective_decode_throughput',
        'effective_prefill_throughput',
        'effective_total_throughput',
        'active_decode_throughput',
        'active_prefill_throughput',
        'active_total_throughput',
        'request_throughput',
        'request_count',
        'total_output_tokens',
        'total_usage_prompt_tokens',
        'total_usage_completion_tokens',
        'total_usage_total_tokens',
    ]
    decode_normalized_metrics = [
        'output_token_throughput',
        'e2e_output_token_throughput',
        'effective_decode_throughput',
        'active_decode_throughput',
    ]
    prefill_normalized_metrics = [
        'input_token_throughput',
        'effective_prefill_throughput',
        'active_prefill_throughput',
    ]
    total_normalized_metrics = [
        'total_token_throughput',
        'effective_total_throughput',
        'active_total_throughput',
    ]
    metric_labels = {
        'active_decode_throughput': 'Active decode throughput',
        'active_decode_throughput_per_user': 'Active decode throughput/user',
        'active_prefill_throughput': 'Active prefill throughput',
        'active_prefill_throughput_per_user': 'Active prefill throughput/user',
        'active_total_throughput': 'Active total throughput',
        'credit_to_start_latency': 'Credit-to-start latency',
        'e2e_output_token_throughput': 'E2E output token throughput',
        'effective_concurrency': 'Effective concurrency',
        'effective_decode_concurrency': 'Effective decode concurrency',
        'effective_decode_throughput': 'Effective decode throughput',
        'effective_decode_throughput_per_user': 'Effective decode throughput/user',
        'effective_latency': 'Effective latency',
        'effective_prefill_concurrency': 'Effective prefill concurrency',
        'effective_prefill_throughput': 'Effective prefill throughput',
        'effective_prefill_throughput_per_user': 'Effective prefill throughput/user',
        'effective_total_throughput': 'Effective total throughput',
        'input_sequence_length': 'Input sequence length',
        'input_token_throughput': 'Input token throughput',
        'inter_token_latency': 'Inter-token latency',
        'output_sequence_length': 'Output sequence length',
        'output_token_throughput': 'Output token throughput',
        'output_token_throughput_per_user': 'Output token throughput/user',
        'request_count': 'Request count',
        'request_latency': 'Request latency',
        'request_throughput': 'Request throughput',
        'theoretical_prefix_cache_hit': 'Theoretical prefix cache hit',
        'time_to_first_token': 'Time to first token',
        'time_to_second_token': 'Time to second token',
        'tokens_in_flight': 'Tokens in flight',
        'total_output_tokens': 'Total output tokens',
        'total_token_throughput': 'Total token throughput',
        'total_usage_completion_tokens': 'Total completion tokens',
        'total_usage_prompt_tokens': 'Total prompt tokens',
        'total_usage_total_tokens': 'Total tokens',
    }

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
            safe_cfg = re.sub(r'[^A-Za-z0-9_-]', '-', cfg)
            buttons, blocks = [], []
            for yname, ycontent in sorted(meta['yamls'].items()):
                uid = f'yaml-{safe_cfg}-{yname.replace(".", "-")}'
                buttons.append(
                    f'<button class="yaml-toggle" style="border-color:{color};color:{color}" '
                    f'onclick="var t=document.getElementById(\'{uid}\'),s=this.closest(\'.yaml-section\');'
                    f's.querySelectorAll(\'.yaml-block.open\').forEach(function(b){{if(b!==t)b.classList.remove(\'open\')}});'
                    f't.classList.toggle(\'open\')">{yname}</button>')
                blocks.append(f'<div class="yaml-block" id="{uid}"><pre>{highlight_yaml(ycontent)}</pre></div>')
            yaml_parts.append(
                f'<div class="yaml-section">'
                f'<div class="yaml-cfg-label" style="color:{color}">{meta["label"]}</div>'
                f'<div class="yaml-btn-row">{"".join(buttons)}</div>'
                f'{"".join(blocks)}</div>')
        yaml_parts.append('</div>')
    yaml_sections_html = '\n'.join(yaml_parts)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{model_label} — Interactivity vs Throughput — vLLM {version_str}</title>
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
  .summary {{ background: #181b1f; border: 1px solid #2a2a2e; border-radius: 4px; padding: 16px; margin-bottom: 16px; overflow-x: auto; }}
  .summary table {{ width: 100%; border-collapse: collapse; font-size: 12px; min-width: 900px; }}
  .summary th {{ text-align: left; padding: 6px 8px; color: #8e8e8e; border-bottom: 1px solid #2a2a2e; font-weight: 500;
                 cursor: pointer; user-select: none; white-space: nowrap; }}
  .summary th:hover {{ color: #d8d9da; }}
  .chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 8px; }}
  @media (max-width: 1200px) {{ .chart-row {{ grid-template-columns: 1fr; }} }}
  .chart-col {{ display: flex; flex-direction: column; gap: 4px; }}
  .chart-col .panel {{ min-width: 0; }}
  .axis-controls {{ display: flex; gap: 10px; align-items: center; padding: 6px 4px; flex-wrap: wrap; }}
  .axis-controls label {{ color: #8e8e8e; font-size: 12px; display: flex; align-items: center; gap: 4px; }}
  .axis-controls select {{ background: #181b1f; color: #d8d9da; border: 1px solid #2a2a2e; border-radius: 4px;
                           padding: 4px 6px; font-size: 11px; font-family: inherit; max-width: 300px; }}
  .summary td {{ padding: 6px 8px; border-bottom: 1px solid #1e1e22; }}
  .summary tr:hover {{ background: #1e2127; }}
  .highlight {{ color: #58a6ff; font-weight: 500; }}
  .hidden {{ display: none; }}
  .yaml-section {{ margin-bottom: 12px; }}
  .yaml-cfg-label {{ font-size: 13px; font-weight: 600; margin-bottom: 4px; }}
  .yaml-btn-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }}
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
{COMPARE_CSS}</style>
</head>
<body>
<h1>{model_label} Disaggregated Serving — Interactivity vs Throughput</h1>
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
  <div class="compare-view" id="sidePanelCompare"></div>
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
const METRICS = {json.dumps(metrics_js)};
const X_AXIS_METRICS = {json.dumps(x_axis_metrics)};
const Y_AXIS_METRICS = {json.dumps(y_axis_metrics)};
const DECODE_NORMALIZED_METRICS = new Set({json.dumps(decode_normalized_metrics)});
const PREFILL_NORMALIZED_METRICS = new Set({json.dumps(prefill_normalized_metrics)});
const TOTAL_NORMALIZED_METRICS = new Set({json.dumps(total_normalized_metrics)});
const METRIC_LABELS = {json.dumps(metric_labels)};
const STAT_KEYS = {json.dumps(STAT_KEYS)};
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
  sidePanelCompare.classList.remove('open');
  sidePanelFrame.classList.remove('hidden');
  sidePanelFrame.src = blobCache[key];
  sidePanel.classList.add('open');
  overlay.classList.add('open');
}}

function closeDashboard() {{
  sidePanel.classList.remove('open');
  overlay.classList.remove('open');
  sidePanelFrame.src = 'about:blank';
  sidePanelCompare.classList.remove('open');
  sidePanelFrame.classList.remove('hidden');
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
    if (!cfg || !conc) return;
    const ev = eventData.event || {{}};
    if (compareMode || ev.shiftKey || ev.metaKey || ev.ctrlKey) pinRun(cfg, conc);
    else openDashboard(cfg, conc);
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

// ── Chart factory ──
function metricLabel(key) {{
  return METRIC_LABELS[key] || key.replace(/_/g, ' ');
}}

let gpuCostPerHour = 0;

function applyNorm(val, norm, meta) {{
  if (val == null) return null;
  if (norm.startsWith('cost') && (val <= 0 || gpuCostPerHour <= 0)) return null;
  if (val === 0) return null;
  if (norm === 'decode') return val / meta.decodeGPUs;
  if (norm === 'prefill') return val / meta.prefillGPUs;
  if (norm === 'total') return val / (meta.decodeGPUs + meta.prefillGPUs);
  if (norm === 'cost_decode') return (meta.decodeGPUs * gpuCostPerHour * 1e6) / (val * 3600);
  if (norm === 'cost_prefill') return (meta.prefillGPUs * gpuCostPerHour * 1e6) / (val * 3600);
  if (norm === 'cost_total') return ((meta.decodeGPUs + meta.prefillGPUs) * gpuCostPerHour * 1e6) / (val * 3600);
  return val;
}}

function normSuffix(norm) {{
  if (norm === 'decode') return ' / decode GPU';
  if (norm === 'prefill') return ' / prefill GPU';
  if (norm === 'total') return ' / total GPU';
  if (norm === 'cost_decode') return ' ($/M tokens, decode)';
  if (norm === 'cost_prefill') return ' ($/M tokens, prefill)';
  if (norm === 'cost_total') return ' ($/M tokens, total)';
  return '';
}}

function metricOptions(keys) {{
  return keys.filter(k => METRICS[k]).map(k => ({{
    value: k, text: metricLabel(k) + (METRICS[k].unit ? ` (${{METRICS[k].unit}})` : '')
  }}));
}}

function metricSample(metric) {{
  for (const cfg of CONFIG_KEYS) {{
    for (const c of CONCURRENCIES) {{
      const sample = DATA[cfg]?.[c]?.[metric];
      if (sample) return sample;
    }}
  }}
  return null;
}}

function statOptionsForMetric(metric) {{
  const sample = metricSample(metric);
  const keys = sample ? STAT_KEYS.filter(s => Object.prototype.hasOwnProperty.call(sample, s)) : ['avg'];
  return (keys.length ? keys : ['avg']).map(s => ({{ value: s, text: s }}));
}}

function normOptionsForMetric(metric, axis) {{
  const opts = [{{ value: 'none', text: 'none' }}];
  if (DECODE_NORMALIZED_METRICS.has(metric)) {{
    opts.push({{ value: 'decode', text: '/ decode GPUs' }});
    opts.push({{ value: 'total', text: '/ total GPUs' }});
    if (axis === 'y') opts.push({{ value: 'cost_decode', text: '$/M tok (decode GPUs)' }});
  }}
  if (PREFILL_NORMALIZED_METRICS.has(metric)) {{
    opts.push({{ value: 'prefill', text: '/ prefill GPUs' }});
    opts.push({{ value: 'total', text: '/ total GPUs' }});
    if (axis === 'y') opts.push({{ value: 'cost_prefill', text: '$/M tok (prefill GPUs)' }});
  }}
  if (TOTAL_NORMALIZED_METRICS.has(metric)) {{
    opts.push({{ value: 'total', text: '/ total GPUs' }});
    if (axis === 'y') opts.push({{ value: 'cost_total', text: '$/M tok (total GPUs)' }});
  }}
  return opts;
}}

function setSelectOptions(sel, options, value) {{
  sel.replaceChildren();
  options.forEach(o => {{
    const opt = document.createElement('option');
    opt.value = o.value;
    opt.textContent = o.text;
    sel.appendChild(opt);
  }});
  const values = new Set(options.map(o => o.value));
  sel.value = values.has(value) ? value : options[0]?.value;
  return sel.value;
}}

function hoverText(cfg, c, d) {{
  const meta = CONFIGS[cfg];
  const out = d.output_token_throughput;
  const itl = d.inter_token_latency;
  const otpu = d.output_token_throughput_per_user;
  const ttft = d.time_to_first_token;
  const norm = out ? (out.avg / meta.decodeGPUs).toFixed(1) : '?';
  return `<b>${{meta.label}} @ c${{C_LABELS[c]}}</b><br>` +
    `Output: ${{out?.avg?.toFixed(1) ?? '?'}} tok/s (${{norm}} tok/s/decode GPU)<br>` +
    `ITL p50: ${{itl?.p50?.toFixed(1) ?? '?'}} ms · p99: ${{itl?.p99?.toFixed(1) ?? '?'}} ms<br>` +
    `Per-user: ${{otpu?.avg?.toFixed(1) ?? '?'}} tok/s/user<br>` +
    `TTFT p50: ${{ttft ? (ttft.p50/1000).toFixed(1) : '?'}}s · p99: ${{ttft ? (ttft.p99/1000).toFixed(1) : '?'}}s`;
}}

const allCharts = [];

function createChart(container, defaults) {{
  const state = {{
    xMetric: defaults.xMetric || 'output_token_throughput_per_user',
    xStat: defaults.xStat || 'avg',
    xNorm: defaults.xNorm || 'none',
    yMetric: defaults.yMetric || 'output_token_throughput',
    yStat: defaults.yStat || 'avg',
    yNorm: defaults.yNorm || 'decode',
    el: null,
  }};

  // Controls
  const ctrl = document.createElement('div');
  ctrl.className = 'axis-controls';

  function mkSelect(label, options, value, onChange) {{
    const lbl = document.createElement('label');
    lbl.textContent = label;
    const sel = document.createElement('select');
    options.forEach(o => {{
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.text;
      sel.appendChild(opt);
    }});
    sel.value = value;
    sel.addEventListener('change', () => onChange(sel.value));
    lbl.appendChild(sel);
    ctrl.appendChild(lbl);
    return sel;
  }}

  const xMetricOpts = metricOptions(X_AXIS_METRICS);
  const yMetricOpts = metricOptions(Y_AXIS_METRICS);

  // X row
  const xDiv = document.createElement('div');
  xDiv.className = 'axis-controls';
  xDiv.style.borderBottom = '1px solid #2a2a2e';
  function mkSelIn(parent, label, options, value, onChange) {{
    const lbl = document.createElement('label');
    lbl.textContent = label;
    const sel = document.createElement('select');
    options.forEach(o => {{
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.text;
      sel.appendChild(opt);
    }});
    sel.value = value;
    sel.addEventListener('change', () => onChange(sel.value));
    lbl.appendChild(sel);
    parent.appendChild(lbl);
    return sel;
  }}

  const xMetricSel = mkSelIn(xDiv, 'X:', xMetricOpts, state.xMetric, v => {{
    state.xMetric = v;
    state.xStat = setSelectOptions(xStatSel, statOptionsForMetric(v), state.xStat);
    state.xNorm = setSelectOptions(xNormSel, normOptionsForMetric(v, 'x'), state.xNorm);
    update();
  }});
  const xStatSel = mkSelIn(xDiv, 'stat:', statOptionsForMetric(state.xMetric), state.xStat, v => {{ state.xStat = v; update(); }});
  const xNormSel = mkSelIn(xDiv, 'norm:', normOptionsForMetric(state.xMetric, 'x'), state.xNorm, v => {{ state.xNorm = v; update(); }});
  state.xMetric = xMetricSel.value;
  state.xStat = xStatSel.value;
  state.xNorm = xNormSel.value;
  container.appendChild(xDiv);

  // Y row
  const yDiv = document.createElement('div');
  yDiv.className = 'axis-controls';
  const yMetricSel = mkSelIn(yDiv, 'Y:', yMetricOpts, state.yMetric, v => {{
    state.yMetric = v;
    state.yStat = setSelectOptions(yStatSel, statOptionsForMetric(v), state.yStat);
    state.yNorm = setSelectOptions(yNormSel, normOptionsForMetric(v, 'y'), state.yNorm);
    update();
  }});
  const yStatSel = mkSelIn(yDiv, 'stat:', statOptionsForMetric(state.yMetric), state.yStat, v => {{ state.yStat = v; update(); }});
  const yNormSel = mkSelIn(yDiv, 'norm:', normOptionsForMetric(state.yMetric, 'y'), state.yNorm, v => {{ state.yNorm = v; update(); }});
  state.yMetric = yMetricSel.value;
  state.yStat = yStatSel.value;
  state.yNorm = yNormSel.value;
  container.appendChild(yDiv);

  // Panel
  const panel = document.createElement('div');
  panel.className = 'panel';
  panel.style.height = '500px';
  const plot = document.createElement('div');
  plot.className = 'plot';
  panel.appendChild(plot);
  container.appendChild(panel);
  new ResizeObserver(() => Plotly.Plots.resize(plot)).observe(panel);
  state.el = plot;

  function axisTitle(metric, stat, norm) {{
    const name = metricLabel(metric);
    const unit = METRICS[metric]?.unit || '';
    const ss = stat === 'avg' ? '' : ` (${{stat}})`;
    const ns = normSuffix(norm);
    if (norm.startsWith('cost')) return `${{name}}${{ss}}${{ns}}`;
    return `${{name}}${{ss}}${{ns}} (${{unit}})`;
  }}

  function buildTraces() {{
    return CONFIG_KEYS.map(cfg => {{
      const meta = CONFIGS[cfg];
      const validConcs = CONCURRENCIES.filter(c => DATA[cfg] && DATA[cfg][c]);
      return {{
        x: validConcs.map(c => {{
          const m = DATA[cfg][c][state.xMetric];
          return m ? applyNorm(m[state.xStat], state.xNorm, meta) : null;
        }}),
        y: validConcs.map(c => {{
          const m = DATA[cfg][c][state.yMetric];
          return m ? applyNorm(m[state.yStat], state.yNorm, meta) : null;
        }}),
        text: validConcs.map(c => hoverText(cfg, c, DATA[cfg][c])),
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

  function getLayout() {{
    const yAxis = {{ ...LAYOUT_DEFAULTS.yaxis, title: {{ text: axisTitle(state.yMetric, state.yStat, state.yNorm), font: {{ size: 11 }} }} }};
    if (!state.yNorm.startsWith('cost')) yAxis.rangemode = 'tozero';
    return {{
      ...LAYOUT_DEFAULTS,
      title: {{ text: `${{metricLabel(state.yMetric)}} vs ${{metricLabel(state.xMetric)}}`, font: {{ size: 13, color: '#d8d9da' }} }},
      xaxis: {{ ...LAYOUT_DEFAULTS.xaxis, title: {{ text: axisTitle(state.xMetric, state.xStat, state.xNorm), font: {{ size: 11 }} }} }},
      yaxis: yAxis,
      legend: {{ ...LAYOUT_DEFAULTS.legend, x: 0.99, y: 0.99, xanchor: 'right', yanchor: 'top' }},
    }};
  }}

  function update() {{
    const traces = buildTraces();
    if (state.el.data) {{
      state.el.data.forEach((old, i) => {{
        if (traces[i] && old.visible === 'legendonly') traces[i].visible = 'legendonly';
      }});
    }}
    Plotly.react(state.el, traces, getLayout(), {{ responsive: true, edits: {{ legendPosition: true }} }});
  }}

  Plotly.newPlot(state.el, buildTraces(), getLayout(), {{ responsive: true, edits: {{ legendPosition: true }} }});
  attachClickHandler(state.el);

  // Legend sync → table
  state.el.on('plotly_restyle', function() {{
    CONFIG_KEYS.forEach((cfg, i) => {{
      if (!state.el.data[i]) return;
      const vis = state.el.data[i].visible;
      const show = vis !== 'legendonly' && vis !== false;
      document.querySelectorAll(`tr[data-cfg="${{cfg}}"]`).forEach(r => {{
        r.style.display = show ? '' : 'none';
      }});
    }});
  }});

  const chart = {{ state, update }};
  allCharts.push(chart);
  return chart;
}}

// ── Top bar: hint + cost input ──
const topBar = document.createElement('div');
topBar.style.cssText = 'display:flex; justify-content:space-between; align-items:center; padding:12px 4px 8px; flex-wrap:wrap; gap:8px;';
const hint = document.createElement('span');
hint.style.cssText = 'color:#ffffff; font-size:15px;';
hint.textContent = 'Click any data point to open its Prometheus dashboard. Shift+click (or turn on Compare mode) to pin runs and overlay their dashboards.';
topBar.appendChild(hint);
const compareToggleBtn = document.createElement('button');
compareToggleBtn.id = 'compareToggle';
compareToggleBtn.className = 'pin-btn';
compareToggleBtn.textContent = 'Compare mode: off';
topBar.appendChild(compareToggleBtn);

root.appendChild(topBar);

const pinBarEl = document.createElement('div');
pinBarEl.id = 'pinBar';
pinBarEl.className = 'pin-bar';
pinBarEl.innerHTML = '<span class="pin-label">Pinned runs:</span><span id="pinChips" style="display:contents"></span>'
  + '<button class="pin-btn" id="pinOpen">Open comparison</button><button class="pin-btn" id="pinClear">Clear</button>';
root.appendChild(pinBarEl);
{COMPARE_JS}

// ── Charts: two side by side ──
const chartRow = document.createElement('div');
chartRow.className = 'chart-row';
root.appendChild(chartRow);

const chartCol1 = document.createElement('div');
chartCol1.className = 'chart-col';
const chartCol2 = document.createElement('div');
chartCol2.className = 'chart-col';
chartRow.appendChild(chartCol1);
chartRow.appendChild(chartCol2);

createChart(chartCol1, {{ xMetric: 'output_token_throughput_per_user', yMetric: 'output_token_throughput', yNorm: 'decode' }});
createChart(chartCol2, {{ xMetric: 'inter_token_latency', xStat: 'p99', yMetric: 'output_token_throughput', yNorm: 'decode' }});

// ── Cost input ──
const costWrap = document.createElement('label');
costWrap.style.cssText = 'color:#8e8e8e; font-size:13px; display:flex; align-items:center; gap:6px; padding:12px 4px 4px;';
costWrap.textContent = '$/node/hr:';
const costInput = document.createElement('input');
costInput.type = 'number';
costInput.step = '0.01';
costInput.min = '0';
costInput.value = '21.68';
costInput.placeholder = '21.68';
costInput.style.cssText = 'background:#181b1f; color:#d8d9da; border:1px solid #2a2a2e; border-radius:4px; padding:4px 8px; font-size:12px; width:80px; font-family:inherit;';
costInput.addEventListener('input', () => {{
  gpuCostPerHour = (parseFloat(costInput.value) || 0) / {GPUS_PER_NODE};
  allCharts.forEach(ch => {{
    if (ch.state.xNorm.startsWith('cost') || ch.state.yNorm.startsWith('cost')) ch.update();
  }});
}});
gpuCostPerHour = (parseFloat(costInput.value) || 0) / {GPUS_PER_NODE};
costWrap.appendChild(costInput);
root.appendChild(costWrap);

// ── Section 2: Sortable summary table ──
const sec2Hdr = document.createElement('div');
sec2Hdr.className = 'row-header';
sec2Hdr.innerHTML = '<span class="arrow">&#9660;</span> Data Summary';
const sec2Wrap = document.createElement('div');
sec2Wrap.id = 'sec-table';
sec2Hdr.addEventListener('click', () => {{ sec2Hdr.classList.toggle('collapsed'); sec2Wrap.classList.toggle('hidden'); }});
root.appendChild(sec2Hdr);
root.appendChild(sec2Wrap);

(function() {{
  const div = document.createElement('div');
  div.className = 'summary';
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const tbody = document.createElement('tbody');
  table.appendChild(thead);
  table.appendChild(tbody);

  const colDefs = [
    'Config', 'Concurrency', 'Prefill GPUs', 'Decode GPUs',
    'Prefill KV Cache (tokens)', 'Decode KV Cache (tokens)',
    'Output tok/s', 'Input tok/s', 'Total tok/s',
    'Output tok/s/GPU', 'Input tok/s/GPU', 'Total tok/s/GPU',
    'ITL p50 (ms)', 'ITL p99 (ms)', 'Per-user tok/s',
    'TTFT p50 (s)', 'TTFT p99 (s)',
    '$/M input', '$/M output',
  ];
  const headerRow = document.createElement('tr');
  colDefs.forEach((label, i) => {{
    const th = document.createElement('th');
    th.textContent = label;
    th.dataset.col = i;
    th.dataset.label = label;
    th.addEventListener('click', () => sortTableBy(i, th));
    headerRow.appendChild(th);
  }});
  thead.appendChild(headerRow);

  const costCells = [];

  for (const cfg of CONFIG_KEYS) {{
    const meta = CONFIGS[cfg];
    const totalGPUs = meta.decodeGPUs + meta.prefillGPUs;
    for (const c of CONCURRENCIES) {{
      if (!DATA[cfg] || !DATA[cfg][c]) continue;
      const d = DATA[cfg][c];
      const out = d.output_token_throughput;
      const inp = d.input_token_throughput;
      const total = d.total_token_throughput;
      const itl = d.inter_token_latency;
      const otpu = d.output_token_throughput_per_user;
      const ttft = d.time_to_first_token;
      const outPerGpu = out ? (out.avg / meta.decodeGPUs).toFixed(1) : '-';
      const inpPerGpu = inp ? (inp.avg / meta.prefillGPUs).toFixed(1) : '-';
      const totalTps = total?.avg ?? ((out?.avg ?? 0) + (inp?.avg ?? 0));
      const totalPerGpu = totalTps > 0 ? (totalTps / totalGPUs).toFixed(1) : '-';

      const tr = document.createElement('tr');
      tr.dataset.cfg = cfg;
      tr.dataset.conc = c;

      const kvCache = d._kv_cache_tokens;
      const prefillKvStr = kvCache?.prefill != null ? kvCache.prefill.toLocaleString() : '-';
      const decodeKvStr = kvCache?.decode != null ? kvCache.decode.toLocaleString() : '-';

      const vals = [
        meta.label, C_LABELS[c], meta.prefillGPUs, meta.decodeGPUs,
        prefillKvStr, decodeKvStr,
        out?.avg?.toFixed(1) ?? '-', inp?.avg?.toFixed(1) ?? '-', totalTps > 0 ? totalTps.toFixed(1) : '-',
        outPerGpu, inpPerGpu, totalPerGpu,
        itl?.p50?.toFixed(1) ?? '-', itl?.p99?.toFixed(1) ?? '-',
        otpu?.avg?.toFixed(1) ?? '-',
        ttft ? (ttft.p50/1000).toFixed(1) : '-',
        ttft ? (ttft.p99/1000).toFixed(1) : '-',
        '-', '-',
      ];
      vals.forEach((v, i) => {{
        const td = document.createElement('td');
        td.textContent = v;
        if (i === 0) {{ td.style.color = COLORS[cfg]; td.style.fontWeight = '500'; }}
        if (i >= 9 && i <= 11) td.className = 'highlight';
        tr.appendChild(td);
      }});

      const ci = colDefs.length;
      costCells.push({{
        inpTd: tr.cells[ci-2],
        outTd: tr.cells[ci-1],
        inpTps: inp?.avg ?? 0,
        outTps: out?.avg ?? 0,
        prefillGPUs: meta.prefillGPUs,
        decodeGPUs: meta.decodeGPUs,
      }});

      tbody.appendChild(tr);
    }}
  }}

  function costPerMTok(gpus, tps) {{
    return (gpuCostPerHour > 0 && tps > 0) ? ((gpus * gpuCostPerHour * 1e6) / (tps * 3600)).toFixed(2) : '-';
  }}
  function updateCostCols() {{
    costCells.forEach(cc => {{
      cc.inpTd.textContent = costPerMTok(cc.prefillGPUs, cc.inpTps);
      cc.outTd.textContent = costPerMTok(cc.decodeGPUs, cc.outTps);
    }});
  }}
  updateCostCols();
  costInput.addEventListener('input', updateCostCols);

  let sortCol = -1, sortAsc = true;
  function sortTableBy(col, th) {{
    if (sortCol === col) sortAsc = !sortAsc;
    else {{ sortCol = col; sortAsc = true; }}

    thead.querySelectorAll('th').forEach(h => h.textContent = h.dataset.label);
    th.textContent = th.dataset.label + (sortAsc ? ' \\u25B2' : ' \\u25BC');

    const cfgOrder = {{}};
    CONFIG_KEYS.forEach((k, i) => cfgOrder[k] = i);
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a, b) => {{
      const va = parseFloat(a.cells[col]?.textContent) || 0;
      const vb = parseFloat(b.cells[col]?.textContent) || 0;
      const diff = sortAsc ? va - vb : vb - va;
      if (diff !== 0) return diff;
      return cfgOrder[a.dataset.cfg] - cfgOrder[b.dataset.cfg];
    }});
    rows.forEach(r => tbody.appendChild(r));
  }}

  div.appendChild(table);
  sec2Wrap.appendChild(div);
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

    configs, metric_units = discover_configs(results_dir)
    if not configs:
        print(f"Error: no results_<config>/ directories found in {results_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.abspath(results_dir)
    output_path = os.path.join(output_dir, 'interactivity_vs_throughput.html')

    generate_html(configs, output_path, results_dir, metric_units)

    print(f"Discovered {len(configs)} configs:")
    for cfg, meta in sorted(configs.items()):
        concs = sorted(meta['runs'].keys())
        print(f"  {cfg}: {meta['label']} ({meta['decode_gpus']} decode GPUs) — c{',c'.join(str(c) for c in concs)}")
    print(f"\nGenerated: {output_path}")


if __name__ == '__main__':
    main()
