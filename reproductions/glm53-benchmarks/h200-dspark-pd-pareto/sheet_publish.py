"""Publish the H200 prefiller comparison to a Google Sheet via the One CLI.

usage: python3 sheet_publish.py <spreadsheetId>
Reads points.csv (collect_points.py) and each point's benchmark_percentile.json for P99 TTFT.
Tabs: "Prefiller comparison" (sheetId 0), "All points" (1), "Notes" (2), created beforehand.
"""
import csv, glob, json, pathlib, subprocess, sys
from pathlib import Path

SID = sys.argv[1]
here = Path(__file__).resolve().parent
root = pathlib.Path("results/.artifacts")  # sweep dirs, relative to repo root
ONE = subprocess.check_output(["bash", "-lc", "echo $(npm prefix -g)/bin/one"], text=True).strip()
CONN = "live::google-sheets::default::8800062aa7ab496594cc206c36ee3f66"
VALUES = "conn_mod_def::GJ30k7Vqavo::zEU1ntnYTCiWrupKRe1Pig"
FORMAT = "conn_mod_def::GJ30jCATCJk::uk1gxM57RXy-ciDvxCQvQQ"

SERIES = {  # points.csv label -> (short name, arm dir, sweep dir, points subdir)
    "PCP8+DCP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": ("PCP8+DCP8+EP8 prefiller", "clean-pcp8-dcp8-ep8-dspark-tp8", "20260908T133000Z-95eb419caf", "points"),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (fixed 95eb419caf)": ("PCP8+EP8 prefiller (DCP1, replicated KV)", "clean-pcp8-ep8-dspark-tp8", "20260908T133000Z-95eb419caf", "points"),
    "TP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": ("TP8+EP8 prefiller", "clean-tp8-ep8-dspark-tp8", "20260908T133000Z-95eb419caf", "points"),
    "TP8+EP8 decoder-local (pre-fix)": ("no prefiller (TP8+EP8 decoder prefills itself, 8 GPUs)", "clean-pcp8-ep8-dspark-tp8", "20260907T233000Z-b9914a14b8", "points-decoder-local"),
}
MATRIX = root / "reproductions/glm53-frankenstein-clean-dspark-matrix-h200"


def p99_ttft(sweep, arm, sub, point_ts):
    for f in glob.glob(str(MATRIX / sweep / arm / sub / f"*{point_ts}*/*/benchmark_percentile.json")):
        for row in json.load(open(f)):
            if row.get("Percentiles") == "99%":
                return row["TTFT (ms)"]
    return ""


rows = [r for r in csv.DictReader(open(here / "points.csv")) if r["series"] in SERIES]
for r in rows:
    short, arm, sweep, sub = SERIES[r["series"]]
    r["short"] = short
    r["p99_ttft_ms"] = p99_ttft(sweep, arm, sub, r["point"])
order = list(SERIES.values())
by = {s[0]: {} for s in order}
for r in rows:
    by[r["short"]][int(r["c"])] = r
CS = [1, 2, 4, 8]


def table(title, key, fmt):
    out = [[title], ["Prefiller"] + [f"c{c}" for c in CS]]
    for short, *_ in order:
        line = [short]
        for c in CS:
            r = by[short].get(c)
            line.append(fmt(r[key]) if r and r.get(key) not in ("", None) else "")
        out.append(line)
    out.append([])
    return out


f0 = lambda v: round(float(v))
f1 = lambda v: round(float(v), 1)
f2 = lambda v: round(float(v), 2)
pct = lambda v: round(100 * float(v), 1)

cmp_rows = [
    ["GLM-5.3 DSpark7 P/D on CoreWeave H200: prefiller layouts in front of the same TP8+EP8 DSpark7 decoder"],
    ["LMSYS OpenHands agentic traces (evalscope, 220 output tokens, ~80K input tokens, 7 turns/request); fork LucasWilkinson/vllm:frankenstein-prefiller-clean@95eb419caf; P/D arms use 16 H200s (8 prefill + 8 decode); cX = X concurrent conversations"],
    [],
]
for r in rows:
    r["rps"] = float(r["requests"]) / float(r["duration_s"])
cmp_rows += table("Mean TTFT (ms)", "ttft_ms", f0)
cmp_rows += table("P99 TTFT (ms)", "p99_ttft_ms", f0)
cmp_rows += table("Sustained request rate (completed req/s)", "rps", f2)
cmp_rows += table("Generation throughput (output tok/s)", "output_tok_s", f1)
cmp_rows += table("Mean TPOT (ms)", "tpot_ms", f1)
cmp_rows += table("Logical total-token throughput per GPU (tok/s)", "total_tok_s_per_gpu", f0)
cmp_rows += table("Decoded tokens per decoder iteration (DSpark7)", "decoded_tok_iter", f2)
cmp_rows += table("Draft acceptance rate, evalscope-reported (%)", "spec_accept", pct)
cmp_rows += [
    ["Reading the numbers"],
    ["PCP8 (either DCP setting) beats the TP8+EP8 prefiller at every concurrency: 1.5-2x lower TTFT at c1/c2 before any queueing, 4x at c4 and 40x at c8 once the TP8 prefiller saturates."],
    ["The TP8+EP8 c8 collapse is prefill compute, not memory: its KV pool stayed at 12-29% with no preemption while 6-7 requests queued behind 1-2 running; the TP8 sparse prefiller sustained a median ~8K prompt tok/s per 10 s window (max 15.5K) vs the DCP8 prefiller's 24K peaks with a mostly idle queue."],
    ["The PCP8 DCP1 arm has a different bottleneck (its replicated KV pool, 1x at 142K per rank): it holds at c2 and collapses at c4. PCP8+DCP8 is the layout to quote; it is the only one whose TTFT stays near 1 s through c4."],
    ["Decode-side metrics (TPOT, acceptance, tok/iter) are the same across arms because the decoder is identical; TEP8's low c8 TPOT only reflects an almost idle decoder."],
    ["The TP8+EP8 prefiller needs gpu_memory_utilization 0.85 (not the decoder's 0.92): with 8 MLA heads per rank the FP8 sparse kernel pads heads 8 -> 64 and allocates a 16384 x 64 x 2048 x 2 B = 4 GiB tensor per rank on the first request, ~14 GiB over the profiled peak. The 0.92 deploy crash-looped twice."],
]

cols = ["series", "short", "gpus", "c", "point", "requests", "duration_s", "ttft_ms", "p99_ttft_ms", "tpot_ms",
        "interactivity", "output_tok_s", "total_tok_s", "total_tok_s_per_gpu", "output_tok_s_per_gpu", "decoded_tok_iter", "spec_accept"]
all_rows = [cols] + [[r.get(k, "") for k in cols] for r in sorted(rows, key=lambda r: (order.index(SERIES[r["series"]]), int(r["c"])))]

notes = [
    ["Item", "Value"],
    ["Cluster", "CoreWeave H200 (coreweave-piggy, ns lwilkinson-dev), 4 nodes x 8 H200 (SM90)"],
    ["vLLM", "LucasWilkinson/vllm:frankenstein-prefiller-clean @ 95eb419caf (fixes dfcaccae07 + 0e591cadf8 for the P/D draft-acceptance gap)"],
    ["Verifier / draft", "zai-org/GLM-5.3 @ 30333038 / RedHatAI/GLM-5.2-speculator.dspark @ cc714308, DSpark7 fixed (no adaptive verification on SM90), FP8 KV, max_model_len 142000"],
    ["Decoder (all arms)", "TP8+EP8 DSpark7, gpu_memory_utilization 0.92, UCX_TLS=rc,cuda_copy,cuda_ipc,self,sm,tcp (rc_gda read stall workaround)"],
    ["Prefiller: PCP8+DCP8+EP8", "spec p1-pcp8dcp8ep-d1-tp8ep-dspark7-frankenstein-clean-agentx.yaml, gmu 0.90, mnbt 16384, PCP direct-KV sharded"],
    ["Prefiller: PCP8+EP8 (DCP1)", "spec p1-pcp8ep-d1-tp8ep-dspark7-frankenstein-clean-agentx.yaml, gmu 0.94, mnbt 16384, PCP direct-KV replicated"],
    ["Prefiller: TP8+EP8", "spec p1-tp8ep-d1-tp8ep-dspark7-frankenstein-clean-agentx.yaml, gmu 0.85 (0.92 OOMs, see comparison tab), mnbt 16384"],
    ["Workload", "scripts/lmsys-run.sh, LMSYS OpenHands, c1/c2/c4/c8 = 4/8/8/16 conversations, 220 output tokens"],
    ["Sweep dir", "results/.artifacts/reproductions/glm53-frankenstein-clean-dspark-matrix-h200/20260908T133000Z-95eb419caf/<arm>/points/"],
    ["Decoder-local point", "20260907T233000Z-b9914a14b8/clean-pcp8-ep8-dspark-tp8/points-decoder-local (c1 only, 8 GPUs; unaffected by the P/D fix)"],
    ["Charts", "results/.artifacts/plots/h200-dspark-pd/h200-glm53-dspark7-prefiller-ttft.png and h200-glm53-dspark7-pd-pareto.png"],
    ["Write-up", "reproductions/glm53-frankenstein-clean-dspark/README.md, sections '2026-09-08 (afternoon)' and '2026-09-08 (evening)'"],
    ["Invalid data", "clean-tp8-ep8-dspark-tp8/invalid-gmu092-prefill-oom/: c1 4/4 failed, c2/c4 served by the decoder alone while the prefiller crash-looped (not P/D numbers)"],
    ["Transfer health", "0 NIXL failures / stalls on all 12 P/D points; decoder handshake plans verified (TEP8: tp 8 -> 8 trivial; PCP arms: head-replicated row-split reads)"],
    ["Generated", "2026-09-08 by Claude Code from points.csv (collect_points.py) via sheet_publish.py"],
]

data = [
    {"range": "'Prefiller comparison'!A1", "majorDimension": "ROWS", "values": cmp_rows},
    {"range": "'All points'!A1", "majorDimension": "ROWS", "values": all_rows},
    {"range": "'Notes'!A1", "majorDimension": "ROWS", "values": notes},
]
out = subprocess.run([ONE, "--agent", "actions", "execute", "google-sheets", VALUES, CONN,
                      "--path-vars", json.dumps({"spreadsheetId": SID}),
                      "-d", json.dumps({"valueInputOption": "RAW", "data": data})], capture_output=True, text=True)
resp = json.loads(out.stdout)
print("values:", resp.get("error") or {k: resp["response"].get(k) for k in ("totalUpdatedCells", "totalUpdatedSheets")})

# formatting: bold title/table headers, freeze header rows, autosize columns
bold = lambda sid, r0, r1, c0=0, c1=6: {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1},
                                          "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}}
reqs = [bold(0, 0, 1, 0, 1)]
for i, row in enumerate(cmp_rows):
    if len(row) == 1 and row[0] and i > 2:  # section titles
        reqs.append(bold(0, i, i + 1, 0, 1))
    if row[:1] == ["Prefiller"]:
        reqs.append(bold(0, i, i + 1, 0, 5))
reqs += [
    bold(1, 0, 1, 0, len(cols)),
    bold(2, 0, 1, 0, 2),
    {"updateSheetProperties": {"properties": {"sheetId": 1, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
    {"updateSheetProperties": {"properties": {"sheetId": 2, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
    {"updateDimensionProperties": {"range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 380}, "fields": "pixelSize"}},
    {"autoResizeDimensions": {"dimensions": {"sheetId": 1, "dimension": "COLUMNS", "startIndex": 0, "endIndex": len(cols)}}},
    {"updateDimensionProperties": {"range": {"sheetId": 2, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 220}, "fields": "pixelSize"}},
    {"updateDimensionProperties": {"range": {"sheetId": 2, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 900}, "fields": "pixelSize"}},
]
out = subprocess.run([ONE, "--agent", "actions", "execute", "google-sheets", FORMAT, CONN,
                      "--path-vars", json.dumps({"spreadsheetId": SID}),
                      "-d", json.dumps({"requests": reqs})], capture_output=True, text=True)
resp = json.loads(out.stdout)
print("format:", resp.get("error") or f"{len(resp['response'].get('replies', []))} replies")
print(f"https://docs.google.com/spreadsheets/d/{SID}/edit")
