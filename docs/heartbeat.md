# Heartbeat Procedure

The Saffron heartbeat is a no-op. Reply `HEARTBEAT_OK`.

Dispatch sync, grooming, and worker execution are handled by k8s CronJobs:

| Function | CronJob | Schedule | What it does |
|----------|---------|----------|--------------|
| Sync | `dispatch-heartbeat-sync` | `*/15` | `POST /api/sync/scheduled` — pull issues from GitHub, reconcile |
| Groom | `dispatch-heartbeat-groom` | `*/10` | `POST /api/groomer/run` — groom one candidate into a claimable lane |
| Worker dispatch | `foreman-dispatch-bridge` | `*/15` | Claims Dispatch work (local/cloud/frontier), creates Foreman Workloads |

Worker crons (local/cloud/frontier) are disabled. Foreman handles execution via
coder/gate/reviewer Agent CRs in k8s.

The OpenClaw heartbeat poll fires but has nothing to do. Saffron remains
available for interactive work: issue shaping, ad-hoc engineering, reviews,
and anything LilDrunkenSmurf asks for directly.

## Active Saffron Crons

| Cron | Schedule | Script |
|------|----------|--------|
| Weekly Audit of Misospace | Wed 1am MT | `project_groom.py --list-tracked-repos` |
| Nightly Audit Decomposer | Daily 2am MT | `audit_decompose.py --scan --apply` |
