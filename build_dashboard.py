#!/usr/bin/env python3
"""Build the COP361 GPU fleet dashboard for CloudWatch Omni: save it, write its JSON body, or write a
CloudFormation template for it.

The dashboard is the COP361 talk ("Monitor every GPU and AWS Trainium chip in your fleet") as one
page, in the talk's order:

  Cost per owner first, then
  Measure:  1. Device (working, or only busy?)  2. Queue (is anything waiting?)  3. Owner (whose is it?)
  Decide:   4. Right-sized?  5. Which instance?  6. Did it work? (goodput, not throughput)
  Appendix: hardware faults (fault hierarchy, Xid action classes), partitioning, Trainium/Inferentia.

  build_dashboard.py --cfn-out template.yaml                     # CloudFormation template (cluster is a parameter)
  build_dashboard.py --cluster C --out body.json                 # dashboard body only
  build_dashboard.py --cluster C --space-id S --save [--dashboard-id ID]   # create, or update ID in place

Body format: CloudWatch Omni dashboard `panels[]` on the 60-column grid (x/w columns, y/h px). Every
metric panel is PromQL over the metrics the Amazon CloudWatch Observability EKS add-on collects with
OTel Container Insights (DCGM GPU fields and vLLM engine metrics).
"""

import argparse
import re
import sys
import json

p = argparse.ArgumentParser()
p.add_argument("--region", default="us-east-1")
p.add_argument("--space-id", default="", help="Omni Space ID (required with --save)")
p.add_argument("--name", default="cop361-gpu-fleet")
p.add_argument("--dashboard-id", default="", help="update this dashboard in place (else create)")
p.add_argument("--cluster", default="", help="EKS cluster name (required with --out / --save)")
p.add_argument("--max-num-seqs", type=int, default=24, help="vLLM --max-num-seqs (batch ceiling) per replica")
p.add_argument("--max-num-seqs-for", action="append", default=[], metavar="NAMESPACE=N",
               help="batch ceiling for one team (namespace), when it differs from --max-num-seqs (repeatable)")
p.add_argument("--tensor-busy-pct", type=float, default=25.0,
               help="Right-sized? matrix: tensor-core activity at or above this counts as 'working'")
p.add_argument("--ttft-slo", type=float, default=2.0, help="time-to-first-token target, seconds")
p.add_argument("--itl-slo", type=float, default=0.1, help="inter-token latency target, seconds")
p.add_argument("--slo-objective", type=float, default=99.0, help="%% of requests / token gaps that must meet the target")
p.add_argument("--out", default="")
p.add_argument("--cfn-out", default="", help="also write a CloudFormation template (AWS::CloudWatch::OmniDashboard)")
p.add_argument("--save", action="store_true")
p.add_argument("--story", action="store_true",
               help="build only the panels that explain a cost-per-token gap between teams (talk version)")
a = p.parse_args()
# In a CloudFormation template the cluster is a parameter: build the body around a placeholder that the
# template's !Sub replaces with ${ClusterName}.
CLUSTER_PLACEHOLDER = "__CLUSTER_NAME__"
if a.cfn_out:
    if a.out or a.save:
        p.error("--cfn-out builds a parameterized body; run --out / --save separately with --cluster")
    a.cluster = CLUSTER_PLACEHOLDER
elif not a.cluster:
    p.error("--cluster is required with --out / --save")
if a.save and not a.space_id:
    p.error("--space-id is required with --save")

# On-demand Linux $/hour and GPUs per instance, US East (N. Virginia), from the AWS Price List API
# (November 2026). Each GPU costs its instance's price / its GPU count; the instance type comes from
# the @resource.host.type attribute on the DCGM series. Edit for your Region or negotiated rates.
INSTANCE_USD = {  # instance type: ($/hour, GPUs)
    "g4dn.xlarge": (0.526, 1), "g4dn.2xlarge": (0.752, 1), "g4dn.12xlarge": (3.912, 4),
    "g5.xlarge": (1.006, 1), "g5.2xlarge": (1.212, 1), "g5.4xlarge": (1.624, 1),
    "g5.12xlarge": (5.672, 4), "g5.48xlarge": (16.288, 8),
    "g6.xlarge": (0.8048, 1), "g6.2xlarge": (0.9776, 1), "g6.4xlarge": (1.3232, 1),
    "g6.12xlarge": (4.6016, 4), "g6.48xlarge": (13.3504, 8),
    "g6e.xlarge": (1.861, 1), "g6e.2xlarge": (2.24208, 1), "g6e.4xlarge": (3.00424, 1),
    "g6e.12xlarge": (10.49264, 4), "g6e.48xlarge": (30.13118, 8),
    "p4d.24xlarge": (21.957642, 8), "p4de.24xlarge": (27.44705, 8),
    "p5.4xlarge": (6.88, 1), "p5.48xlarge": (55.04, 8), "p5en.48xlarge": (63.296, 8),
}

C = f'"@resource.k8s.cluster.name"="{a.cluster}"'
DEP = '"@resource.k8s.deployment.name"'
NS = '"@resource.k8s.namespace.name"'
# Every panel groups by team: the workload's Kubernetes namespace. Deployment attribution is still
# required (OWNED / VLLM below) so only GPUs and servers that belong to a workload count.
GROUP = NS
# Per-GPU panels name the workload that owns the device (deployment), its GPU model and index, not
# the node (an IP-based host name). promql() relabels a GPU no pod is using as deployment "unowned".
GPU_BY = f'{GROUP}, "modelName", "gpu", "@resource.host.type"'


def gpu_labels(expr: str) -> str:
    """Readable legend for per-GPU series: gpu ("A10G #0"; DCGM's `modelName` is the GPU model, easily
    misread as the LLM) and instance ("g5.xlarge"). The Team label is added by promql(); a GPU no pod
    is using has no namespace and is labelled Team="unowned"."""
    out = f'label_replace({expr}, "Team", "unowned", {GROUP}, "")'
    out = f'label_join({out}, "gpu", " #", "modelName", "gpu")'
    out = f'label_replace({out}, "gpu", "$1", "gpu", "NVIDIA (.*)")'
    out = f'label_replace({out}, "instance", "$1", "@resource.host.type", "(.*)")'
    return f'sum without ("modelName", "@resource.host.type") ({out})'
HOST_TYPE = '"@resource.host.type"'
# Owner = namespace / service / build. Service is the pod's app.kubernetes.io/name (on GPU metrics
# service.name is the exporter, dcgm-exporter). Build is the ReplicaSet: no image tag or
# service.version reaches the metrics, and the ReplicaSet name changes with every rollout.
SVC_SRC, BUILD_SRC = '"@resource.k8s.pod.label.app.kubernetes.io/name"', '"@resource.k8s.replicaset.name"'
# A GPU in use by a workload carries that workload's attribution; an idle GPU carries none.
OWNED = f'{C}, {DEP}!=""'
VLLM = f'{C}, {DEP}!=""'
SLOTS = a.max_num_seqs
CEILINGS = {k: int(v) for k, v in (x.split("=", 1) for x in a.max_num_seqs_for)}
T = a.tensor_busy_pct


def gpu_types(expr: str) -> str:
    """For GPU-count tables: modelName -> gpu_type ("A10G"), host.type -> instance."""
    out = f'label_replace({expr}, "gpu_type", "$1", "modelName", "(?:NVIDIA )?(.*)")'
    out = f'label_replace({out}, "instance", "$1", {HOST_TYPE}, "(.*)")'
    return f'sum without ("modelName", {HOST_TYPE}) ({out})'


def g(metric: str, sel: str = C) -> str:
    return f'{{"{metric}", {sel}}}'


# $/GPU-hour as a lookup vector keyed on instance type, joined once per cost query (repeating the
# DCGM selector per instance type would push the body past Omni's 131,072-byte limit).
GPU_PRICE = " or ".join(f'label_replace(vector({usd / n:.6g}), {HOST_TYPE}, "{t}", "", "")'
                        for t, (usd, n) in INSTANCE_USD.items())


def gpu_usd_per_hour(by: str) -> str:
    """On-demand $/h of the GPUs each group holds: GPUs per instance type x that type's $/GPU-hour.
    GPUs on an instance type missing from INSTANCE_USD find no price and drop out."""
    return (f'sum by ({by}) (count by ({by}, {HOST_TYPE}) ({g("DCGM_FI_DEV_GPU_UTIL", OWNED)}) '
            f'* on ({HOST_TYPE}) group_left () ({GPU_PRICE}))')


# ---------------------------------------------------------------------------------- queries
TENSOR = f'100 * avg by ({GROUP}) ({g("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", OWNED)})'
RUNNING = f'sum by ({GROUP}) ({g("vllm:num_requests_running", VLLM)})'
REPLICAS = f'count by ({GROUP}) ({g("vllm:num_requests_running", VLLM)})'
# vLLM does not export max_num_seqs: the ceiling per deployment comes from the builder's settings,
# as a lookup keyed on deployment with --max-num-seqs as the default for everything else.
# Written as `by(`/`on(` (no space) so per_instance() leaves them keyed on deployment alone.
_default_ceiling = f'(0 * sum by({GROUP}) ({g("vllm:num_requests_running", VLLM)}) + {SLOTS})'
CEILING = (" or ".join([f'label_replace(vector({n}), {GROUP}, "{d}", "", "")' for d, n in CEILINGS.items()]
                       + [_default_ceiling]) if CEILINGS else _default_ceiling)
OCC = f'100 * ({RUNNING}) / ({REPLICAS}) / on({GROUP}) group_left() ({CEILING})'
WAITING = f'sum by ({GROUP}) ({g("vllm:num_requests_waiting", VLLM)})'
KV = f'100 * max by ({GROUP}) ({g("vllm:kv_cache_usage_perc", VLLM)})'

# Right-sized? over the last hour: Device axis = mean tensor activity, Queue axis = sustained waiting
# (peak 5-minute average >= 1), cache only splits the Held back cell. Value shown = peak occupancy %.
TENSOR_1H = f'avg_over_time(({TENSOR})[1h:1m])'
WAIT_1H = f'max_over_time((sum by ({GROUP}) (avg_over_time({g("vllm:num_requests_waiting", VLLM)}[5m])))[1h:1m])'
KV_1H = f'max_over_time(({KV})[1h:1m])'
OCC_1H = f'max_over_time(({OCC})[1h:1m])'


def cell(verdict: str, *conds: str) -> str:
    expr = f"({OCC_1H})" + "".join(f" and on ({GROUP}) ({c})" for c in conds)
    return f'label_replace({expr}, "verdict", "{verdict}", "", "")'


WL_KEY = f"{GROUP}, {HOST_TYPE}"


def per_instance(expr: str) -> str:
    """Re-key a per-deployment expression to deployment + instance type."""
    return expr.replace(f"by ({GROUP})", f"by ({WL_KEY})").replace(f"on ({GROUP})", f"on ({WL_KEY})")


WORKING, IDLE_CORES = f"({TENSOR_1H}) >= {T}", f"({TENSOR_1H}) < {T}"
WAITS, NO_WAIT = f"({WAIT_1H}) >= 1", f"({WAIT_1H}) < 1"
VERDICT = " or ".join([
    cell("SATURATED: add capacity (the only one that needs hardware)", WORKING, WAITS),
    cell("Held back: cache has room, raise the batch ceiling", IDLE_CORES, WAITS, f"({KV_1H}) < 90"),
    cell("Held back: cache is full, resize", IDLE_CORES, WAITS, f"({KV_1H}) >= 90"),
    cell("Fully used at this load: nothing to do", WORKING, NO_WAIT),
    cell("Over-provisioned: give it more work, partition, or reclaim", IDLE_CORES, NO_WAIT),
])
VERDICT = per_instance(VERDICT)

# The same matrix live: last-5-minute tensor activity and queue, current cache. One panel per box,
# bar height = occupancy now, labelled by workload and instance type.
TENSOR_5M = f'avg_over_time(({TENSOR})[5m:1m])'
WAIT_5M = f'sum by ({GROUP}) (avg_over_time({g("vllm:num_requests_waiting", VLLM)}[5m]))'


def live_box(*parts: tuple[str, list[str]]) -> str:
    """parts: (fix label, conditions). Empty box -> one placeholder bar so the panel never looks broken."""
    cells = []
    for fix, conds in parts:
        expr = f"({OCC})" + "".join(f" and on ({GROUP}) ({c})" for c in conds)
        cells.append(f'label_replace({expr}, "fix", "{fix}", "", "")')
    box = per_instance(" or ".join(cells))
    box = f'sum without ({HOST_TYPE}) (label_replace({box}, "instance", "$1", {HOST_TYPE}, "(.*)"))'
    return f'({box}) or (label_replace(vector(0), "Team", "(none right now)", "", "") unless on () ({box}))'


L_WORK, L_IDLE = f"({TENSOR_5M}) >= {T}", f"({TENSOR_5M}) < {T}"
L_WAIT, L_NOWAIT = f"({WAIT_5M}) >= 1", f"({WAIT_5M}) < 1"
BOX_FULLY_USED = live_box(("nothing to do", [L_WORK, L_NOWAIT]))
BOX_SATURATED = live_box(("add GPUs", [L_WORK, L_WAIT]))
BOX_OVER = live_box(("add work, share the GPU, or downsize", [L_IDLE, L_NOWAIT]))
BOX_HELD = live_box(("raise the batch ceiling", [L_IDLE, L_WAIT, f"({KV}) < 90"]),
                    ("cache full: use a bigger GPU", [L_IDLE, L_WAIT, f"({KV}) >= 90"]))


# vLLM histograms from the CloudWatch add-on are cumulative: read them with rate(). Their bucket counts
# can show false resets after conversion, which inflates totals but cancels out in quantiles and shares,
# so request counts come from the vllm:request_success_total counter instead.
def hq(q: float, metric: str, by: str = GROUP) -> str:
    return f'histogram_quantile({q}, sum by ({by}) (rate({g(metric, VLLM)}[5m])))'


def within(metric: str, slo: float) -> str:
    """Share of observations at or under the target, from the native histogram."""
    return f'histogram_fraction(0, {slo}, sum by ({GROUP}) (rate({g(metric, VLLM)}[5m])))'


TOKENS_PER_S = f'sum by ({GROUP}) (rate({g("vllm:generation_tokens_total", VLLM)}[5m]))'


def as_measure(expr: str, name: str) -> str:
    return f'label_replace({expr}, "measure", "{name}", "", "")'


# One unit (requests per replica) so the bars compare directly: a batch at its ceiling with a queue
# behind it is held back by the ceiling.
STEP_FULL_PARTS = [
    (f'({RUNNING}) / ({REPLICAS})', "1. in the batch"),
    (f'({WAITING}) / ({REPLICAS})', "2. waiting"),
    (f'({CEILING})', "3. ceiling"),
]


def workload(expr: str) -> str:
    """Per-workload series; promql() names each one by its Team (namespace)."""
    return expr


# vLLM's batch and cache configuration per workload. vLLM exports its CacheConfig as an info gauge
# (value 1, settings in labels) but not the scheduler's max_num_seqs, so the ceiling is this dashboard's
# --max-num-seqs setting, shown as such.
CFG_LABELS = [("kv_cache_size_tokens", "cache_tokens"), ("kv_cache_max_concurrency", "chats_that_fit_in_cache")]
_cfg = f'max by ({GROUP}, {", ".join(chr(34) + src + chr(34) for src, _ in CFG_LABELS)}) ({g("vllm:cache_config_info", VLLM)})'
for _src, _dst in CFG_LABELS:
    if _src != _dst:
        _cfg = f'sum without ("{_src}") (label_replace({_cfg}, "{_dst}", "$1", "{_src}", "(.*)"))'
_cfg = f'label_replace({_cfg}, "max_num_seqs", "{SLOTS} (dashboard setting)", "", "")'
for _d, _n in CEILINGS.items():
    _cfg = f'label_replace({_cfg}, "max_num_seqs", "{_n} (dashboard setting)", {GROUP}, "{_d}")'
# Add the served model: vLLM's request metrics carry model_name; join it on (value stays 1).
_cfg = (f'{_cfg} * on ({GROUP}) group_left (model_name) '
        f'(0 * count by ({GROUP}, model_name) ({g("vllm:num_requests_running", VLLM)}) + 1)')
VLLM_CONFIG = workload(_cfg)

# Averaged over the last 15 minutes, so a load change shows within minutes (Omni has no time-picker variable).
# `and on (team) RUNNING`: only teams serving now (a team that moved or was removed drops out at once).
STEP_FULL = workload(" or ".join(as_measure(f"(avg_over_time(({e})[15m:1m]) and on ({GROUP}) ({RUNNING}))", n)
                                 for e, n in STEP_FULL_PARTS))
# Owner section: the same two charts grouped three ways.
OWNER_VIEWS = [
    ("Team", NS, "The team boundary: the Kubernetes namespace (`k8s.namespace.name`)."),
    ("service", SVC_SRC, "The application: the pod's `app.kubernetes.io/name` label."),
    ("build", BUILD_SRC, "The rollout: the ReplicaSet name changes with every deployment, so a regression or a cost change lines up with the build that introduced it."),
]


def by_owner_label(expr: str, src: str, name: str) -> str:
    """Expose one owner attribute under a short legend key (namespace / service / build)."""
    return f'sum without ({src}) (label_replace({expr}, "{name}", "$1", {src}, "(.*)"))'


def busy_vs_working(key: str, src: str, name: str) -> str:
    util = f'label_replace(avg by ({key}) ({g("DCGM_FI_DEV_GPU_UTIL", OWNED)}), "signal", "busy (GPU utilization)", "", "")'
    tensor = f'label_replace(100 * avg by ({key}) ({g("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", OWNED)}), "signal", "working (tensor cores)", "", "")'
    return by_owner_label(f"{util} or {tensor}", src, name)


def usd_per(by: str, metric: str, per: float) -> str:
    """GPU $ held by each group / its rate of `metric` (a vLLM counter), scaled to `per` units."""
    return (f'{per:g} * (({gpu_usd_per_hour(by)}) / 3600) / on ({by}) '
            f'(sum by ({by}) (rate({g(metric, VLLM)}[5m])))')


TOKENS_CTR, REQUESTS_CTR = "vllm:generation_tokens_total", "vllm:request_success_total"


# ---------------------------------------------------------------------------------- layout
panels: list[dict] = []
_y = 0


CHART_H, TABLE_H = 380, 320


def _md_height(content: str) -> int:
    """Pixel height for full-width markdown. Omni renders h='auto' at a fixed default (~300 px), not at
    the content's height, so short text left large gaps. ~160 characters per wrapped line, 26 px per
    line, headings and table rows a little taller; the API needs at least 100 px."""
    h = 24
    for line in content.split("\n"):
        if not line.strip():
            h += 10
        elif line.startswith("|---"):
            continue
        elif line.startswith("#"):
            h += 40
        elif line.startswith("|"):
            h += 34
        else:
            h += 26 * (1 + len(line) // 160)
    return max(100, h)


def row(*cells):
    """Flow cells onto the 60-column grid, readable on a projector: markdown gets its own full-width
    row, then charts and tables (never mixed in a row) go two per row at w=30, or w=60 when alone."""
    global _y
    groups: list[tuple[str, list[dict]]] = []
    for c in cells:
        if c is None:  # dropped by full() in --story mode
            continue
        c.pop("_w", None)
        kind = "md" if c["type"] == "markdown" else ("table" if c["config"]["visualization"] == "table" else "chart")
        if groups and groups[-1][0] == kind and kind != "md":
            groups[-1][1].append(c)
        else:
            groups.append((kind, [c]))
    for kind, group in groups:
        if kind == "md":
            c = group[0]
            mh = _md_height(c["config"]["content"])
            c["layout"] = {"x": 0, "y": _y, "w": 60, "h": mh}
            panels.append(c)
            _y += mh
            continue
        h = CHART_H if kind == "chart" else TABLE_H
        for i in range(0, len(group), 2):
            pair = group[i:i + 2]
            w = 60 // len(pair)
            for j, c in enumerate(pair):
                c["layout"] = {"x": j * w, "y": _y, "w": w, "h": h}
                panels.append(c)
            _y += h


STORY = a.story


def full(cell):
    """A panel that only the full (lab) dashboard shows."""
    return None if STORY else cell


def sec(full_no: int, story_no: int) -> int:
    return story_no if STORY else full_no


def divider(title: str):
    global _y
    panels.append({"type": "markdown", "variant": "transparent", "layout": {"x": 0, "y": _y, "w": 60, "h": 80},
                   "config": {"content": f"# {title}\n\n---"}})
    _y += 80


def md(content: str, w=60):
    return {"type": "markdown", "_w": w, "variant": "transparent", "config": {"content": content}}


def team_labels(query: str) -> str:
    """Name every series by its team first: the namespace label becomes `Team` (capitalised so it sorts
    first in legends and tables) and the instance type becomes `instance`."""
    if NS not in query:
        return query
    out = f'label_replace(label_replace({query}, "Team", "$1", {NS}, "(.+)"), "instance", "$1", {HOST_TYPE}, "(.+)")'
    return f'sum without ({NS}, {DEP}, {HOST_TYPE}) ({out})'


def promql(title: str, query: str, w=20, viz="line", desc=None, ymax=None, unit=None, lines=(), hide=()):
    """unit: y-axis title on charts, a `unit` column on tables. lines: (value, label) pairs drawn as
    flat labelled series (Omni charts have no threshold-line option), e.g. an SLO target."""
    if GPU_BY in query:
        query = gpu_labels(query)
    query = team_labels(query)
    if viz == "table" and unit:
        query = f'label_replace({query}, "unit", "{unit}", "", "")'
    for value, label in lines:
        query = f'({query}) or label_replace(vector({value}), "target", "{label}", "", "")'
    cfg = {"queryString": query, "queryLanguage": "promql", "visualization": viz, "autoRun": True,
           "promqlQueryOptions": {"step": 300 if "[5m]" in query else 60}}
    if viz in ("line", "area", "bar"):
        opts = {"legend": {"show": True, "position": "bottom"}}
        axis = {"min": 0}
        if ymax is not None:
            axis["max"] = ymax
        if unit:
            axis["title"] = unit
        opts["yAxis"] = [axis]
        cfg["chartOptions"] = {"view": viz, "plotOptions": opts}
    panel = {"type": "explore", "_w": w, "title": title, "config": cfg}
    if viz == "bar":
        # Current value: one 5-minute bucket, one bar per series, the unit on the y-axis.
        cfg["promqlQueryOptions"] = {"step": 300}
        panel["scope"] = {"timeRange": {"start": "now-5m", "end": "now"}}
    if viz == "table":
        # Current value only: a 5-minute window at one 5-minute step, no time-series rows.
        cfg["promqlQueryOptions"] = {"step": 300}
        cfg["chartOptions"] = {"view": "table", "plotOptions": {"showTimeSeriesData": False}}
        cfg["chartOptions"]["plotOptions"]["hiddenColumns"] = ["@timestamp", "labels", "metric", "@labels", *hide]
        panel["scope"] = {"timeRange": {"start": "now-5m", "end": "now"}}
    if desc:
        panel["description"] = desc
    return panel


COST_DESC = "GPU cost per hour (on-demand instance price ÷ its GPUs) divided by what the team served."
# Cost per owner = per namespace (the team boundary); section 3 breaks it down by service and build.
TOK_1M = by_owner_label(usd_per(NS, TOKENS_CTR, 1e6), NS, "Team")
REQ_1K = by_owner_label(usd_per(NS, REQUESTS_CTR, 1e3), NS, "Team")
divider("Cost per team")
row(
    promql("GPU $ per 1K requests, by team", REQ_1K, unit="$ / 1K requests", desc=COST_DESC + " Counts completed requests."),
    promql("GPU $ per 1M generated tokens, by team", TOK_1M, unit="$ / 1M tokens", desc=COST_DESC),
)
row(
    promql("GPUs per team",
           by_owner_label(gpu_types(f'count by ({NS}, {HOST_TYPE}, "modelName") ({g("DCGM_FI_DEV_GPU_UTIL", OWNED)})'), NS, "Team"),
           unit="GPUs", desc="GPUs each team holds, with GPU and instance type. Same GPU count but different cost points to configuration."),
    promql("Tokens generated per second, by team (what the GPU $ buys)",
           by_owner_label(f'sum by ({NS}) (rate({g(TOKENS_CTR, VLLM)}[5m]))', NS, "Team"), unit="tokens/s"),
)

if STORY:
    row(md(
        "## Same traffic, one GPU per team. Why does one team pay more per token?\n"
        "Read down. **1. Device:** is the GPU doing real work? **2. Queue:** how full is each batch? "
        "**3. Right-sized?:** what's the fix? **4. Did it work?:** are users answered fast enough?\n\n"
        "Each step reads the whole model from memory, whether it serves 1 request or 24. "
        "**More requests per step means cheaper tokens.**"))
else:
    row(md(
    "## Measure, then decide\n"
    "**Measure:** Device. Queue. Owner. *Is it working, is anything waiting, whose is it.*  \n"
    "**Decide:** Right-sized? Which instance? Did it work?\n\n"
    "A decode step reads **the whole model** from GPU memory, and that read costs the same whether it carries "
    "1 request or 64. So stop asking whether the GPU is *busy*; ask **how full each step is**. "
    "Of all the reasons an accelerator is not producing tokens, **only one requires more hardware.**"))

# ---------------------------------------------------------------------------------- 1. device
divider("1. Device: is the GPU working, or just busy?")
row(
    promql("GPU busy vs GPU working (%), per team",
           f'label_replace(avg by ({GPU_BY}) ({g("DCGM_FI_DEV_GPU_UTIL", OWNED)}), "signal", "busy (GPU utilization)", "", "") or '
           f'label_replace(100 * avg by ({GPU_BY}) ({g("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", OWNED)}), "signal", "working (tensor cores)", "", "")',
           w=36, ymax=100, unit="%", desc="Busy: GPU utilization, the standard metric (DCGM_FI_DEV_GPU_UTIL, nvidia-smi GPU-Util): % of time any work ran. Working: tensor-core activity (DCGM_FI_PROF_PIPE_TENSOR_ACTIVE): % of time the matrix units computed."),
    md(
        "**100% busy is not 100% working.** Utilization counts time when *any* work ran, even on a sliver of the chip. "
        "Each token means reading every weight from memory, so the compute units spend much of their time waiting. "
        "Low tensor activity is normal for serving; lower than a peer on the same work means wasted capacity. "
        "(For non-AI workloads, read SM activity instead.)", w=24),
)
row(
    full(promql("Memory copy utilization % (bandwidth-bound decode)", f'avg by ({GPU_BY}) ({g("DCGM_FI_DEV_MEM_COPY_UTIL", OWNED)})', w=20, ymax=100, unit="%")),
    full(promql("SM activity % (use for CUDA-core workloads)", f'100 * avg by ({GPU_BY}) ({g("DCGM_FI_PROF_SM_UTIL_RATIO", OWNED)})', w=20, ymax=100, unit="%",
           desc="DCGM_FI_PROF_SM_UTIL_RATIO, the current name for SM_ACTIVE (field 1002)")),
    full(promql("GPU memory used % (weights + KV cache)",
           f'100 * sum by ({GPU_BY}) ({g("DCGM_FI_DEV_FB_USED", OWNED)}) / (sum by ({GPU_BY}) ({g("DCGM_FI_DEV_FB_USED", OWNED)}) + sum by ({GPU_BY}) ({g("DCGM_FI_DEV_FB_FREE", OWNED)}))',
           w=20, ymax=100, unit="%", desc="vLLM pre-allocates the KV cache, so this sits near --gpu-memory-utilization; the Queue section shows how much of that cache is in use")),
)

# ---------------------------------------------------------------------------------- 2. queue
divider("2. Queue: how full is each batch?")
row(md(
    "The model server runs requests in batches. **Running:** requests in the current batch. **Waiting:** requests "
    "queued for a slot. **KV cache:** memory holding each conversation in progress. The **batch ceiling** "
    "(`max_num_seqs`) caps how many run at once. Batch below the ceiling and nothing waiting: spare capacity. "
    "**Batch at the ceiling, a queue, and spare cache: the ceiling is too low.**"))
row(
    promql("Batch and cache settings per team", VLLM_CONFIG, viz="table", hide=["value"],
           desc="max_num_seqs: the batch ceiling. chats_that_fit_in_cache: how many maximum-length conversations the KV cache holds."),
    promql("Batch vs queue vs ceiling, per team (15-minute average)", STEP_FULL, viz="bar",
           unit="requests per replica",
           desc="Requests in the batch, requests waiting, and the batch ceiling."),
)
row(
    promql("Batch fullness (% of ceiling)", OCC, w=15, ymax=100, unit="%"),
    promql("Requests waiting", WAITING, w=15, unit="requests", lines=[(1, "queue threshold (1)")]),
    promql("KV cache used %", KV, w=15, ymax=100, unit="%", lines=[(90, "cache full (90%)")]),
    full(promql("Preemptions per second (evicted for cache)", f'sum by ({GROUP}) (rate({g("vllm:num_preemptions_total", VLLM)}[5m]))', w=15, unit="preemptions/s",
           desc="Non-zero means the cache could not hold the batch: requests were evicted and their work redone")),
)

# ---------------------------------------------------------------------------------- 3. owner
if not STORY:
    divider("3. Owner: correlate the tag with the metric")
    row(md(
        "DCGM and vLLM counters collected through the same OpenTelemetry pipeline carry the **same resource attributes** "
        "(`k8s.namespace.name`, `k8s.deployment.name`) because they describe the same pod: that is the join key, not "
        "timestamps. Group a device metric by owner and a hardware chart becomes an ownership chart; divide the owner's "
        "cost by its tokens for **cost per token per team**. Unused capacity with no owner never gets fixed."))
    for gname, gsrc, gwhat in OWNER_VIEWS:
        gkey = gsrc if gsrc == NS else f"{NS}, {gsrc}"  # every view keeps the team
        row(md(f"### By {'namespace' if gname == 'Team' else gname}\n{gwhat}"))
        charts = [promql(f"GPU utilization vs tensor-core activity % by {'team' if gname == 'Team' else gname}", busy_vs_working(gkey, gsrc, gname),
                         ymax=100, unit="%", desc="Utilization = a kernel was running (busy); tensor active = matrix work (working)")]
        if gname != "Team":  # per-team cost per token is the headline chart at the top
            charts.append(promql(f"GPU $ per 1M generated tokens by {gname}", by_owner_label(usd_per(gkey, TOKENS_CTR, 1e6), gsrc, gname),
                                 unit="$ / 1M tokens", desc=COST_DESC))
        row(*charts)
    row(
        promql("GPU cost by namespace", by_owner_label(gpu_usd_per_hour(NS), NS, "Team"), w=20, viz="table", unit="$/h", desc="On-demand GPU $/h held by each namespace"),
        promql("GPUs by namespace, GPU type and instance", by_owner_label(gpu_types(f'count by ({NS}, {HOST_TYPE}, "modelName") ({g("DCGM_FI_DEV_GPU_UTIL", OWNED)})'), NS, "Team"), w=20, viz="table", unit="GPUs"),
        promql("GPUs with no price (instance type missing from the price list)",
               f'count by ({HOST_TYPE}) ({g("DCGM_FI_DEV_GPU_UTIL", C + ", " + HOST_TYPE + "!~" + chr(34) + "|".join(t.replace(".", "[.]") for t in INSTANCE_USD) + chr(34))}) or vector(0)',
               viz="table", unit="GPUs", desc="Add these instance types to INSTANCE_USD in build_dashboard.py, or the cost panels leave them out"),
        promql("Unowned GPUs now (no workload attribution = idle)", f'count({g("DCGM_FI_DEV_GPU_UTIL", C + ", " + DEP + "=" + chr(34) * 2)}) or vector(0)',
               w=20, viz="table", unit="GPUs", desc="A GPU no pod is using carries no deployment attribution. Each one is paid for and producing nothing."),
    )

# ---------------------------------------------------------------------------------- 4. right-sized
divider(f"{sec(4, 3)}. Right-sized? What's the fix?")
row(md(
    f"**Which box is each team in?** Cores working: tensor activity at least {T:g}% (last 5 minutes). "
    "Requests waiting: a queue of 1 or more on average (last 5 minutes). Bar height: batch fullness. "
    "**Only Saturated needs more hardware.**"))
BOX_DESC = "A bar means the team is in this box now. Height: batch fullness."
row(
    promql("Working, no queue → Fully used: nothing to do", BOX_FULLY_USED, viz="bar", ymax=100, unit="% of ceiling", desc=BOX_DESC),
    promql("Working, queue → Saturated: add GPUs (the only box that needs hardware)", BOX_SATURATED, viz="bar", ymax=100, unit="% of ceiling", desc=BOX_DESC),
)
row(
    promql("Idle, no queue → Over-provisioned: add work or downsize", BOX_OVER, viz="bar", ymax=100, unit="% of ceiling", desc=BOX_DESC),
    promql("Idle, queue → Held back: fix the configuration", BOX_HELD, viz="bar", ymax=100, unit="% of ceiling", desc=BOX_DESC),
)
if not STORY:
    row(
        promql("Verdict per team (last hour; value = peak occupancy)", VERDICT, w=34, viz="table", unit="% of batch slots (peak)",
               desc=f"Device axis: mean tensor-core activity >= {T:g}% = working. Queue axis: a sustained queue "
                    "(peak 5-minute average >= 1 waiting). Cache splits Held back: < 90% has room (batch ceiling), >= 90% full (memory)."),
    )
    row(
        promql("Device axis: tensor-core activity % by team", TENSOR, w=20, ymax=100, unit="%",
               lines=[(T, f"threshold: working ({T:g}%)")]),
        promql("Queue axis: requests waiting by team", WAITING, w=20, unit="requests",
               lines=[(1, "queue threshold (1)")]),
        promql("Tie-break: KV cache used % by team", KV, w=20, ymax=100, unit="%",
               lines=[(90, "cache full (90%)")]),
    )

# ---------------------------------------------------------------------------------- 5. which instance
if not STORY:
    divider("5. Which instance?  Match it to your traffic shape")
    row(
        md(
            "**Find your row from your own latency target, then read across.**\n\n"
            "| Breaching | Bound by | Buy |\n|---|---|---|\n"
            "| Inter-token latency | decode, **memory bandwidth** | read speed per GPU |\n"
            "| Time to first token | prefill, **tensor cores** | matrix throughput (check prefix caching first) |\n"
            "| Weights + cache don't fit | **memory capacity** | GB per GPU |\n\n"
            "| GPU | Memory | Read speed |\n|---|---|---|\n"
            "| L4 | 24 GB | 300 GB/s |\n| A10G | 24 GB | 600 GB/s |\n| L40S | 48 GB | 864 GB/s |\n"
            "| A100 | 40 GB | 1,555 GB/s |\n| H100 | 80 GB | 3,350 GB/s |\n| H200 | 141 GB | 4,800 GB/s |\n\n"
            "**Twice the bandwidth, roughly twice the tokens per second.** An L40S has more tensor cores than an H100 "
            "and a quarter of the bandwidth. Size fit with AWS's 22 GiB usable for L4/A10G. Tensor cores are GPU units, "
            "not host vCPUs: the `48` in `p5.48xlarge` is vCPUs, the instance holds 8 GPUs.", w=24),
        promql("Prompt tokens p50 / p95 (input length)",
               f'label_replace({hq(0.5, "vllm:request_prompt_tokens")}, "q", "p50", "", "") or label_replace({hq(0.95, "vllm:request_prompt_tokens")}, "q", "p95", "", "")', w=18, unit="tokens",
               desc="Take percentiles, not means"),
        promql("Generation tokens p50 / p95 (output length)",
               f'label_replace({hq(0.5, "vllm:request_generation_tokens")}, "q", "p50", "", "") or label_replace({hq(0.95, "vllm:request_generation_tokens")}, "q", "p95", "", "")', w=18, unit="tokens"),
    )
    row(
        promql("Input : output token ratio (prompt-heavy > 1, answer-heavy < 1)",
               f'sum by ({GROUP}) (rate({g("vllm:prompt_tokens_total", VLLM)}[5m])) / sum by ({GROUP}) (rate({g("vllm:generation_tokens_total", VLLM)}[5m]))', w=15, unit="input tokens per output token"),
        promql("Prefix cache hit rate % (removes prefill work)",
               f'100 * sum by ({GROUP}) (rate({g("vllm:prefix_cache_hits_total", VLLM)}[5m])) / sum by ({GROUP}) (rate({g("vllm:prefix_cache_queries_total", VLLM)}[5m]))', w=15, ymax=100, unit="%"),
        promql(f"Time to first token p95 vs SLO ({a.ttft_slo:g} s)", hq(0.95, "vllm:time_to_first_token_seconds"), w=15, unit="seconds",
               lines=[(a.ttft_slo, f"target: {a.ttft_slo:g} s")],
               desc="Breaching = prefill-bound: compute matters more than bandwidth"),
        promql(f"Inter-token latency p95 vs SLO ({a.itl_slo:g} s)", hq(0.95, "vllm:inter_token_latency_seconds"), w=15, unit="seconds",
               lines=[(a.itl_slo, f"SLO: ITL {a.itl_slo:g} s")],
               desc="Breaching = decode-bound: the bar is your read bandwidth"),
    )
    row(
        promql("Generated tokens per second per GPU", f'({TOKENS_PER_S}) / on ({GROUP}) (count by ({GROUP}) ({g("DCGM_FI_DEV_GPU_UTIL", OWNED)}))', w=20, unit="tokens/s per GPU",
               desc="Compare across GPU models: decode throughput tracks read bandwidth"),
        promql("GPU hardware per team", gpu_types(f'count by ({GROUP}, {HOST_TYPE}, "modelName") ({g("DCGM_FI_DEV_GPU_UTIL", OWNED)})'), w=20, viz="table", unit="GPUs"),
        md(
            "**The vCPU trap.** Tokenization, detokenization, request handling and the scheduler run on the **host "
            "CPU**. Starve it and the GPU idles while throughput falls, which looks exactly like an over-provisioned "
            "GPU. Before shrinking, check the vLLM pods' CPU.\n\n"
            "**Shrink path from a p5.48xlarge** ($55.04/h): p5.4xlarge, one H100 (~$6.88/h, identical per-GPU "
            "behavior); then partition the H100 (MIG); then a g6e (~4x less bandwidth: re-verify both clocks).", w=20),
    )

# ---------------------------------------------------------------------------------- 6. did it work
divider(f"{sec(6, 4)}. Did it work? Count fast answers, not just output")
row(md(
    "**Throughput** counts tokens produced. **Goodput** counts requests answered within the latency target. "
    "Bigger batches raise throughput, but past a point every request slows down. "
    "**Raise the batch ceiling until goodput stops improving.**"))
row(
    full(promql("Throughput: requests completed per second", f'sum by ({GROUP}) (rate({g("vllm:request_success_total", VLLM)}[5m]))', w=20, unit="requests/s")),
    promql(f"Goodput: requests/s answered within {a.ttft_slo:g} s",
           f'({within("vllm:time_to_first_token_seconds", a.ttft_slo)}) * on ({GROUP}) (sum by ({GROUP}) (rate({g(REQUESTS_CTR, VLLM)}[5m])))', w=20, unit="requests/s",
           desc="Share answered within the target × completed requests per second. Time to first token measures the answer."),
    promql(f"Requests answered within {a.ttft_slo:g} s (%)", f'100 * {within("vllm:time_to_first_token_seconds", a.ttft_slo)}', w=20, ymax=100, unit="%",
           lines=[(a.slo_objective, f"target: {a.slo_objective:g}%")]),
)
row(
    None if not STORY else promql(f"Time to first answer token, p95 vs {a.ttft_slo:g} s target", hq(0.95, "vllm:time_to_first_token_seconds"), unit="seconds",
                                  lines=[(a.ttft_slo, f"target: {a.ttft_slo:g} s")]),
    full(promql(f"Share of token gaps inside the ITL target ({a.itl_slo:g}s) %", f'100 * {within("vllm:inter_token_latency_seconds", a.itl_slo)}', w=20, ymax=100, unit="%",
           lines=[(a.slo_objective, f"target: {a.slo_objective:g}%")],
           desc="Long gaps are what users experience as stuttering")),
    promql("Throughput: tokens generated per second", TOKENS_PER_S, w=20, unit="tokens/s"),
    full(promql("Batch actually run: requests running per replica", f'({RUNNING}) / ({REPLICAS})', w=20, unit="requests per step",
           desc="Read against goodput: when this rises and goodput falls, the ceiling is past the knee")),
)

# ---------------------------------------------------------------------------------- appendix
if not STORY:
    divider("Appendix: when it breaks, isolate the device, not the instance")
    row(md(
        "**Read the fault hierarchy in order, quietly degraded to failed:** throttling and clock drops, then correctable "
        "memory errors, then uncorrectable errors and row remapping, then Xid. Throttling with falling tokens per second is "
        "a cooling or power limit, not a failure. Correctable errors are a warning (watch the trend); **uncorrectable is "
        "terminal: cordon and replace.** Where the code allows, **reset the one GPU**: cycling a p5.48xlarge for one fault "
        "takes seven healthy H100s (~$48/h) down with it. Verify the fix cleared the signal it was raised for. "
        "Collective-communication health is in logs and traces only; there is no metric."))
    row(
        promql("1. SM clock (MHz): drops = throttling", f'avg by ({GPU_BY}) ({g("DCGM_FI_DEV_SM_CLOCK")})', w=15, unit="MHz"),
        promql("1. Power draw", f'avg by ({GPU_BY}) ({g("DCGM_FI_DEV_POWER_USAGE")})', w=15, unit="watts"),
        promql("1. GPU temperature", f'avg by ({GPU_BY}) ({g("DCGM_FI_DEV_GPU_TEMP")})', w=15, unit="°C"),
        promql("1. Clock event reasons (bitmask; non-zero = throttled)", f'max by ({GPU_BY}) ({g("DCGM_FI_DEV_CLOCKS_EVENT_REASONS")})', w=15, unit="bitmask",
               desc="DCGM_FI_DEV_CLOCKS_EVENT_REASONS, formerly CLOCK_THROTTLE_REASONS. Read with tokens per second: throttling while tokens fall is a cooling or power limit"),
        promql("2-3. ECC errors since boot: correctable (warning) / uncorrectable (terminal)",
               f'label_replace(max by ({GPU_BY}) ({g("DCGM_FI_DEV_ECC_SBE_VOL_TOTAL")}), "signal", "correctable (SBE)", "", "") or '
               f'label_replace(max by ({GPU_BY}) ({g("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL")}), "signal", "uncorrectable (DBE): cordon and replace", "", "")', w=15, unit="errors"),
    )
    row(
        md(
            "**Not every Xid means broken hardware** (31 codes in the catalog):\n\n"
            "- **Reset the GPU or reboot (17):** 46 48 62 64 79 95 109 110 119 120 136 140 143 151 155 156 158\n"
            "- **Investigate (6):** 45 54 61 74 142 154\n"
            "- **Fix your application (5):** 13 31 68 69 94: usually an illegal memory access; replacing hardware changes nothing\n"
            "- **Ignore (3):** 43 63 92\n\n"
            "`DCGM_FI_DEV_XID_ERRORS` and the remapped-row fields are not in the add-on's default DCGM field set; Xid events "
            "are in the node's kernel log (EKS node monitoring agent).", w=60),
    )
    row(
        md(
            "### Over-provisioned? Partition the GPU\n"
            "Multi-Instance GPU splits one A100 or H100 into up to **seven** partitions with dedicated multiprocessors, "
            "memory and bandwidth: the only sharing mode that attributes a regression to one tenant (time-slicing and MPS "
            "report per device). **Never co-locate two workloads with latency targets on a mode that cannot attribute a "
            "regression.** Trade-off: `GPU_UTIL` does not report MIG devices, so use occupancy (a profiling field, off by "
            "default).", w=20),
        md(
            "### Trainium and Inferentia\n"
            "**Queue, Owner, goodput and cost per token transfer unchanged** (vLLM on Neuron reports the same running, "
            "waiting and cache numbers). **Device does not:** `neuron_core_utilization_avg` measures the NeuronCore "
            "(the multiprocessor's counterpart), not the Tensor Engine, so busy-vs-working cannot be drawn, and there is "
            "no bandwidth counter: classify by output:input ratio, then host vs device time at low batch. Neuron compiles "
            "static shapes, so **padding** is a waste category: set bucket boundaries from your measured length "
            "distribution (section 5). No Xid: four kernel-log hardware errors, EKS replaces the node.", w=20),
        md(
            "### Queries return nothing?\n"
            "- DCGM renamed profiling fields (IDs unchanged): `PROF_TENSOR_UTIL_RATIO` was `PIPE_TENSOR_ACTIVE` (1004), "
            "`PROF_SM_UTIL_RATIO` was `SM_ACTIVE` (1002), `PROF_DRAM_UTIL_RATIO` was `DRAM_ACTIVE` (1005).\n"
            "- Profiling fields are **off by default**; sampling too fast returns **zeroes**, not nothing.\n"
            "- Accelerated Compute Insights does not query `PIPE_TENSOR_ACTIVE` or `MEM_COPY_UTIL` today; this dashboard "
            "reads them directly.\n- Quote `@` labels inside the selector or it is a parse error.", w=20),
    )

body = {"panels": panels, "scope": {"timeRange": {"start": "now-1h", "end": "now"}}}
# Compact JSON: the Omni API caps a dashboard body at 131,072 bytes.
text = json.dumps(body, separators=(",", ":"))
# The limit applies to the body as escaped inside the API request (quotes become \"), with headroom
# for the name and description sent alongside it.
_escaped = len(json.dumps(text)) + 2048
if _escaped > 131072:
    sys.exit(f"dashboard body is {_escaped} bytes escaped, over Omni's 131,072-byte limit")
if a.out:
    with open(a.out, "w") as f:
        f.write(text)
print(f"{len(panels)} panels, {len(text)} bytes ({_escaped} escaped, limit 131072)")

DESCRIPTION = "COP361 as a dashboard: measure Device, Queue, Owner; decide Right-sized?, Which instance?, Did it work?"

if a.cfn_out:
    # The body sits verbatim in a YAML literal block under !Sub, which substitutes ${ClusterName}
    # (the body has no other "${" sequences; PromQL's "$1" is left alone). The template is larger than
    # the 51,200-byte inline limit, so it is deployed through S3 (see README.md).
    if "${" in text:
        sys.exit("dashboard body contains '${', which the template's !Sub would try to substitute")
    body_block = "\n".join("        " + line for line in text.replace(CLUSTER_PLACEHOLDER, "${ClusterName}").splitlines())
    tmpl = f"""AWSTemplateFormatVersion: "2010-09-09"
Description: >-
  CloudWatch Omni dashboard for COP361 (Monitor every GPU and AWS Trainium chip in your fleet).
  Generated by build_dashboard.py{" --story (talk version)" if STORY else ""}; edit the builder, not this file.
Parameters:
  SpaceId:
    Type: String
    AllowedPattern: "^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$"
    Description: ID of the CloudWatch Omni Space to create the dashboard in (changing it replaces the dashboard)
  ClusterName:
    Type: String
    AllowedPattern: "^[0-9A-Za-z][A-Za-z0-9_-]*$"
    Description: Name of the EKS cluster whose GPU and vLLM workloads the dashboard shows
  DashboardName:
    Type: String
    Default: {a.name}
    AllowedPattern: "^[a-zA-Z0-9_.@~()-]+$"
    MaxLength: 256
Resources:
  Dashboard:
    Type: AWS::CloudWatch::OmniDashboard
    Properties:
      SpaceId: !Ref SpaceId
      Name: !Ref DashboardName
      Description: "{DESCRIPTION}"
      Tags:
        - Key: session
          Value: COP361
      Body: !Sub |
{body_block}
Outputs:
  DashboardArn:
    Value: !GetAtt Dashboard.Arn
  DashboardId:
    Value: !GetAtt Dashboard.DashboardId
"""
    with open(a.cfn_out, "w") as f:
        f.write(tmpl)
    print(f"wrote {a.cfn_out} ({len(tmpl)} bytes)")

if a.save:
    import boto3

    c = boto3.client("cloudwatchomni", region_name=a.region)
    desc = DESCRIPTION
    if a.dashboard_id:
        r = c.update_omni_dashboard(spaceId=a.space_id, dashboardId=a.dashboard_id, body=text, name=a.name, description=desc)
    else:
        r = c.create_omni_dashboard(spaceId=a.space_id, name=a.name, body=text, description=desc)
    d = r["omniDashboard"]
    print("saved", d["dashboardId"], d.get("arn"))
