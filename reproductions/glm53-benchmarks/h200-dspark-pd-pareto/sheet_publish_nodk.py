"""Add a "No-direct-KV lineage" tab to the H200 prefiller comparison sheet.

usage: python3 sheet_publish_nodk.py <spreadsheetId>

Covers the 2026-09-09 work: dropping PCP direct KV, pricing PR 54036, and the
three PCP-spanning-DCP transports. The two 2026-09-09 arms are read live from
points.csv; the rest are transcribed, because their raw sweeps were purged on
2026-09-09 (see the provenance block at the bottom of the tab).
"""
import csv, json, subprocess, sys
from pathlib import Path

SID = sys.argv[1]
here = Path(__file__).resolve().parent
ONE = subprocess.check_output(["bash", "-lc", "echo $(npm prefix -g)/bin/one"], text=True).strip()
CONN = "live::google-sheets::default::8800062aa7ab496594cc206c36ee3f66"
VALUES = "conn_mod_def::GJ30k7Vqavo::zEU1ntnYTCiWrupKRe1Pig"
FORMAT = "conn_mod_def::GJ30jCATCJk::uk1gxM57RXy-ciDvxCQvQQ"
TAB = "No-direct-KV lineage (2026-09-09)"
CS = [1, 2, 4, 8]

live = {}
for r in csv.DictReader(open(here / "points.csv")):
    live[(r["series"], int(r["c"]))] = r
NODK = "PCP8+EP8 (DCP1) -> TP8+EP8 (no-54036 20a94597)"
SIMP = "PCP8+DCP8+EP8 -> TP8+EP8 (token-sharded simplify a6f09074)"
g = lambda s, c, k: live[(s, c)][k] if (s, c) in live else ""

def sect(title, header, body):
    return [[title], header] + body + [[]]

rows = [
    ["GLM-5.3 DSpark7 P/D on CoreWeave H200: removing PCP direct KV, and what it costs"],
    ["Follow-on to the 2026-09-08 tabs. Same workload (LMSYS OpenHands via evalscope, 220 output tokens, ~80K input, 7 turns/req), same TP8+EP8 DSpark7 decoder, 16 H200s per P/D arm."],
    ["Question: PCP direct KV (#52863) publishes every KV row into all peers' symmetric-memory caches. Can it be dropped for plain collectives, and what breaks?"],
    [],
]

rows += sect(
    "1. PCP8 + DCP1 - dropping direct KV is free",
    ["Mean TTFT (ms)", "c1", "c2", "c4", "c8", "branch"],
    [
        ["direct KV (baseline)", 804, 971, 7379, 15616, "frankenstein-prefiller-clean @ 95eb419caf"],
        ["collectives", 862, 976, 7431, 16045, "frankenstein-prefiller-collectives @ cd6953f849"],
        ["no direct KV (productized)", 870, 1062, 7530, 15968, "frankenstein-prefiller-no-direct-kv @ 1ca5131a07"],
    ])
rows += sect(
    "   Generation throughput (output tok/s), same three arms",
    ["Arm", "c1", "c2", "c4", "c8", ""],
    [
        ["direct KV (baseline)", 101.2, 188.2, 100.5, 102.6, ""],
        ["collectives", 103.8, 190.4, 99.0, 100.0, ""],
        ["no direct KV (productized)", 107.8, 192.9, 100.0, 99.9, ""],
    ])
rows += [["All three are statistically tied; run-to-run spread between branches that should be identical reached ~6% at c1. Draft acceptance 82-88% on every point. There is no performance reason to keep direct KV at DCP1."], []]

rows += sect(
    "2. Pricing PR 54036 (context-KV projection sharding)",
    ["Arm", "c1", "c2", "c4", "c8", "metric"],
    [
        ["no direct KV (with 54036)", 870.3, 1062.2, 7529.6, 15967.6, "mean TTFT (ms)"],
        ["same, PR 54036 backed out", g(NODK,1,"ttft_ms"), g(NODK,2,"ttft_ms"), g(NODK,4,"ttft_ms"), g(NODK,8,"ttft_ms"), "mean TTFT (ms)"],
        ["no direct KV (with 54036)", 107.83, 192.86, 100.01, 99.88, "generation tok/s"],
        ["same, PR 54036 backed out", g(NODK,1,"output_tok_s"), g(NODK,2,"output_tok_s"), g(NODK,4,"output_tok_s"), g(NODK,8,"output_tok_s"), "generation tok/s"],
    ])
rows += [["Backing 54036 out costs 0.9-3.6% generation throughput and up to +3.3% TTFT, consistent in direction across all four concurrencies; acceptance unchanged. Identical request counts (52/104/104/208) and 100% success on all eight points."],
         ["LOWER BOUND, not the PR's full value: the no-direct-KV branch's restore_context already un-shards the projection, so every PCP rank projects the full global context and writes only its rows via PAD_SLOT_ID. Against upstream without that change, 54036 is worth more."], []]

rows += sect(
    "3. PCP-spanning DCP (DCP8 = PCP8) - this is where direct KV actually matters",
    ["Transport", "c1", "c2", "c4", "c8", "requires direct KV?"],
    [
        ["peer gather (reads peer KV via symmetric memory)", 916, 978, 862, 2041, "YES"],
        ["token-sharded over NCCL", 1708, 1701, 968, 5591, "no"],
        ["token-sharded over symmetric staging", "12036 (invalid)", 3542, 572, 14378, "no"],
        ["token-sharded, simplify branch", g(SIMP,1,"ttft_ms"), g(SIMP,2,"ttft_ms"), g(SIMP,4,"ttft_ms"), g(SIMP,8,"ttft_ms"), "no"],
    ])
rows += sect(
    "   Generation throughput (output tok/s), same transports",
    ["Transport", "c1", "c2", "c4", "c8", ""],
    [
        ["peer gather", 108.1, 188.5, 369.8, 425.1, ""],
        ["token-sharded over NCCL", 76.6, 143.5, 372.1, 242.5, ""],
        ["token-sharded over symmetric staging", "16.3 (invalid)", 87.3, 363.9, 100.6, ""],
        ["token-sharded, simplify branch", g(SIMP,1,"output_tok_s"), g(SIMP,2,"output_tok_s"), g(SIMP,4,"output_tok_s"), g(SIMP,8,"output_tok_s"), ""],
    ])
rows += [
    ["Peer gather wins decisively at c8: 2041 ms / 425 tok/s vs 5591 / 242 for NCCL. It structurally requires direct KV - it reads context KV straight out of the DCP owners' symmetric-memory caches."],
    ["Acceptance stayed 80.9-84.1% on every transport, so all three are numerically correct; the gap is transport cost, not a correctness bug."],
    ["The symmetric-staging c1 point is NOT a measurement: 6 requests, 4 failures, 27 s, vs 52 requests in the comparison arms. Its 12,036 ms TTFT and 76.8% acceptance are startup artifacts."],
    ["Symmetric staging was dead code on the regroup branch (unreachable at DCP==PCP) and had never been benchmarked. These numbers measure one mechanical port, not the idea's ceiling."],
    ["Upstream implements the token-sharded exchange only for FlashInfer sparse MLA (SM100); on SM90 the FlashMLA constructor raises unless peer gather is available. The collectives / no-direct-KV branches removed that guard, which is why they can run DCP8 at all - and why they run it slowly."],
    [],
    ["BOTTOM LINE: dropping PCP direct KV is free at DCP1 and costs roughly 2x at DCP8 on H200. 'Drop direct KV' and 'keep the fast DCP8 path' are currently mutually exclusive."],
    [],
]

rows += sect(
    "4. Provenance and caveats",
    ["Item", "Value", "", "", "", ""],
    [
        ["Cluster", "CoreWeave H200 (coreweave-piggy, ns lwilkinson-dev), SM90", "", "", "", ""],
        ["Live from points.csv", "the PR-54036 backout arm (sweep 20260909T140000Z-no54036-20a94597) and the simplify DCP8 arm (20260909T150000Z-simplify-a6f09074)", "", "", "", ""],
        ["Transcribed", "all other rows. Their raw sweep directories were deleted on 2026-09-09 during a disk cleanup; points.csv and this sheet are now the surviving record.", "", "", "", ""],
        ["Peer gather verification", "needle probes at 19K / 39K / 98K tokens after the sparse-utils block-bound fix 11c47809ab, plus a full c1-c8 sweep with 0 failed points", "", "", "", ""],
        ["Untested variant", "229ff1766d makes peer gather the ONLY PCP-spanning DCP path and deletes the 305-line token-sharded fallback. Never re-run on hardware.", "", "", "", ""],
        ["c8 acceptance, simplify arm", "evalscope reported 90.8%, which is an estimator artifact: 10.88 decoded tok/iter exceeds the 8-token ceiling for 7 speculative tokens (chunk coalescing).", "", "", "", ""],
        ["Charts", "results/.artifacts/plots/h200-dspark-pd/h200-glm53-dspark7-pd-pareto.png (6 series, rebuilt 2026-09-09)", "", "", "", ""],
        ["Generated", "2026-09-09 by Claude Code via sheet_publish_nodk.py", "", "", "", ""],
    ])

# 1. create the tab (idempotent: delete an existing one with the same title first)
meta = subprocess.run([ONE, "--agent", "actions", "execute", "google-sheets",
                       "conn_mod_def::GJ30jpJCuBA::-7kldtebSUeO7_FYtT48JQ", CONN,
                       "--path-vars", json.dumps({"spreadsheetId": SID})], capture_output=True, text=True)
sheets = json.loads(meta.stdout)["response"]["sheets"]
existing = next((s["properties"]["sheetId"] for s in sheets if s["properties"]["title"] == TAB), None)
reqs = ([{"deleteSheet": {"sheetId": existing}}] if existing is not None else []) + [
    {"addSheet": {"properties": {"title": TAB, "gridProperties": {"rowCount": 200, "columnCount": 12}}}}]
out = subprocess.run([ONE, "--agent", "actions", "execute", "google-sheets", FORMAT, CONN,
                      "--path-vars", json.dumps({"spreadsheetId": SID}),
                      "-d", json.dumps({"requests": reqs})], capture_output=True, text=True)
resp = json.loads(out.stdout)
if resp.get("error"):
    sys.exit(f"addSheet failed: {resp['error']}")
new_id = [r for r in resp["response"]["replies"] if "addSheet" in r][0]["addSheet"]["properties"]["sheetId"]
print("tab:", TAB, "sheetId", new_id)

# 2. values
out = subprocess.run([ONE, "--agent", "actions", "execute", "google-sheets", VALUES, CONN,
                      "--path-vars", json.dumps({"spreadsheetId": SID}),
                      "-d", json.dumps({"valueInputOption": "RAW",
                                        "data": [{"range": f"'{TAB}'!A1", "majorDimension": "ROWS", "values": rows}]})],
                     capture_output=True, text=True)
resp = json.loads(out.stdout)
print("values:", resp.get("error") or {k: resp["response"].get(k) for k in ("totalUpdatedCells",)})

# 3. formatting
bold = lambda r0, r1, c0=0, c1=6: {"repeatCell": {"range": {"sheetId": new_id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1},
                                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}}
freqs = [bold(0, 1, 0, 1)]
for i, row in enumerate(rows):
    if len(row) == 1 and row[0] and i > 2 and not row[0].startswith(("All three", "Backing", "LOWER", "Peer gather w", "Acceptance", "The symmetric", "Symmetric", "Upstream", "BOTTOM")):
        freqs.append(bold(i, i + 1, 0, 1))
    if row and row[0] in ("Mean TTFT (ms)", "Arm", "Transport", "Item"):
        freqs.append(bold(i, i + 1, 0, 6))
    if row and row[0] == "BOTTOM LINE: dropping PCP direct KV is free at DCP1 and costs roughly 2x at DCP8 on H200. 'Drop direct KV' and 'keep the fast DCP8 path' are currently mutually exclusive.":
        freqs.append(bold(i, i + 1, 0, 1))
freqs += [
    {"updateDimensionProperties": {"range": {"sheetId": new_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 330}, "fields": "pixelSize"}},
    {"updateDimensionProperties": {"range": {"sheetId": new_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 5}, "properties": {"pixelSize": 110}, "fields": "pixelSize"}},
    {"updateDimensionProperties": {"range": {"sheetId": new_id, "dimension": "COLUMNS", "startIndex": 5, "endIndex": 6}, "properties": {"pixelSize": 400}, "fields": "pixelSize"}},
]
out = subprocess.run([ONE, "--agent", "actions", "execute", "google-sheets", FORMAT, CONN,
                      "--path-vars", json.dumps({"spreadsheetId": SID}),
                      "-d", json.dumps({"requests": freqs})], capture_output=True, text=True)
resp = json.loads(out.stdout)
print("format:", resp.get("error") or f"{len(resp['response'].get('replies', []))} replies")
print(f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid={new_id}")
