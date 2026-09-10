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
NODKRE = "PCP8+EP8 (DCP1) -> TP8+EP8 (no direct KV 1ca5131a07)"
CGN = "PCP8+EP8 (DCP1) -> TP8+EP8 (no direct KV, prefill cudagraph NONE)"
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
        ["no direct KV, re-measured 2026-09-09", g(NODKRE,1,"ttft_ms"), g(NODKRE,2,"ttft_ms"), g(NODKRE,4,"ttft_ms"), g(NODKRE,8,"ttft_ms"), "same branch, same env, same commit - re-run hours later"],
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
    "1b. Measured noise floor - the same commit, run twice, hours apart",
    ["Concurrency", "first run (ms)", "re-run (ms)", "delta", "", "what this qualifies"],
    [
        ["c1", 870.3, g(NODKRE,1,"ttft_ms"), "-11.3%", "", "PR 54036's c1 delta (0.2%) is inside the noise"],
        ["c2", 1062.2, g(NODKRE,2,"ttft_ms"), "-11.8%", "", "PR 54036's c2 delta (1.4%) is inside the noise"],
        ["c4", 7529.6, g(NODKRE,4,"ttft_ms"), "+0.1%", "", "c4/c8 are stable - this is where signal lives"],
        ["c8", 15967.6, g(NODKRE,8,"ttft_ms"), "-0.6%", "", "PR 54036's c4/c8 deltas (up to 3.6%) are real"],
    ])
rows += [["The re-run was forced: the original nodk sweep was deleted before its TPOT and total-token columns were transcribed, so the arm could not be plotted. Re-measuring it also produced this noise floor, which is firmer than the ~6% previously assumed."],
         ["Low concurrency swings ~11% run-to-run; c4/c8 hold under 1%. Treat any sub-5% claim at c1/c2 as unmeasured."], []]

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
    "4. Can upstream PR 56107 replace the branch's spec-decode commits? (2026-09-10)",
    ["Branch commit", "Covered by #56107?", "", "", "", "Note"],
    [
        ["#53661 part 1/3 (63633f85dd)", "YES", "", "", "", "56107 is a consolidation of 53653 + 53660 on current main"],
        ["#53661 part 2/3 (18bbbdadf3)", "YES", "", "", "", "53661 itself is closed; 56107 is the live replacement"],
        ["replicated drafter 1/2 (8a73c079cf)", "YES", "", "", "", "56107 scopes PCP checks to target layers in one argument"],
        ["replicated drafter 2/2 (cb06388afd)", "YES", "", "", "", "56107 drops req_states from PCPManager and never samples local logits - cleaner than what it replaces"],
        ["#54036 (4437bc5c41)", "no", "", "", "", "Structurally incompatible: 56107 restores aux hidden states to global order and runs the drafter fully replicated, so the sharded precompute does not exist there"],
        ["spanning-group DCP prefill (5e2cd71ae0)", "no", "", "", "", "Stays on the branch"],
        ["NIXL PCP-to-TP (089dc73cd8)", "no", "", "", "", "Stays on the branch; this is the real porting cost"],
    ])
rows += [
    ["Since the branch was cut, 7 of its 11 upstream PRs merged into main: 52783, 53311, 53682, 53955, 53869, 53515, 50611. The branch carries commits main already has."],
    ["TWO LIMITS IN 56107 AS WRITTEN. (a) Its validate_config rejects any speculative config under PCP unless cudagraph_mode is NONE, and every frankenstein prefiller spec runs PIECEWISE. (b) It permits DCP=PCP, but main has no token-sharded sparse MLA prefill on either backend, so in practice it covers DCP1 only."],
    ["The cudagraph guard looks over-broad rather than load-bearing: it was introduced by 4d675de05f ('Harden PCP MTP admission and buffers'), which lands BEFORE df3e1678bd ('Add replicated PCP DSpark groundwork') in the PR's own commit order, and its message still reads 'MRV2 PCP with MTP does not support CUDA graphs yet' while firing for DSpark. Narrowing it to the MTP branch is a small upstream fix."],
    ["The swap is not mechanical either way. Forward-porting onto main+56107: 54016 and the cudagraph downgrade cherry-pick clean, but the NIXL commit hits 23 conflict hunks in base_worker.py. Reverting the four commits out of the branch conflicts on 4-7 files each."],
    [],
]

rows += sect(
    "4b. Pricing #56107's cudagraph constraint - measured 2026-09-10",
    ["Metric", "c1", "c2", "c4", "c8", "arm"],
    [
        ["mean TTFT (ms)", 772.0, 936.5, 7533.5, 15868.8, "nodk, prefill PIECEWISE (what we run)"],
        ["mean TTFT (ms)", g(CGN,1,"ttft_ms"), g(CGN,2,"ttft_ms"), g(CGN,4,"ttft_ms"), g(CGN,8,"ttft_ms"), "nodk, prefill cudagraph NONE (what #56107 forces)"],
        ["TTFT delta", "+18.8%", "+19.6%", "-0.1%", "+2.2%", ""],
        ["total tok/s per GPU", 2643.8, 4392.2, 2273.7, 2287.1, "prefill PIECEWISE"],
        ["total tok/s per GPU", g(CGN,1,"total_tok_s_per_gpu"), g(CGN,2,"total_tok_s_per_gpu"), g(CGN,4,"total_tok_s_per_gpu"), g(CGN,8,"total_tok_s_per_gpu"), "prefill cudagraph NONE"],
        ["throughput delta", "-12.7%", "-8.0%", "+0.0%", "-1.4%", ""],
    ])
rows += [
    ["Same env, same commit (1ca5131a07), same points, 0 failures and 0 NCCL errors on all four. The ONLY delta is cudagraph_mode on the prefill role."],
    ["Graphless prefill costs ~19% TTFT and 8-13% throughput at c1/c2, is free at c4, and costs ~1-2% at c8. The shape fits the mechanism: with few requests in flight the prefiller's per-step launch overhead is exposed, and because this workload is 7 turns per request, prefill time lands directly in the end-to-end duration that throughput is computed over. By c4 there is enough concurrency to overlap prefill with decode."],
    ["CAVEAT: c1 and c2 are the noisy points (see section 1b - the floor there is ~11-12%). These deltas are about 1.6x the floor, consistent in direction across both concurrencies and both metrics, but from a single run. c4 and c8 are the trustworthy points, and they are near-flat."],
    ["CONCLUSION: do not adopt #56107's guard as written. Narrow it to the MTP branch - it was introduced by 4d675de05f for MTP and never scoped when DSpark support was added. Then #56107 is adoptable for the DCP1 arm, and the remaining cost is the NIXL PCP-to-TP port (23 conflict hunks in base_worker.py)."],
    [],
]

rows += sect(
    "5. Provenance and caveats",
    ["Item", "Value", "", "", "", ""],
    [
        ["Cluster", "CoreWeave H200 (coreweave-piggy, ns lwilkinson-dev), SM90", "", "", "", ""],
        ["Live from points.csv", "the PR-54036 backout arm (20260909T140000Z-no54036-20a94597), the simplify DCP8 arm (20260909T150000Z-simplify-a6f09074), the re-measured nodk arm (20260909T183000Z-nodk-1ca5131a), and the cudagraph-NONE arm (20260910T-nodk-cgnone)", "", "", "", ""],
        ["Transcribed", "all other rows. Their raw sweep directories were deleted on 2026-09-09 during a disk cleanup; points.csv and this sheet are now the surviving record.", "", "", "", ""],
        ["Peer gather verification", "needle probes at 19K / 39K / 98K tokens after the sparse-utils block-bound fix 11c47809ab, plus a full c1-c8 sweep with 0 failed points", "", "", "", ""],
        ["Untested variant", "229ff1766d makes peer gather the ONLY PCP-spanning DCP path and deletes the 305-line token-sharded fallback. Never re-run on hardware.", "", "", "", ""],
        ["c8 acceptance, simplify arm", "evalscope reported 90.8%, which is an estimator artifact: 10.88 decoded tok/iter exceeds the 8-token ceiling for 7 speculative tokens (chunk coalescing).", "", "", "", ""],
        ["Charts", "results/.artifacts/plots/h200-dspark-pd/h200-glm53-dspark7-pd-pareto.png (10 series / 37 points, rebuilt 2026-09-10); pipeline tracked at reproductions/glm53-benchmarks/h200-dspark-pd-pareto", "", "", "", ""],
        ["Generated", "2026-09-10 by Claude Code via sheet_publish_nodk.py", "", "", "", ""],
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
