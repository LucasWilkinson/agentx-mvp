#!/usr/bin/env python3
"""Send one deterministic request and summarize SpecDecoding counters."""

import argparse
import json
import re
import urllib.request
from pathlib import Path

from transformers import AutoTokenizer


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode()


def counters(metrics: str) -> tuple[float, float, dict[int, float]]:
    def scalar(name: str) -> float:
        match = re.search(rf"^{re.escape(name)}\{{[^}}]*\}} ([0-9.e+-]+)$", metrics, re.M)
        return float(match.group(1)) if match else 0.0

    per_position = {
        int(position): float(value)
        for position, value in re.findall(
            r'^vllm:spec_decode_num_accepted_tokens_per_pos_total\{[^}]*position="(\d+)"[^}]*\} ([0-9.e+-]+)$',
            metrics,
            re.M,
        )
    }
    return (
        scalar("vllm:spec_decode_num_drafts_total"),
        scalar("vllm:spec_decode_num_draft_tokens_total"),
        per_position,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    seed_text = """
You are debugging an intermittent distributed inference failure. Four GPU ranks
run the same quantized mixture-of-experts model. Tensor parallel execution is
stable, but enabling expert parallelism changes the final hidden state and lowers
speculative-token acceptance. Rank zero reports 8.4 ms in the expert kernel,
while the other ranks report 8.1, 8.6, and 8.3 ms. Converting every collective
reduction to float32 produces bit-identical outputs to the original reduction.
Replacing the fused expert kernel with a CUTLASS implementation changes the
tensor-parallel output as well, and combining CUTLASS with expert parallelism
makes the mismatch slightly larger. Target decoding remains output-exact because
every draft token is verified. Explain the most likely mechanism, distinguish
numerical drift from communication overhead, and propose one controlled test
that would falsify your explanation. Be precise about which tensors to compare.
""".strip()
    token_ids = tokenizer.encode(seed_text, add_special_tokens=False)[:128]
    prompt = tokenizer.decode(token_ids, skip_special_tokens=True)
    assert len(tokenizer.encode(prompt, add_special_tokens=False)) == 128

    before = counters(fetch(f"{args.url}/metrics"))
    body = json.dumps(
        {
            "model": args.model,
            "prompt": prompt,
            "max_tokens": 128,
            "temperature": 0,
            "ignore_eos": True,
        }
    ).encode()
    request = urllib.request.Request(
        f"{args.url}/v1/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=1800) as response:
        completion = json.loads(response.read())
    after = counters(fetch(f"{args.url}/metrics"))

    drafts = after[0] - before[0]
    draft_tokens = after[1] - before[1]
    accepted = {
        position: after[2].get(position, 0) - before[2].get(position, 0)
        for position in range(7)
    }
    accepted_total = sum(accepted.values())
    result = {
        "prompt_token_ids": token_ids,
        "prompt": prompt,
        "completion": completion,
        "num_drafts": drafts,
        "num_draft_tokens": draft_tokens,
        "accepted_tokens": accepted_total,
        "acceptance_rate": accepted_total / draft_tokens if draft_tokens else 0,
        "per_position_acceptance": {
            str(position): count / drafts if drafts else 0
            for position, count in accepted.items()
        },
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"prompt", "completion", "prompt_token_ids"}}))


if __name__ == "__main__":
    main()
