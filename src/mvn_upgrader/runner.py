"""The mutating upgrade loop: apply -> build -> codex-fix -> commit / revert.

One artifact at a time, one commit per success. A failed item is reset hard to
its checkpoint (wiping POM edits and any Codex code changes) and recorded as
skipped-build-failed; on ``on_failure: abort`` the run stops there.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable, Optional

from . import apply as apply_mod
from . import baseline as baseline_mod
from . import build as build_mod
from . import codex as codex_mod
from .config import Config
from .git_ops import Git, GitError
from .models import PlanItem, Status, UpgradeResult
from .nexus import NexusClient
from .planner import build_plan
from .pom import discover
from .preflight import run_preflight
from .report import RunState, load_state, now_iso, today, write_reports

log = logging.getLogger("mvn_upgrader.runner")


def _default_prompt(message: str) -> Optional[str]:
    """Read a line from the user; return None when no interactive TTY is available."""
    if not sys.stdin or not sys.stdin.isatty():
        return None
    try:
        return input(message)
    except EOFError:
        return None


def _commit_message(item: PlanItem) -> str:
    a = item.artifact
    return f"build(deps): bump {a.ga} from {a.current_version} to {item.target_version}"


def _result_from_item(item: PlanItem, status: Status, **kw) -> UpgradeResult:
    a = item.artifact
    return UpgradeResult(
        group_id=a.group_id,
        artifact_id=a.artifact_id,
        kind=a.kind,
        version_source=a.version_source,
        old_version=a.current_version,
        new_version=item.target_version,
        status=status,
        co_moved=list(item.co_moved),
        **kw,
    )


def _filter_plan(
    plan: list[PlanItem], only: list[str], max_items: Optional[int]
) -> list[PlanItem]:
    if only:
        only_set = set(only)
        plan = [i for i in plan if i.ga in only_set or set(i.co_moved) & only_set]
    if max_items is not None:
        plan = plan[:max_items]
    return plan


def _git_exclude(repo: Path, entries: list[str]) -> None:
    """Add entries to .git/info/exclude so tool artifacts stay out of commits."""
    exclude = repo / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prev = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        lines = set(prev.splitlines())
        new = [e for e in entries if e not in lines]
        if new:
            text = prev
            if text and not text.endswith("\n"):
                text += "\n"
            exclude.write_text(text + "\n".join(new) + "\n", encoding="utf-8")
    except OSError:
        pass


def _rel(repo: Path, p: Path) -> str:
    try:
        return str(p.relative_to(repo))
    except ValueError:
        return str(p)


def _setup_run(git: Git, repo: Path, cfg: Config) -> bool:
    """Exclude tool artifacts from commits; write AGENTS.md. Returns created_agents."""
    from .build import WORKDIR_NAME

    md_path, json_path = _report_paths(cfg)
    _git_exclude(repo, [
        WORKDIR_NAME + "/",
        _rel(repo, md_path),
        _rel(repo, json_path),
        "AGENTS.md",
    ])
    agents = repo / "AGENTS.md"
    if agents.exists():
        return False
    agents.write_text(codex_mod.agents_md_content(cfg), encoding="utf-8")
    return True


def _generic_fix_loop(
    cfg: Config,
    *,
    build_fn: Callable,
    codex_invoke: Callable[[Config, str], None],
    tag_prefix: str,
) -> tuple[bool, int, Optional[str], Optional[build_mod.BuildResult]]:
    """Bounded build → Codex loop. ``codex_invoke(cfg, log_tail)`` runs one fix."""
    max_attempts = cfg.codex.max_fix_attempts
    prev_sig: Optional[str] = None
    fix_attempts = 0
    last_sig: Optional[str] = None
    last_build: Optional[build_mod.BuildResult] = None
    safe_tag = tag_prefix.replace(":", "_").replace("/", "_")
    for attempt in range(1, max_attempts + 1):
        print(f"  build attempt {attempt}/{max_attempts}...")
        res = build_fn(cfg, attempt_tag=f"{safe_tag}-{attempt}")
        last_build = res
        if res.ok:
            return True, fix_attempts, None, res
        last_sig = res.error_signature
        if attempt == max_attempts:
            break
        if (
            cfg.codex.stop_on_no_progress
            and res.error_signature is not None
            and res.error_signature == prev_sig
        ):
            print(
                "  no progress (same error as last attempt); stopping Codex loop. "
                "Set codex.stop_on_no_progress=false to keep retrying."
            )
            break
        prev_sig = res.error_signature
        print(f"  invoking Codex fix (attempt {fix_attempts + 1}/{max_attempts})...")
        codex_invoke(cfg, res.tail)
        fix_attempts += 1
    return False, fix_attempts, last_sig, last_build


def _fix_loop(
    cfg: Config,
    item: PlanItem,
    *,
    build_fn: Callable,
    codex_fn: Callable,
) -> tuple[bool, int, Optional[str]]:
    """Bounded build/codex loop for a single dependency upgrade."""

    def invoke(c, tail):
        codex_fn(c, item, tail, build_cmd=c.maven.build_command)

    return _generic_fix_loop(
        cfg, build_fn=build_fn, codex_invoke=invoke, tag_prefix=item.ga
    )


def _preexisting_fix_loop(
    cfg: Config,
    *,
    build_fn: Callable,
    codex_baseline_fn: Callable,
) -> tuple[bool, int, Optional[str]]:
    def invoke(c, tail):
        r = codex_baseline_fn(c, tail, build_cmd=c.maven.build_command)
        if r is None:
            return
        print(f"  Codex exited with code {r.exit_code}")
        if r.stdout.strip():
            snippet = r.stdout.strip().splitlines()[-1][:200]
            print(f"  Codex: {snippet}")
        if r.exit_code != 0 and r.stderr.strip():
            err = r.stderr.strip().splitlines()[-1][:300]
            print(f"  Codex error: {err}")

    return _generic_fix_loop(
        cfg, build_fn=build_fn, codex_invoke=invoke, tag_prefix="preexisting"
    )


def _decide_baseline_action(
    cfg: Config, result: baseline_mod.BaselineResult, prompt_fn: Callable
) -> str:
    """Return ``codex``, ``skip`` (tests only), or ``abort``."""
    mode = cfg.run.baseline
    if mode == "fix-codex":
        return "codex"
    if mode == "abort":
        return "abort"
    if mode == "skip-failing":
        return "skip" if result.has_failing_tests else "codex"

    # mode == "ask"
    if not result.has_failing_tests:
        answer = prompt_fn(
            "\nPre-existing build failure (compile/resolution, not a test).\n"
            "Fix with Codex before upgrading? [Y/n/a] "
        )
        if answer is None:
            print("(no interactive input; defaulting to Codex fix)")
            return "codex"
        answer = answer.strip().lower()
        if answer in ("", "y", "yes", "c", "codex"):
            return "codex"
        return "abort"

    n = len(result.failures)
    answer = prompt_fn(
        f"\n{n} pre-existing failing test(s) before any upgrade.\n"
        "[C] Fix with Codex  [S] Skip failing tests and continue  [A] Abort [C]: "
    )
    if answer is None:
        print("(no interactive input; defaulting to Codex fix)")
        return "codex"
    answer = answer.strip().lower()
    if answer in ("s", "skip"):
        return "skip"
    if answer in ("a", "abort", "n", "no"):
        return "abort"
    return "codex"


def _print_build_diagnostics(build, *, tail_lines: int = 40) -> None:
    """Surface where the baseline log is and show its tail to aid debugging."""
    if build is None:
        return
    if build.log_path:
        print(f"baseline build log: {build.log_path}")
    if build.tail:
        tail = "\n".join(build.tail.splitlines()[-tail_lines:])
        print("---- last lines of baseline build ----")
        print(tail)
        print("---------------------------------------")


def _baseline_gate(
    cfg: Config,
    git: Git,
    *,
    baseline_fn: Callable,
    build_fn: Callable,
    codex_baseline_fn: Callable,
    prompt_fn: Callable,
) -> Optional[int]:
    """Run baseline build; fix, skip tests, or abort on pre-existing failures."""
    if cfg.run.baseline == "off":
        return None

    print("\nrunning baseline build to check current project health...")
    result = baseline_fn(cfg)
    if result.ok:
        print("baseline build is green.")
        return None

    if result.has_failing_tests:
        failures = result.failures
        preview = ", ".join(ft.selector for ft in failures[:8])
        more = "" if len(failures) <= 8 else f" (+{len(failures) - 8} more)"
        print(f"baseline build is RED: {len(failures)} pre-existing failing test(s): "
              f"{preview}{more}")
    else:
        print(
            "baseline build is RED (compilation, dependency-resolution, or config — "
            "not a tracked test failure)."
        )
        _print_build_diagnostics(result.build)

    action = _decide_baseline_action(cfg, result, prompt_fn)
    if action == "abort":
        print("aborting — fix the build manually or use --baseline fix-codex.")
        return 1

    if action == "skip":
        excludes = baseline_mod.to_test_excludes(result.failures)
        cfg.maven.test_excludes = list(excludes)
        print(f"proceeding; excluding {len(excludes)} pre-existing failing test(s) "
              "from subsequent builds.")
        return None

    # action == "codex"
    print("attempting to fix pre-existing build failure(s) with Codex...")
    checkpoint = git.checkpoint()
    green, attempts, sig, last_build = _preexisting_fix_loop(
        cfg, build_fn=build_fn, codex_baseline_fn=codex_baseline_fn
    )
    if not green:
        git.restore_to(checkpoint)
        print(f"pre-existing build still red after {attempts} Codex fix attempt(s).")
        if sig:
            print(f"last error signature: {sig}")
        _print_build_diagnostics(last_build or result.build)
        return 1

    sha = git.commit_all("fix: resolve pre-existing build failures")
    print(f"pre-existing build is green after {attempts} Codex fix attempt(s)"
          + (f" (commit {sha[:10]})" if sha else "") + ".")
    return None


def run_upgrades(
    cfg: Config,
    *,
    create_mr: bool = False,
    only: Optional[list[str]] = None,
    max_items: Optional[int] = None,
    plan_override: Optional[tuple[list[PlanItem], list[UpgradeResult]]] = None,
    nexus: Optional[NexusClient] = None,
    git_factory: Callable[..., Git] = Git,
    build_fn: Callable = build_mod.run,
    codex_fn: Callable = codex_mod.run_fix,
    codex_baseline_fn: Callable = codex_mod.run_baseline_fix,
    apply_fn: Callable = apply_mod.apply_item,
    baseline_fn: Callable = baseline_mod.run_baseline,
    prompt_fn: Callable = _default_prompt,
    skip_preflight: bool = False,
) -> int:
    only = only or []

    if not skip_preflight:
        pf = run_preflight(cfg, need_mutation=True, need_codex=True, need_mr=create_mr)
        print(pf.render())
        if not pf.ok:
            print("\npreflight failed; aborting.")
            return 1

    # ---- build plan -------------------------------------------------------
    if plan_override is not None:
        plan, base_results = plan_override
        used_fallback = False
    else:
        disc = discover(cfg)
        used_fallback = disc.used_fallback
        client = nexus or (NexusClient.from_config(cfg) if cfg.nexus.configured else None)
        plan, base_results = build_plan(cfg, disc.artifacts, client)

    # Carry over non-actionable results (info / not-in-nexus / etc); the PENDING
    # entries are recomputed per item below.
    carry = [r for r in base_results if r.status != Status.PENDING]
    plan = _filter_plan(plan, only, max_items)

    # ---- idempotency / resume --------------------------------------------
    branch = f"{cfg.git.branch_prefix}/{today()}"
    prior = load_state(cfg)
    already_upgraded: dict[str, UpgradeResult] = {}
    if prior is not None:
        for r in prior.results:
            if r.status == Status.UPGRADED:
                already_upgraded[r.ga] = r
    resumed_results = [
        already_upgraded[i.ga] for i in plan if i.ga in already_upgraded
    ]
    plan = [i for i in plan if i.ga not in already_upgraded]
    if resumed_results:
        print(f"resuming: {len(resumed_results)} artifact(s) already upgraded; "
              "skipping them.")

    if not plan:
        print("\nNothing to upgrade.")
        _write(cfg, carry + resumed_results, mode="run", branch=branch,
               base_branch=cfg.git.base_branch, used_fallback=used_fallback)
        return 0

    # ---- git setup --------------------------------------------------------
    repo = cfg.repo
    git = git_factory(repo)
    if not git.is_clean():
        print("working tree not clean; aborting.")
        return 1

    resume_branch = (
        prior is not None and prior.branch == branch
        and bool(resumed_results) and git.branch_exists(branch)
    )
    try:
        if resume_branch:
            git.checkout(branch)
        else:
            git.create_branch(branch, cfg.git.base_branch, remote=cfg.git.remote)
    except GitError as exc:
        print(f"could not switch to branch: {exc}")
        return 1

    created_agents = _setup_run(git, repo, cfg)

    gate_rc = _baseline_gate(
        cfg, git,
        baseline_fn=baseline_fn,
        build_fn=build_fn,
        codex_baseline_fn=codex_baseline_fn,
        prompt_fn=prompt_fn,
    )
    if gate_rc is not None:
        if created_agents:
            (repo / "AGENTS.md").unlink(missing_ok=True)
        return gate_rc

    results: list[UpgradeResult] = list(carry) + list(resumed_results)
    successes = len(resumed_results)
    aborted = False
    try:
        for item in plan:
            res = _process_item(cfg, git, item, build_fn=build_fn, codex_fn=codex_fn,
                                 apply_fn=apply_fn)
            results.append(res)
            # Persist after every item so a crashed run can resume.
            _write(cfg, results, mode="run", branch=branch,
                   base_branch=cfg.git.base_branch, used_fallback=used_fallback)
            if res.status == Status.UPGRADED:
                successes += 1
            elif res.status in (Status.SKIPPED_BUILD_FAILED, Status.ERROR):
                if cfg.run.on_failure == "abort":
                    print(f"aborting after failure on {item.ga} (on_failure=abort)")
                    aborted = True
                    break
    finally:
        if created_agents:
            (repo / "AGENTS.md").unlink(missing_ok=True)

    # ---- finalize report --------------------------------------------------
    state = _write(cfg, results, mode="run", branch=branch,
                   base_branch=cfg.git.base_branch, used_fallback=used_fallback)
    md_path, _ = _report_paths(cfg)

    mr_url = None
    if successes:
        _commit_report(cfg, git)
        if create_mr:
            mr_url = _open_mr(cfg, git, branch, md_path)
            if mr_url:
                state.mr_url = mr_url
                write_reports(cfg, state)

    _print_summary(results, successes, branch, mr_url, aborted)
    return 0


def _process_item(cfg, git: Git, item: PlanItem, *, build_fn, codex_fn, apply_fn) -> UpgradeResult:
    checkpoint = git.checkpoint()
    print(f"\n-> {item.artifact.kind.value} {item.ga} "
          f"{item.artifact.current_version} -> {item.target_version}")

    ar = apply_fn(cfg, item)
    if not ar.ok:
        git.restore_to(checkpoint)
        return _result_from_item(item, Status.ERROR,
                                 notes=[f"apply failed: {ar.detail}"])

    # The apply must touch only POM files; otherwise revert defensively.
    changed = git.changed_files()
    nonpom = [f for f in changed if not f.endswith(".xml") and Path(f).name != "pom.xml"]
    if nonpom:
        git.restore_to(checkpoint)
        return _result_from_item(item, Status.ERROR,
                                 notes=[f"apply touched non-POM files: {nonpom}"])

    green, fix_attempts, sig, _ = _fix_loop(cfg, item, build_fn=build_fn, codex_fn=codex_fn)
    if green:
        sha = git.commit_all(_commit_message(item))
        notes = []
        if item.co_moved:
            notes.append("also moved: " + ", ".join(item.co_moved))
        return _result_from_item(item, Status.UPGRADED, commit=sha,
                                 fix_attempts=fix_attempts, notes=notes)

    git.restore_to(checkpoint)
    notes = [f"build red after {fix_attempts} fix attempt(s)"]
    if sig:
        notes.append(f"error signature {sig}")
    return _result_from_item(item, Status.SKIPPED_BUILD_FAILED,
                             fix_attempts=fix_attempts, notes=notes)


def _commit_report(cfg, git: Git) -> None:
    md_path, json_path = _report_paths(cfg)
    paths = [_rel(cfg.repo, md_path), _rel(cfg.repo, json_path)]
    git.commit_files("docs: add dependency-updates report", paths, force=True)


def _open_mr(cfg, git: Git, branch: str, md_path: Path) -> Optional[str]:
    from . import gitlab_mr

    try:
        git.push(cfg.git.remote, branch)
    except GitError as exc:
        print(f"push failed: {exc}")
        return None
    return gitlab_mr.create_mr(cfg, branch=branch, description_file=md_path)


# ---- report helpers -------------------------------------------------------
def _report_paths(cfg):
    from .report import report_paths
    return report_paths(cfg)


def _write(cfg, results, *, mode, base_branch, branch=None, used_fallback=False):
    state = RunState(
        generated_at=now_iso(), mode=mode, branch=branch,
        base_branch=base_branch, used_fallback=used_fallback, results=results,
        baseline_excluded_tests=list(getattr(cfg.maven, "test_excludes", []) or []),
    )
    write_reports(cfg, state)
    return state


def _print_summary(results, successes, branch, mr_url, aborted):
    print("\n=== Summary ===")
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    print(f"  branch: {branch}")
    if mr_url:
        print(f"  merge request: {mr_url}")
    if aborted:
        print("  (run aborted early)")
