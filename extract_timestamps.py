#!/usr/bin/env python3
"""Extract start/end timestamps (epoch seconds) from profile_export_aiperf.json with 60s padding."""
import json, sys

with open(sys.argv[1]) as f:
    d = json.load(f)

pad = 60
ns_to_s = 1e9
print(d["min_request_timestamp"]["avg"] / ns_to_s - pad)
print(d["max_response_timestamp"]["avg"] / ns_to_s + pad)
