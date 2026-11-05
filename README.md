# OCI-Monitor

A read-only Oracle Cloud Infrastructure (OCI) monitoring dashboard. Start it from a terminal and it shows the live status of your OCI resources in the browser, grouped by compartment. It also has an **Alerts** tab for your OCI alarms and a **Cost** tab with daily spend.

- Reuses your existing OCI CLI configuration (`~/.oci/config`). No new credentials are needed.
- **Strictly read-only**: it only calls `list_*`, `get_*`, Resource Search, and the Usage API and Monitoring queries. A unit test fails the build if a mutating SDK call ever appears in the code.
- A single page with no build step and no CDN, so it works offline.

## Quick start

You need:

- **Python 3.10+.** On Ubuntu/Debian also run `sudo apt install python3-venv`.
- **A working OCI CLI config.** Check with `oci os ns get`.

Each user runs their own copy with their own OCI credentials, so they see what their IAM permissions allow.

```bash
./run.sh                    # Linux / macOS / WSL: creates .venv on first run, then starts
.\run.ps1                   # Windows PowerShell
```

Or manually (Python 3.10+):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m ocimon
```

The dashboard opens at <http://127.0.0.1:8080/>. Add `--no-browser` to stop it opening a browser. Under WSL the URL is handed to your Windows browser.

### Running in the background

So you don't have to keep a terminal open:

```bash
./run.sh start --regions all   # starts in the background; flags as usual
./run.sh status                # running? on which URL?
./run.sh logs                  # follow the log (Ctrl+C stops following, not the app)
./run.sh restart --regions all # e.g. after `git pull`
./run.sh stop
```

The log goes to `ocimon.log`. On Windows, `.\run.ps1 start | stop | status | logs` does the same. The app stops when the machine shuts down; run `./run.sh start` again after a reboot. For an always-on, shared instance, see [Running behind a reverse proxy](#running-behind-a-reverse-proxy).

To try the UI without OCI access, use `./run.sh --demo`. It serves a realistic fake tenancy with healthy, stopped, failing and unreadable resources.

## What it shows

| Category | Resources | Status details |
|---|---|---|
| Compute | Instances | state, shape, OCPUs/memory, AD, fault domain, private/public IPs; **CPU %, memory used %, filesystem health** |
| Block Storage | Block volumes, boot volumes | state, size, VPUs, attached instance |
| Load Balancing | Load balancers, network load balancers | state, overall health and per-backend-set health (OK/WARNING/CRITICAL/UNKNOWN) |
| Databases | DB systems (VM/BM), Autonomous Databases, MySQL HeatWave (and replication channels), Database with PostgreSQL, OCI Cache (Redis/Valkey) | state, version, shape/size, workload type, HeatWave cluster, HA, nodes; **storage used %, CPU %, memory %** (MySQL, Autonomous, PostgreSQL; CPU/memory for OCI Cache) |
| Kubernetes | OKE clusters, node pools | state, Kubernetes version, node health (failed nodes → CRITICAL) |
| Networking | VCNs, subnets, internet/NAT/service gateways, DRGs, LPGs | summary |
| Object Storage | Buckets | public/private, approx. object count and size, tier |
| Integration | Functions applications, API Gateways, Streaming | state, function count, endpoint, partitions |
| Disaster Recovery | Full Stack DR protection groups and DR plans | state, role (primary/standby), peer region, members, plans, last run (drill/switchover/failover); a failed last run or a plan needing refresh is a problem |
| Security & APM | Web Application Firewalls, APM domains | state, which load balancer each firewall protects, free tier |

The UI has these parts:

- **Sidebar.** A collapsible compartment tree with resource counts, and a red badge when anything inside is unhealthy. Selecting a compartment shows it and its children.
- **Summary cards.** Each card shows healthy, stopped, warning and critical counts. The cards work as views: clicking a category card shows everything in that category, clicking **Problems** shows every unhealthy resource, and clicking the current card again goes back to all resources.
- **Tables.** Each resource type has a sortable table with color-coded status pills. Click a row to see its full details, OCID (with a copy button), related resources and a link to the OCI Console.
- **Search and filters.** A global search (press `/`) matches name, OCID, IP, shape and compartment. You can also filter by type, state and region, toggle **Only show problems**, and toggle **Show terminated** (terminated items are hidden by default).
- **Empty results.** When nothing matches, the page lists the active filters as chips, each removable with one click.
- **Read issues panel.** Lists every compartment or service that could not be read, such as one with no permission or one that was throttled. The rest of the page still loads.
- **Cost/day column.** Each resource row shows its average daily cost over the last 7 full days (sortable), and the expanded row shows a 7-day trend.
- **Other.** Light, dark and auto themes, responsive down to phone width, and URL-hash filters, so a filtered view can be bookmarked.

### Auto-refresh

- **Refreshes while watched:** while a dashboard tab is visible, data refreshes every `--refresh` seconds (cost every `--cost-refresh`).
- **Background tabs go quiet:** a background tab stops polling, and when no tab has been visible for `--idle-after` seconds (5 minutes by default), the server **stops calling OCI**.
- **Catches up on return:** come back to the tab and it refreshes at once if the data is older than the interval.
- **Cost:** a full refresh of 5 regions is about 190 read calls, so an unwatched dashboard no longer makes ~138,000 calls a day.
- **Pause button (⏸):** pauses scheduled refreshes, for inventory and cost. **Refresh now** still works while paused. Pausing applies to everyone using that server process, and a restart resumes it.

### Cost tab

The Cost tab follows the compartment selected in the sidebar and the region selector:

- **Summary tiles:** total for the last 7, 14 or 30 days with the change against the previous period, the daily average, yesterday, today so far, and the share of spend that matches resources on the dashboard.
- **Daily cost by service:** a stacked chart, with a hover tooltip per day and a table view.
- **By service and by compartment:** click a compartment to drill down.
- **Top 10 resources by cost:** with a daily trend. This includes items the dashboard doesn't list, such as volume backups. Click a resource to open it in the Resources tab.

How it stays light:

- **Its own schedule.** Cost data comes from the OCI Usage API on its own background thread, **once an hour** (`--cost-refresh`). That's two queries, taking about 10 seconds for a medium tenancy.
- **Never in the page's path.** The page and the inventory refresh never wait on it. Pages are served from memory.
- **Fails on its own.** If the Usage API is denied or fails, only the Cost tab shows a message.

Notes:

- **UTC days.** Days are in UTC, as the Usage API reports them.
- **Today is partial.** Usage data arrives a few hours late.
- **Contract pricing.** Amounts are the billed (`computed_amount`) cost under your contract, so credits or discounts may make the invoice differ slightly.
- **MySQL.** MySQL HeatWave usage is billed against an internal OCID, so it appears under MySQL in the service breakdown rather than on the DB system row.

A resource counts as a **problem** if any of these is true:

- its state is FAILED;
- its health is CRITICAL or WARNING;
- it needs attention (for example `AVAILABLE_NEEDS_ATTENTION` or `UNAVAILABLE`).

### Health signals from metrics

Instances and managed databases get a **Health** column from OCI Monitoring metrics. Each one is the latest 5-minute value; hover the health pill to see why.

| Signal | Warning | Critical | Source (no setup needed) |
|---|---|---|---|
| Memory used | ≥ 90% | ≥ 97% | Oracle Cloud Agent (`oci_computeagent`), MySQL, Autonomous DB |
| Storage used (databases) | ≥ 80% | ≥ 90% | `oci_mysql_database`, `oci_autonomous_database`, `oci_postgresql` |
| CPU | ≥ 90% | — | Oracle Cloud Agent, MySQL, Autonomous DB |
| Filesystem health | — | problem reported | `oci_compute_instance_health` |

The thresholds are in `ocimon/metrics.py`.

- **No disk free space for instances.** OCI only sees block and boot volumes from outside (I/O), so free space inside an instance needs an in-guest reporter. That isn't included yet.
- **Agent required.** Instances without the Oracle Cloud Agent's monitoring plugin, and stopped instances, show "—".
- **Cost.** Metrics are read with one query per metric and region, so it adds about a second to each refresh.

### Alerts tab

The Alerts tab lists your OCI Monitoring **alarms** with their current status. It follows the compartment, region and search filters. The tab shows how many alarms are firing.

- **Firing now:** sorted by severity, showing how long each alarm has been firing.
- **Triggered in the last 24 hours:** alarms that fired recently and are back to OK.
- **All other alarms:** OK, suppressed and disabled alarms, collapsed by default.

Click an alarm to see its query, summary and notification count, plus a link to it in the OCI Console. The dashboard only reads alarms. It never acknowledges, suppresses or changes them.

TERMINATED and DELETED resources show red but never count as problems, because they are usually intentional.

## Command-line flags

| Flag | Env var | Default | Description |
|---|---|---|---|
| `--host` | `OCIMON_HOST` | `127.0.0.1` | Bind address. Anything other than localhost requires `--token`. |
| `--port` | `OCIMON_PORT` | `8080` | Port. |
| `--base-path` | `OCIMON_BASE_PATH` | *(none)* | URL prefix, e.g. `/ocimon`, for running behind a reverse proxy. |
| `--config-file` | `OCI_CLI_CONFIG_FILE` | `~/.oci/config` | OCI config file. |
| `--profile` | `OCI_CLI_PROFILE` | `DEFAULT` | Config profile. |
| `--auth` | `OCI_CLI_AUTH` | `auto` | `auto`, `api_key`, `security_token` or `instance_principal`. |
| `--regions` | `OCIMON_REGIONS` | profile region | Comma-separated list, or `all` for every subscribed region. |
| `--compartment` | `OCIMON_COMPARTMENT` | tenancy | Root compartment OCID to monitor. Only this compartment and its subtree are shown. |
| `--refresh` | `OCIMON_REFRESH` | `120` | Inventory refresh interval in seconds (minimum 10). |
| `--cost-refresh` | `OCIMON_COST_REFRESH` | `3600` | Cost data refresh interval in seconds (minimum 300). |
| `--no-cost` | | | Don't fetch cost data; hides the Cost tab. |
| `--idle-after` | `OCIMON_IDLE_AFTER` | `300` | Stop calling OCI when no dashboard tab has been visible for this many seconds. `0` = keep refreshing with nobody watching. |
| `--token` | `OCIMON_TOKEN` | *(none)* | Access token required on every request. |
| `--workers` | `OCIMON_WORKERS` | `16` | Parallel OCI API calls. |
| `--demo` | | | Serve fake data. No OCI access is needed. |
| `--no-browser` | | | Don't open a browser on start. |
| `--log-level` | `OCIMON_LOG_LEVEL` | `info` | `debug` also logs HTTP requests. |

Examples:

```bash
./run.sh --regions all                          # every subscribed region
./run.sh --profile PROD --refresh 300           # another profile, refresh every 5 minutes
./run.sh --compartment ocid1.compartment.oc1..aaaa...   # only one subtree
```

## Authentication

OCI-Monitor reads the same config file and profile as the OCI CLI. Credentials stay on your machine. They are never logged and never sent to the browser. `/api/health` reports only the auth type, profile, region and session-token expiry time.

- **API key**: the standard `user`/`fingerprint`/`key_file` profile written by `oci setup config`.
- **Session token**: profiles created with `oci session authenticate` (they have `security_token_file`) are detected automatically. When the token expires, the UI shows a banner. Run `oci session refresh --profile <name>` and the next refresh picks up the new token without a restart.
- **Instance principal**: `--auth instance_principal` when running on an OCI compute instance.

## Required IAM policies

Only `inspect`/`read` verbs are needed. Replace `OCIMonitorReaders` with your group, or use a dynamic group for instance principals.

```text
Allow group OCIMonitorReaders to inspect compartments in tenancy
Allow group OCIMonitorReaders to inspect tenancies in tenancy
Allow group OCIMonitorReaders to read instance-family in tenancy
Allow group OCIMonitorReaders to read virtual-network-family in tenancy
Allow group OCIMonitorReaders to read volume-family in tenancy
Allow group OCIMonitorReaders to read load-balancers in tenancy
Allow group OCIMonitorReaders to read network-load-balancers in tenancy
Allow group OCIMonitorReaders to read database-family in tenancy
Allow group OCIMonitorReaders to read autonomous-database-family in tenancy
Allow group OCIMonitorReaders to read mysql-family in tenancy
Allow group OCIMonitorReaders to read postgres-db-systems in tenancy
Allow group OCIMonitorReaders to read redis-family in tenancy
Allow group OCIMonitorReaders to read disaster-recovery-family in tenancy
Allow group OCIMonitorReaders to read waf-family in tenancy
Allow group OCIMonitorReaders to read apm-domains in tenancy
Allow group OCIMonitorReaders to read streams in tenancy
Allow group OCIMonitorReaders to read cluster-family in tenancy
Allow group OCIMonitorReaders to read buckets in tenancy
Allow group OCIMonitorReaders to read functions-family in tenancy
Allow group OCIMonitorReaders to read api-gateway-family in tenancy
Allow group OCIMonitorReaders to read usage-reports in tenancy
Allow group OCIMonitorReaders to read metrics in tenancy
Allow group OCIMonitorReaders to read alarms in tenancy
```

Notes:

- **Resource Search** needs no policy of its own. It returns only what the caller can `inspect`.
- **Narrower scope.** You can grant per compartment (`in compartment X`) instead of `in tenancy`. Anything you can't read appears in the **read issues** panel, and the rest of the page still loads.
- **Bucket visibility.** `read buckets` is needed to show public/private and object counts. With only `inspect buckets`, buckets still appear but without those details. No access to object contents is needed.
- **Avoid `read all-resources`.** It works, but it also grants reading object contents in Object Storage.
- **Cost tab.** `read usage-reports` must be granted at tenancy level, because the Usage API reports on the whole tenancy. Without it, the Cost tab shows the missing policy and everything else works. Tenancy administrators already have it.

## Running behind a reverse proxy

Run OCI-Monitor on localhost with a base path and a token, and let the proxy publish it:

```bash
OCIMON_TOKEN='a-long-random-string' ./run.sh --no-browser --base-path /ocimon
```

```nginx
location /ocimon/ {
    proxy_pass http://127.0.0.1:8080/ocimon/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Users open `https://your.host/ocimon/?token=<token>` once. The token is swapped for an HttpOnly cookie and removed from the URL. API clients can send `Authorization: Bearer <token>` instead.

- **Without a token**, the server only answers requests whose `Host` is `localhost`, `127.0.0.1` or `::1`. This protects against DNS rebinding. If your proxy handles authentication itself and you don't want a token, set `proxy_set_header Host 127.0.0.1;`.
- **Prefix stripping.** If the proxy strips the prefix (`proxy_pass http://127.0.0.1:8080/;`), leave out `--base-path`. All URLs in the page are relative, so both setups work.
- **Binding directly to a network interface** (`--host 0.0.0.0`) prints a warning and refuses to start without a token. Serve it over HTTPS (via the proxy) so the token isn't sent in clear text.

## API

All endpoints return JSON and are relative to the base path.

| Endpoint | Description |
|---|---|
| `GET /api/summary` | Counts per resource type, per state and per status class, plus problem totals |
| `GET /api/compartments` | Compartment tree with per-compartment and rolled-up counts and problem counts |
| `GET /api/resources` | Filtered resource list |
| `GET /api/health` | Last refresh time, duration, next refresh, read errors, auth type |
| `GET /api/meta` | Regions, categories and resource types |
| `GET /api/cost?days=7\|14\|30` | Cost tab data. Also accepts `compartment` and `region` |
| `POST /api/auto-refresh?enabled=true\|false` | Resume or pause scheduled refreshes. Requires the header `X-OCIMON: 1` |
| `POST /api/refresh` | Trigger an immediate refresh. Add `?target=cost` to refresh cost data instead. Requires the header `X-OCIMON: 1` |

`/api/summary`, `/api/compartments` and `/api/resources` accept these filters:

- `compartment` (includes children unless `subtree=false`)
- `type` and `state` (comma lists; health values such as `CRITICAL` also work as states)
- `region` and `category` (comma lists)
- `q` (free text)
- `problems=true` and `include_terminated=true`

Example: `curl 'http://127.0.0.1:8080/api/resources?type=load_balancer&problems=true'`

## How it works

```
Refresher thread (every --refresh seconds, or "Refresh now")
  1. Compartment tree: list_compartments(subtree, ACCESSIBLE) + root
  2. Resource Search per region, limited to the covered types (fast inventory)
  3. Collectors run in a thread pool, only for compartments where Search found that type
  4. Anything Search found but a collector couldn't read is still listed (marked "from Search")
  5. The new Snapshot replaces the old one atomically; the API always serves the latest snapshot
```

- **Retries.** API calls use exponential backoff with jitter on 429, 5xx, timeouts and connection errors.
- **Errors.** A 401, 403 or 404 becomes an entry in the read-issues list.
- **Isolation.** A failing collector, compartment or region never breaks the page. If a refresh fails completely, the previous data stays visible with a warning.

### Adding a collector

1. Create `ocimon/collectors/<service>.py` with a `Collector` subclass decorated with `@register`. Declare its `types` (key, label, category, Resource Search type, console path template) and implement `collect(ctx)`. Use `ctx.compartments_with(...)`, `ctx.per_compartment(...)`, `ctx.map(...)` and `list_all(...)`. These run calls in parallel and record errors for you.
2. Import the module in `ocimon/collectors/__init__.py`.
3. Optionally add table columns for the new type in `COLUMNS` in `ocimon/static/app.js`. Otherwise you get name, state, compartment and region.
4. Add a test with mocked clients (see `tests/test_collectors.py`).

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests cover:

- every collector, against mocked SDK responses (including permission errors, pagination and partial failures);
- the refresh engine (Search fallback and per-type retry);
- API filters, summaries and the compartment tree;
- token, Host and CSRF protection, and the base path;
- config/auth loading;
- the read-only guard.

## Limitations

- OCI Console links are generated for the commercial realm (`oc1`) only.
- Data lives in memory. Restarting triggers a fresh collection, which takes about 5–10 seconds per region for a medium tenancy.
