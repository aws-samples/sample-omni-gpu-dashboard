# COP361 lab: a GPU fleet dashboard in CloudWatch Omni

This lab deploys one CloudWatch Omni dashboard that turns the advice in the re:Invent session
**COP361, "Monitor every GPU and AWS Trainium chip in your fleet"**, into live charts for the GPU
and vLLM workloads on your Amazon EKS cluster.

The dashboard follows the talk, measuring first and then deciding. Every series is labelled by
**Team** (the Kubernetes namespace), so two teams' model servers can be compared side by side:

| Section | Question it answers | Main signals |
|---|---|---|
| Cost per team | What does a request, or a token, cost each team? | GPU $/h ÷ generated tokens or completed requests |
| 1. Device | Is the GPU working, or just busy? | GPU busy (utilization) vs working (tensor-core activity), memory copy, SM activity |
| 2. Queue | How full is each batch? | Requests running, requests waiting, KV cache used, occupancy, vLLM cache config |
| 3. Owner | Whose capacity is it? | The same signals grouped by namespace, service and build |
| 4. Right-sized? | Which of the four boxes is each team in, and what's the fix? | Live 2×2 matrix (tensor activity × queue), 1-hour verdict with instance type |
| 5. Which instance? | Does the GPU match the traffic shape? | Prompt and output length, prefix cache hits, TTFT and ITL p95 against SLOs |
| 6. Did it work? | Are users answered fast enough (goodput, not just throughput)? | Requests inside the latency targets, batch size against goodput |
| Appendix | When it breaks | Clocks, power, temperature, ECC errors, Xid action classes, MIG, Trainium and Inferentia notes |

Two versions are included: the **full dashboard** above (`template.yaml`), and a **story version**
(`template-story.yaml`, about 20 panels) that explains why one team pays more per token than another:
cost per team, Device, Queue, the Right-sized? boxes, and goodput. Use the story version for a
presentation.

## Files

| File | What it is |
|---|---|
| `template.yaml` | CloudFormation template for the full dashboard: one `AWS::CloudWatch::OmniDashboard` resource |
| `template-story.yaml` | The same for the story version |
| `deploy.sh` | Deploys the template with `aws cloudformation deploy` |
| `build_dashboard.py` | Generates the dashboard and the template (edit this, not `template.yaml`) |
| `check_queries.py` | Runs every panel's query against your account and lists the ones that return no data |

## Prerequisites

1. **An Amazon EKS cluster running GPU inference with vLLM**, with each model server as a Kubernetes
   Deployment. To compare teams, put each team's model servers in its own namespace.
2. **The Amazon CloudWatch Observability EKS add-on with OTel Container Insights enabled.** The
   add-on collects the NVIDIA DCGM GPU fields (including the profiling fields this dashboard uses)
   and the vLLM engine metrics, and attributes both to the Deployment that owns the GPU. This lab was
   tested with add-on `v7.0.1-eksbuild.1` and this configuration:

   ```json
   { "containerInsights": { "enabled": false }, "otelContainerInsights": { "enabled": true } }
   ```

3. **A CloudWatch Omni Space** in the same Region. If you don't have one, follow
   [Set up Omni for a single account](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/omni-set-up-omni-for-a-single-account.html)
   (one console flow creates the domain and the Space), or
   [Set up Omni for your organization](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/omni-set-up-omni-for-your-organization.html).
   To find the Space ID:

   ```bash
   aws cloudwatchomni list-spaces --region <region> --query "items[?region=='<region>']"
   ```

   If the command fails with an unknown-service error, your AWS CLI predates CloudWatch Omni.
   Upgrade to the latest AWS CLI v2.
4. **An S3 bucket you own** in the same Region. The template is about 100 KB, which is over
   CloudFormation's 51,200-byte limit for a template passed inline, so `deploy.sh` uploads it to S3.
5. **IAM permissions** to deploy CloudFormation stacks, upload to that bucket, and create Omni
   dashboards in the Space.

## Prepare your workloads

The dashboard groups everything by **Team**, and a team is a Kubernetes namespace. Before you deploy:

1. **Run each team's vLLM servers in its own namespace.** The namespace *name* becomes the Team label
   (for example `team-a`, `payments`). Name namespaces the way you want teams to appear.
2. **Don't rely on namespace labels.** The add-on attaches the namespace name and every *pod* label
   to the metrics, but not labels set on the Namespace object. For an extra grouping such as a cost
   center, put a label on the pods (for example `cost-center: retail`); it arrives as
   `@resource.k8s.pod.label.cost-center`.
3. **Set `app.kubernetes.io/name` on each model server's pods.** It's the service grouping in the Owner
   section (`SVC_SRC` in the builder).
4. **Optional: set `app.kubernetes.io/version`** to group by release instead of ReplicaSet (change
   `BUILD_SRC` in the builder).
5. **Note each team's `--max-num-seqs`.** vLLM doesn't export it. Pass the default with
   `--max-num-seqs` and any team that differs with `--max-num-seqs-for <namespace>=<n>`.

## Deploy

```bash
SPACE_ID=<omni-space-id> CLUSTER_NAME=<eks-cluster-name> ARTIFACT_BUCKET=<s3-bucket> AWS_REGION=<region> ./deploy.sh
```

Add `STORY=1` to deploy the story version instead. The script prints the new dashboard's ID and ARN. Open the CloudWatch Omni console, go to your
Space's dashboards, and open **cop361-gpu-fleet**. Set `DASHBOARD_NAME` to use a different name.

You can also deploy a template from the CloudFormation console: upload `template.yaml` (or
`template-story.yaml`) and fill in `SpaceId` and `ClusterName`.

## Check that every panel has data

Generate the dashboard body for your cluster, then run its queries:

```bash
pip install boto3   # --save also needs a boto3 recent enough to include CloudWatch Omni
python build_dashboard.py --cluster <eks-cluster-name> --out body.json
AWS_REGION=<region> python check_queries.py body.json
```

Each line reports `ok` with the number of series returned, or `EMPTY`. Run it while the vLLM
servers have traffic. Common reasons for an empty panel:

- **No vLLM or GPU metrics for the cluster.** Check that the add-on is installed with OTel
  Container Insights, and that the cluster name you passed matches exactly.
- **Profiling fields missing** (tensor-core and SM activity). DCGM profiling fields can be renamed
  between DCGM releases, and sampling them too often returns zeroes. The dashboard's appendix lists
  the old and new names.
- **Nothing in a box or queue.** Several panels are legitimately empty or zero until requests queue.

## Customize

The values below are fixed when the dashboard is generated. Change them by regenerating
`template.yaml` and deploying again:

```bash
python build_dashboard.py --cfn-out template.yaml --max-num-seqs 32 --ttft-slo 1.5 --itl-slo 0.05
python build_dashboard.py --cfn-out template-story.yaml --story --max-num-seqs 32 --max-num-seqs-for team-b=6
```

| Option | Default | Used for |
|---|---|---|
| `--max-num-seqs` | `24` | vLLM's batch ceiling per replica, used for occupancy. vLLM does not export it, so set it to the value your servers run with |
| `--max-num-seqs-for` | none | A different ceiling for one team, as `NAMESPACE=N`. Repeat for each namespace that differs |
| `--tensor-busy-pct` | `25` | Tensor-core activity at or above this counts as "cores working" in the Right-sized? matrix |
| `--ttft-slo` / `--itl-slo` | `2` s / `0.1` s | Latency targets, drawn as SLO lines and used for goodput |
| `--slo-objective` | `99` | Share of requests or token gaps that must meet the targets |
| `--name` | `cop361-gpu-fleet` | Default dashboard name |
| `--story` | off | Build only the ~20 panels that explain a cost-per-token gap between teams (cost, device, queue, the Right-sized? boxes, goodput). Use it for a presentation; the full dashboard is the default |

**GPU prices.** Each GPU is priced at its instance's on-demand Linux rate divided by the instance's
GPU count, from `INSTANCE_USD` at the top of `build_dashboard.py` (US East, N. Virginia, from the AWS
Price List API, November 2026). The instance type comes from the `@resource.host.type` attribute on
each GPU series. GPUs on instance types missing from the list are left out of the cost panels and
show in **GPUs with no price** in the Owner section. Edit the list for your Region, other instance
types, or Savings Plans and Reserved Instance rates, then regenerate the template.

To create or update the dashboard directly through the Omni API, without CloudFormation:

```bash
python build_dashboard.py --cluster <eks-cluster-name> --space-id <omni-space-id> --region <region> --save
```

Add `--dashboard-id <id>` to update an existing dashboard in place.

## What the dashboard reads

- **Every series is labelled by `Team` first**: the Kubernetes namespace of the workload
  (`k8s.namespace.name`). Put each team's model servers in its own namespace to compare teams.
- **Ownership comes from OpenTelemetry resource attributes**, not from timestamps. GPU and vLLM
  series from the same pod carry the same `k8s.namespace.name` and `k8s.deployment.name`, so they
  join. **Service** is the pod's `app.kubernetes.io/name` label. **Build** is the ReplicaSet name,
  which changes with every rollout. To group by a real version instead, add an
  `app.kubernetes.io/version` label to your pods and change `BUILD_SRC` in the builder.
- **"Unowned" GPUs** are GPUs with no workload attribution: paid for and doing nothing.
- **Current-value charts** ("right now") use the last 5 minutes. "How full is the step?" averages over
  the last 15 minutes and the Right-sized? verdict table over the last hour. Omni has no time-picker variable for
  queries, so these windows don't follow the dashboard's time range. The dashboard opens on the
  last hour.

## Known limitations

- **PromQL tables always label the number `value`.** Tables carry a `unit` column, and the
  current-value views are bar charts with the unit on the y-axis. Omni also draws a "Distribution"
  strip (when samples arrived) above every table; it can't be turned off from the dashboard body.
- **Dashboard size.** Omni accepts a dashboard body up to 131,072 bytes once escaped in the API
  request. The builder checks this and stops with an error if an edit (for example a longer price
  list) goes over.
- **`max_num_seqs` is not exported by vLLM.** The configuration table shows vLLM's exported cache
  settings beside the `--max-num-seqs` value the dashboard was generated with.
- **Xid errors and remapped rows** are not in the add-on's default DCGM field set. The appendix
  lists the Xid action classes as text.
- **No metric-against-metric plot.** Omni charts are time series, so the Right-sized? matrix is four
  bar charts laid out as the 2×2 grid, each showing the teams in that box.
- **Trainium and Inferentia:** the Queue, Owner and goodput sections apply unchanged to vLLM on
  Neuron. The Device section is NVIDIA-specific; the appendix explains why.

## Clean up

```bash
aws cloudformation delete-stack --stack-name cop361-gpu-dashboard --region <region>
```

This deletes the dashboard. The template uploaded to your S3 bucket under the `cop361-gpu-dashboard/`
prefix is not deleted.
