# maven-dep-upgrader (`mvn-upgrade`)

Automated Maven dependency & plugin upgrader. For a given Maven repository it:

1. Reads the effective POM(s) and produces the full dependency **and** plugin list.
2. Checks **Nexus** for a newer allowed version of each artifact.
3. Builds an upgrade plan and writes it to a report.
4. Creates a branch and upgrades **one artifact at a time**. After each bump it
   runs the build through the **OpenAI Codex CLI** (`codex exec`) in a bounded
   auto-fix loop until the build is green, then makes **one commit per artifact**.
5. Writes `dependency-updates.md` + `dependency-updates.json`.
6. If anything was upgraded, pushes the branch and opens a **GitLab merge request**.

Nexus is the single source of truth: the tool never upgrades to a version Nexus
does not serve, even if Maven Central offers something newer.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Python 3.11+. At runtime the host also needs `mvn`, `git`, the
`codex` CLI (for `run --apply`), and `glab` (for `--create-mr`; a REST fallback
exists).

## Configure

Copy `config.example.yaml` and fill in the `# TODO` values (Nexus base URL +
repositories, GitLab project path). Secrets are **never** put in the config —
they come from environment variables only:

| Variable | Used for |
|---|---|
| `NEXUS_USER`, `NEXUS_PASSWORD` | Nexus REST search auth |
| `OPENAI_API_KEY` (optional — see `codex.api_key_envs`) | Only if Codex reads the key from env |
| `GITLAB_TOKEN` (`GITLAB_HOST` for self-managed) | GitLab MR |

For **LiteLLM** or a custom Codex wrapper, you usually need nothing in config — Codex
inherits your shell environment and its own config. Optional overrides:

```yaml
codex:
  api_key_envs: [YOUR_LITELLM_KEY]   # only used for preflight when require_api_key: true
  extra_env:
    OPENAI_API_BASE: http://your-litellm-host:4000/v1
```

Preflight does **not** require an API key by default (`codex.require_api_key: false`).
The tool never calls the LLM itself; it only runs `codex exec`, which uses whatever
auth Codex already has.

Maven server credentials live in `settings.xml`. Point `maven.settings` at a
`settings.xml` containing a `mirrorOf=*` mirror to your Nexus so the build and
`versions-maven-plugin` resolve only against Nexus.

## Usage

```bash
# Plan only (default, no mutations). Writes the report + upgrade-plan.csv.
mvn-upgrade plan --config config.yaml

# Run upgrades from an edited plan file (order column controls sequence).
mvn-upgrade run --config config.yaml --apply --plan-file upgrade-plan.csv

# Also push the branch and open a GitLab MR if anything was upgraded.
mvn-upgrade run --config config.yaml --apply --create-mr

# Restrict / cap for testing.
mvn-upgrade run --config config.yaml --apply --only com.google.guava:guava --max 1

# Override failure handling for one run.
mvn-upgrade run --config config.yaml --apply --on-failure abort

# Regenerate the markdown report from the last run's state.
mvn-upgrade report --config config.yaml
```

Without `--apply`, `run` behaves exactly like `plan` (safety).

## Editable upgrade plan (`upgrade-plan.csv`)

Every `plan` writes **`upgrade-plan.csv`** alongside the report. Edit it in Excel
or a text editor before running:

| Column | Meaning |
|--------|---------|
| `order` | Execution sequence. **0 = skip** this row. |
| `target_version` | May be edited (Nexus policy is not re-checked). |
| other columns | Informational; used to match rows to discovered artifacts |

**Same `order` on multiple rows** → applied in **one round** (all POM edits, then
one build + Codex cycle, one commit).

Example:

```csv
order,kind,group_id,artifact_id,current_version,target_version,...
1,dependency,com.google.guava,guava,32.0.0-jre,32.1.0-jre,...
2,plugin,org.apache.maven.plugins,maven-surefire-plugin,2.22.2,3.0.0,...
0,dependency,joda-time,joda-time,2.10,2.12.7,...    ← skipped
3,dependency,foo,bar,1.0,2.0,...
3,dependency,baz,qux,1.0,2.0,...                    ← same round as bar
```

```bash
mvn-upgrade plan --config config.yaml
# edit upgrade-plan.csv
mvn-upgrade run --config config.yaml --apply --plan-file upgrade-plan.csv
```

## Follow-along run log

Every `plan` and `run` writes a line-buffered log under the target repo (default
`.mvn-upgrade-work/run.log`). The tool prints the path at startup — follow it
from another terminal:

```bash
tail -f /path/to/your/maven-project/.mvn-upgrade-work/run.log
```

## How it decides what to upgrade

- **Discovery (`pom.py`)** runs `mvn help:effective-pom` to get resolved
  versions, then inspects raw POMs to classify each artifact's editable version
  source: `literal`, `property`, `managed` (BOM/`dependencyManagement`), or
  `parent`. If Maven is unavailable it falls back to resolving an approximate
  effective model from the raw POMs.
- **Policy (`versioning.py`)** uses a faithful port of Maven's
  `ComparableVersion` for ordering. By default it excludes pre-releases
  (`alpha/beta/rc/cr/milestone/snapshot/...`), forbids major-version jumps,
  drops versions `<= current`, honors `include`/`exclude` globs, `pin`, and
  `ignore_versions`, then picks the highest remaining candidate.
- **Apply (`apply.py`)** uses one of four strategies, preferring
  `versions-maven-plugin` goals (`use-dep-version`, `set-property`,
  `update-parent`) and falling back to a precise targeted XML edit (used for
  plugins, which have no exact-version goal, and whenever a goal can't apply).
- **Parent POMs last (`policy.parent_last`, default `true`)** — coordinates
  like `spring-boot-starter-parent` are planned and executed **after** all
  individual dependencies and plugins. Nexus lookup uses `extension=pom` for
  parent coordinates. Set `parent_last: false` to interleave with other upgrades.

## Baseline check (before upgrading)

Before any dependency is bumped, `run --apply` runs the configured build once.
Controlled by `run.baseline` (or `--baseline`):

| Mode | Behavior |
|------|----------|
| `ask` (default) | Prompt: **Codex fix** / skip failing tests / abort |
| `fix-codex` | Use Codex to fix compile/test failures, commit, then upgrade |
| `skip-failing` | Exclude pre-existing **test** failures; compile errors → Codex fix |
| `abort` | Stop if baseline is red |
| `off` | Skip baseline |

For compile errors (e.g. `package org.joda.time does not exist`), Codex may add
missing dependencies to `pom.xml` — it does **not** bump existing versions.

```bash
# Non-interactive: always Codex-fix a red baseline, then upgrade
mvn-upgrade run --config my-config.yaml --apply --baseline fix-codex
```

## The fix loop

The orchestrator owns the loop and **always re-runs Maven** to judge success —
Codex's self-report is never trusted.

```
for attempt in 1..max_fix_attempts (default 4):
    build → green? commit & done
    last attempt? stop
    same error signature as last time? stop (no progress)
    codex exec --sandbox workspace-write --ask-for-approval never <prompt>
```

A failed item is hard-reset to its checkpoint (wiping POM edits **and** any
Codex code changes) and recorded as `skipped-build-failed`. With
`run.on_failure: abort`, the run stops at the first failure instead.

## Safety & secrets

- All external commands run as argument lists (never `shell=True` with
  interpolated strings).
- Build logs and Codex output are redacted before being written.
- Report files, build logs (`.mvn-upgrade-work/`), and the helper `AGENTS.md`
  are kept out of per-artifact commits; the report is committed last as a
  separate `docs:` commit.
- Run state (`dependency-updates.json`) is persisted after every item, so a
  crashed run resumes and already-upgraded artifacts are skipped.

## Statuses in the report

`upgraded`, `skipped-build-failed`, `skipped-no-newer`, `not-in-nexus`,
`managed-external`, `informational`, `error`.

## Development

```bash
pytest            # full unit/acceptance suite (no mvn/codex/glab required)
```

The pure logic (version ordering, policy, discovery/classification, report,
command construction) is fully covered by tests that mock all external tools.
External-command construction is isolated per module (`apply`, `build`, `codex`,
`git_ops`, `gitlab_mr`) for easy mocking.
