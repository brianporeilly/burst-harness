Write a short morning summary of what needs attention in my Kubernetes cluster.

1. Run `python3 /workspace/scripts/cluster_state.py https://10.0.0.5:6443 > /workspace/state.json`. If it fails, report the error.
2. Read the file with `jq` or `python3`. It is already boiled down to what is wrong, section by section: `nodes`, `pods` (with `failed_jobs`), `warning_events_24h`, `certificates`, `flux`, `backups`, `volumes`. A section with `unavailable` could not be read; say so in one line, do not guess.
3. Write the summary:
   - Start with one line: all clear, or how many things need attention.
   - Then **Needs attention**: one bullet per problem, most serious first (nodes, backups, Flux, certificates, then pods and jobs). Say what is wrong in plain English and, where the data shows it, the likely cause. Group related findings: a pending PVC and the warning events about provisioning it are one problem, not two.
   - Then **Noise**: one line listing things that look alarming but probably are not (a single restart, a job that failed once days ago).
4. Do not suggest commands to run unless the fix is obvious from the data, and never more than one per problem.

Put the summary straight into finish_report. Do not write it out as a message first.
