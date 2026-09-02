#!/usr/bin/env python3
"""Keep only model-server objects from a `manifesto render manifest` stream.

The llm-d router (InferencePool, EPP, Gateway, HTTPRoute, DestinationRule) is Helm-managed
from deploy/router-values.yaml, so manifesto's routing objects are dropped here.
"""
import os
import json
import signal
import sys

import yaml

signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # quiet exit when a downstream `head`/`grep -m1` closes the pipe


class KubernetesDumper(yaml.SafeDumper):
    """Quote strings that Kubernetes' YAML 1.1 decoder treats as booleans."""


def represent_kubernetes_string(dumper, value):
    node = dumper.represent_scalar("tag:yaml.org,2002:str", value)
    if value.lower() in {"y", "yes", "n", "no", "true", "false", "on", "off"}:
        node.style = "'"
    return node


KubernetesDumper.add_representer(str, represent_kubernetes_string)

VLLM_IMAGE = os.environ.get("VLLM_IMAGE", "")  # optional override of the model-server image
KUEUE_QUEUE = os.environ.get("KUEUE_QUEUE", "")  # optional Kueue LocalQueue; admission gates the model pods

KEEP_KINDS = {"Deployment", "LeaderWorkerSet", "Service", "ConfigMap", "ServiceAccount"}
DROP_NAME_PARTS = ("-infpool", "-gateway", "-epp", "-envoy")
DROP_COMPONENTS = {"epp", "routing", "envoy"}

docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
kept = [
    d for d in docs
    if d["kind"] in KEEP_KINDS
    and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") not in DROP_COMPONENTS
    and not any(part in d["metadata"]["name"] for part in DROP_NAME_PARTS)
]
if VLLM_IMAGE:
    for d in kept:
        if d["kind"] == "Deployment":
            tmpl = d["spec"]["template"]
        elif d["kind"] == "LeaderWorkerSet":
            tmpl = d["spec"]["leaderWorkerTemplate"]["workerTemplate"]
        else:
            continue
        for c in tmpl["spec"]["containers"]:
            if c["name"] == "vllm":
                c["image"] = VLLM_IMAGE

# GKE multi-network pods must request each secondary Network named in the pod
# annotation. Manifesto currently models one RDMA resource; expand the declared
# rdma-* attachments here so every GPU gets its dedicated RoCE interface.
for d in kept:
    if d["kind"] == "Deployment":
        tmpl = d["spec"]["template"]
    elif d["kind"] == "LeaderWorkerSet":
        tmpl = d["spec"]["leaderWorkerTemplate"]["workerTemplate"]
    else:
        continue
    interfaces = tmpl.get("metadata", {}).get("annotations", {}).get(
        "networking.gke.io/interfaces"
    )
    if not interfaces:
        continue
    networks = [
        item.get("network")
        for item in json.loads(interfaces)
        if str(item.get("network", "")).startswith("rdma-")
    ]
    vllm = next(c for c in tmpl["spec"]["containers"] if c["name"] == "vllm")
    for resources in ("requests", "limits"):
        values = vllm.setdefault("resources", {}).setdefault(resources, {})
        for network in networks:
            values[f"networking.gke.io.networks/{network}"] = "1"
# The llm-d EPP treats every InferencePool targetPort as live on every pod unless the pod
# declares which ones it serves. Single-rank pods only listen on 8000; DP decode pods expose
# one rank per port through the disagg sidecar.
ACTIVE_PORTS_ANNOTATION = "llm-d.ai/active-ports"
POOL_PORTS = range(8000, 8008)
for d in kept:
    if d["kind"] == "Deployment":
        tmpl = d["spec"]["template"]
    elif d["kind"] == "LeaderWorkerSet":
        tmpl = d["spec"]["leaderWorkerTemplate"]["workerTemplate"]
    else:
        continue
    spec = tmpl["spec"]
    ports = sorted({
        p["containerPort"]
        for c in spec.get("initContainers", []) + spec.get("containers", [])
        for p in c.get("ports", [])
        if p.get("containerPort") in POOL_PORTS
    })
    if ports:
        tmpl.setdefault("metadata", {}).setdefault("annotations", {})[ACTIVE_PORTS_ANNOTATION] = ",".join(map(str, ports))
if KUEUE_QUEUE:
    for d in kept:
        if d["kind"] in ("Deployment", "LeaderWorkerSet"):
            d["metadata"].setdefault("labels", {})["kueue.x-k8s.io/queue-name"] = KUEUE_QUEUE
if not kept:
    raise SystemExit("filter-render: no model-server objects in input")
print("# Rendered by llm-manifesto; routing objects stripped (router is Helm-managed, see deploy/router-values.yaml).")
yaml.dump_all(kept, sys.stdout, Dumper=KubernetesDumper, sort_keys=False)
