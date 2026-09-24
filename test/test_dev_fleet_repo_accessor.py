"""``MAIN_REPO`` reaches git and the filesystem only through ``_repo()``.

``MAIN_REPO`` reaches git and the filesystem only through the accessors.

Dev Fleet represents "no main checkout found" as an empty string in
``MAIN_REPO``. That sentinel is fail-open at any call site that consumes the
global directly: ``git -C ""`` does not fail — it silently runs against the
backend process's working directory — and ``Path("")`` is ``Path(".")``, so an
unguarded consumer operates on an arbitrary directory and returns plausible
results. ``_repo_read()`` centralizes the guard: it returns the path or
raises ``RepoNotConfigured``, which the HMAC middleware converts to the 409
``repo_not_configured`` boundary. ``_repo()`` is the MUTATING accessor and adds
one refusal on top — a checkout served read-only — and it reaches the path
through ``_repo_read()``, so exactly one function still reads the global.

Two enforcement tiers (same pattern as ``test_apps_instances_loop_offload.py``):

- Behavior tests: ``_repo_read()`` raises on the empty sentinel and returns the
  path otherwise; ``_repo()`` additionally raises ``RepoReadOnly`` while the
  read-only state is set. Both preserve the exception types the middleware
  boundary maps.
- AST ratchet: outside the read accessor itself, a ``MAIN_REPO`` load may appear
  ONLY as a bare truthiness guard (``if MAIN_REPO:`` / ``not MAIN_REPO`` / a
  ``BoolOp`` operand). Any other load — a git argv element, a subprocess
  ``cwd=``, a ``Path(...)`` build, an f-string interpolation, a payload
  field — fails this test, so a future call site cannot silently reintroduce
  the fail-open shape.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import locale
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    gateway_routes,
    http_api,
    live,
    repository,
    runtime,
    server,
    worktree_ops,
)

# The read accessor is the ONLY function whose body may read the bare global: it
# IS the guard, and the mutating accessor delegates to it rather than loading the
# global a second time, so the count of functions touching MAIN_REPO stays at one.
# The startup hook's discovery/re-resolve runs on a local and writes the global
# exactly once (a Store, which this ratchet ignores), so even the assignment site
# needs no exemption — and a git call added to startup, where MAIN_REPO is most
# often still unresolved, is caught like anywhere else.
_DEV_FLEET_MODULES = (
    runtime,
    repository,
    live,
    fleet_state,
    worktree_ops,
    http_api,
    server,
)
_ALLOWED_LOADS = {(repository.__name__, "_repo_read")}


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(cur)
    return None


def _is_bare_truthiness(node: ast.expr, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when the load feeds a truthiness test and nothing else.

    Walking up from the Name, only ``BoolOp`` and ``not`` may intervene before
    the expression lands as the ``test`` of an ``if``/``while`` or a ternary.
    Any other intervening node (a call argument, a container literal, an
    f-string, an assignment value) means the VALUE escapes, which is exactly
    the shape the accessor exists to prevent.
    """
    child: ast.AST = node
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.BoolOp, ast.UnaryOp)):
            if isinstance(cur, ast.UnaryOp) and not isinstance(cur.op, ast.Not):
                return False
            child = cur
            cur = parents.get(cur)
            continue
        if isinstance(cur, (ast.If, ast.While)):
            return cur.test is child
        if isinstance(cur, ast.IfExp):
            return cur.test is child
        return False
    return False


def test_main_repo_loads_only_via_accessor_or_truthiness() -> None:
    violations: list[str] = []
    for module in _DEV_FLEET_MODULES:
        tree = ast.parse(inspect.getsource(module))
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            is_main_repo = (isinstance(node, ast.Name) and node.id == "MAIN_REPO") or (
                isinstance(node, ast.Attribute) and node.attr == "MAIN_REPO"
            )
            if not is_main_repo or not isinstance(node.ctx, ast.Load):
                continue  # assignments (Store) stay on the global by design
            func = _enclosing_function(node, parents)
            if (module.__name__, func) in _ALLOWED_LOADS:
                continue
            if _is_bare_truthiness(node, parents):
                continue
            violations.append(
                f"{module.__name__}:{node.lineno}: MAIN_REPO load in "
                f"{func or '<module>'} — route it through repository._repo()"
                " (or _repo_read() for a read-only consumer)"
            )
    assert not violations, (
        "MAIN_REPO's empty-string sentinel is fail-open when consumed "
        "directly (git -C '' runs against the process CWD). Use _repo():\n" + "\n".join(violations)
    )


def test_repo_accessor_raises_on_unresolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    with pytest.raises(repository.RepoNotConfigured):
        repository._repo()


def test_repo_accessor_returns_resolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
    assert repository._repo() == "/somewhere/kirocrew"


def test_read_accessor_raises_on_unresolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The weaker accessor still refuses the fail-open sentinel.

    It is the one the ratchet admits, so if it ever stopped raising here the
    ratchet would be guarding a function that hands out ``""``.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    with pytest.raises(repository.RepoNotConfigured):
        repository._repo_read()


def test_read_accessor_serves_a_checkout_the_app_may_only_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only is the whole point of the split: the generic surface still works."""
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    assert repository._repo_read() == "/somewhere/other-project"


def test_mutating_accessor_refuses_a_checkout_the_app_may_only_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal every mutating call site inherits without being touched.

    ``RepoReadOnly`` must keep sharing the ``RepoUnavailable`` base, because the
    degrade sites catch that base and their "not derivable" answer is the right
    one here too.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    with pytest.raises(repository.RepoReadOnly) as caught:
        repository._repo()
    assert isinstance(caught.value, repository.RepoUnavailable)
    assert "read-only" in str(caught.value)


def test_mutating_accessor_allows_a_marker_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate does not fire when the state is genuinely empty."""
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    assert repository._repo() == "/somewhere/kirocrew"


def test_read_only_reason_reports_the_state_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    """One reader for the route boundary, the payload and the row fields."""
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    assert repository._read_only_reason() is None
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    assert repository._read_only_reason() == "read-only: no markers"


@pytest.mark.parametrize(
    "name, ok",
    [
        ("main", True),
        ("trunk", True),
        ("release/2.0", True),
        ("feature.x", True),
        # A leading dash is parsed as a FLAG by git once the name is
        # interpolated into an argv, so it must never be accepted.
        ("--exec=touch /tmp/pwn", False),
        ("-main", False),
        # ``..`` splits a rev range at the wrong place: ``origin/a..b..HEAD``.
        ("a..b", False),
        ("", False),
        ("main branch", False),
        ("main;rm", False),
    ],
)
def test_base_branch_names_are_constrained_before_reaching_an_argv(name: str, ok: bool) -> None:
    assert repository._plausible_branch_name(name) is ok


def test_every_local_base_candidate_survives_the_argv_constraint() -> None:
    """The fallback list and the argv guard must agree.

    A candidate the guard rejects would be published into ``BASE_BRANCH`` by the
    fallback loop without ever meeting ``_plausible_branch_name``, which only
    screens the remote's answer. Asserting over the tuple itself keeps a name
    added later from slipping past.
    """
    assert repository._LOCAL_BASE_CANDIDATES
    for candidate in repository._LOCAL_BASE_CANDIDATES:
        assert repository._plausible_branch_name(candidate) is True


def test_primary_checkout_resolution_preserves_the_host_text_decoder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Moving the startup probe must not reinterpret non-ASCII checkout paths."""
    primary = tmp_path / "primary"
    seen: dict[str, object] = {}

    def _run(_argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=str(primary / ".git"))

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    _unwrapped_sandbox(monkeypatch)
    monkeypatch.setattr(repository.subprocess, "run", _run)

    assert repository._resolve_primary_checkout(str(tmp_path / "linked")) == str(primary)
    assert seen["text"] is True
    assert seen["encoding"] == locale.getpreferredencoding(False)


# --- base-branch resolution reads ONE remote ---------------------------------
#
# ``git remote`` lists names alphabetically, so a checkout carrying an archive or
# fork remote beside ``origin`` hands the first-listed one the casting vote. That
# remote decides ``BASE_BRANCH`` while ``_upstream_remote`` resolves to ``origin``
# independently, and ``/rebase`` rewrites onto ``{remote}/{BASE_BRANCH}`` — a base
# the upstream never published. These three pin which remote is consulted.


def _stub_base_branch_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    remotes: str,
    published: dict[str, str],
    local: set[str],
    head: str | None = None,
) -> list[str]:
    """Wire the two git readers ``_resolve_base_branch`` uses. Returns the ref probes."""
    probed: list[str] = []

    async def _run_cmd(argv, **_kwargs):
        assert argv[-1] == "remote", argv
        return 0, remotes, ""

    async def _git(_repo: str, *args: str) -> str | None:
        if args[0] == "symbolic-ref":
            ref = args[-1]
            probed.append(ref)
            if ref == "HEAD":
                return head
            remote = ref.split("/")[2]
            published_head = published.get(remote)
            return f"{remote}/{published_head}" if published_head else None
        if args[0] == "rev-parse":
            name = args[-1].removeprefix("refs/heads/")
            return name if name in local else None
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)
    monkeypatch.setattr(repository, "_git", _git)
    return probed


@pytest.mark.asyncio
async def test_base_branch_ignores_a_remote_sorted_before_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alphabetically earlier remote must not decide the rebase base.

    ``archive`` publishes one default and ``origin`` another. Only ``origin`` may
    be consulted, because ``_upstream_remote`` resolves to it and the two answers
    are combined into a single rev range.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="archive\norigin\n",
        published={"archive": "legacy-default", "origin": "trunk"},
        local=set(),
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    assert probed == ["refs/remotes/origin/HEAD"]


@pytest.mark.asyncio
async def test_base_branch_reads_a_sole_remote_under_another_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One remote is unambiguous whatever it is called, so its answer is taken."""
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="kirocrew\n",
        published={"kirocrew": "release/3"},
        local=set(),
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "release/3"
    assert probed == ["refs/remotes/kirocrew/HEAD"]


@pytest.mark.asyncio
async def test_base_branch_falls_back_locally_when_no_remote_is_unambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several remotes and no ``origin`` is ambiguous: ask the local branches."""
    local_default = repository._LOCAL_BASE_CANDIDATES[-1]
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="fork\nupstream\n",
        published={"fork": "a", "upstream": "b"},
        local={local_default},
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == local_default
    assert probed == []


@pytest.mark.asyncio
async def test_base_branch_takes_the_checked_out_branch_when_no_candidate_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository whose base is named something else answers with its own HEAD.

    No published remote default, and no name in ``_LOCAL_BASE_CANDIDATES`` exists, so
    every earlier tier declines. Keeping the default would label the primary row with
    a name that matches no ref and query ``{remote}/<that name>`` against it.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\n",
        published={},
        local={"trunk"},
        head="trunk",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    assert probed == ["refs/remotes/origin/HEAD", "HEAD"]


@pytest.mark.asyncio
async def test_a_present_candidate_outranks_the_checked_out_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD is the LAST tier, because a dev checkout sits on a feature branch.

    ``main`` exists here, so it is the base even though the checkout is parked on
    someone's branch -- taking HEAD would retarget every rebase and every ahead/behind
    reading at that branch for as long as it stays checked out.
    """
    candidate = repository._LOCAL_BASE_CANDIDATES[0]
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\n",
        published={},
        local={candidate, "feature/some-work"},
        head="feature/some-work",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == candidate
    assert "HEAD" not in probed


@pytest.mark.asyncio
async def test_an_implausible_head_is_refused_like_every_other_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last tier is validated too, or it becomes the way a bad name gets in."""
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\n",
        published={},
        local=set(),
        head="--upload-pack=touch /tmp/pwned",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "main"
    assert probed == ["refs/remotes/origin/HEAD", "HEAD"]


# --- reads leave the repository byte-identical -------------------------------


def test_optional_locks_are_off_for_every_git_this_handler_runs() -> None:
    """``git status`` rewrites the index unless optional locks are off.

    It is a read to its caller and a write to the repository: it refreshes the
    index's stat cache and saves it back under ``index.lock``. Every fleet render
    runs one per row, so without this a checkout the app may only read is modified
    on its ordinary path. Pinned on the env chokepoint rather than per call site,
    which is what makes a read added later inherit it.
    """
    assert runtime._GIT_ENV_NEUTRALIZERS["GIT_OPTIONAL_LOCKS"] == "0"


@pytest.mark.asyncio
async def test_run_cmd_puts_the_neutralizers_in_the_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dict is only a guarantee if the spawn actually carries it.

    Asserted through the spawn preparation, because that is the last place the env
    can be read before the child exists, and an entry dropped anywhere earlier
    would leave the dict stating a pin nothing applies.
    """
    seen: dict[str, str] = {}

    def _prepare(cmd, _mode, env=None):
        seen.update(env or {})
        return list(cmd), dict(env or {}), None

    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", _prepare)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "/usr/bin/git")

    async def _off_loop(fn, executor=None):
        return fn()

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _off_loop)

    async def _no_child(*_a, **_kw):
        raise AssertionError("the env is read before the child spawns")

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", _no_child)
    with pytest.raises(AssertionError):
        await runtime._run_cmd(["git", "-C", "/somewhere/other-project", "status"])

    for key, value in runtime._GIT_ENV_NEUTRALIZERS.items():
        assert seen[key] == value


# --- the gateway's own boundary carries the refusal --------------------------


@pytest.mark.asyncio
async def test_gateway_repo_resolution_refuses_a_read_only_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway routes skip the backend middleware, so the refusal lives here.

    Make Live is why it matters: it reaches ``_find_worktree_by_path`` and writes
    the live-target pointer, which would aim the running gateway at a checkout
    this app may only read.
    """

    async def _discovered() -> None:
        return None

    monkeypatch.setattr(repository, "ensure_main_repo_discovered", _discovered)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")

    refused = await gateway_routes._ensure_repo()
    assert refused is not None
    assert refused.status == 409
    assert json.loads(refused.body)["code"] == "repo_read_only"

    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    assert await gateway_routes._ensure_repo() is None


# --- a fenced path is refused, never served read-only ------------------------


def test_read_only_adoption_asks_the_central_path_gate() -> None:
    """The gate runs off the loop, on BOTH spellings, and BEFORE the probes.

    ``sensitive_path_refusal`` waits on the bounded path-resolution pool, so calling
    it in the coroutine body pauses every other task on the gateway loop. It also
    has to see the path the operator NAMED: ``_resolve_primary_checkout`` rewrites a
    linked worktree to its primary, and a fenced worktree can have a primary outside
    the fence, so gating only the rewritten form clears it by a path that is not it.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert "sensitive_path_refusal" not in body, "the gate must not run in the coroutine body"
    # Both spellings, each asked at its own time: the named path before anything
    # reads inside it, the resolved primary before the probes that read inside THAT.
    named = body.index('_fenced_reason, configured, ""')
    resolved = body.index('_fenced_reason, "", discovered')
    assert named < resolved
    # Anchored on the CALL text, not the bare names: the comment above these calls
    # names both functions, so a bare-name index would compare against prose.
    assert named < body.index("_discover_main_repo, configured"), "asked before the path is walked"
    assert resolved < body.index(
        "_configured_filter_commands(discovered)"
    ), "asked before git reads inside it"

    probe = inspect.getsource(repository._fenced_reason)
    assert "for candidate in (configured, resolved)" in probe


def test_a_foreign_read_runs_in_the_strict_tier_with_the_clearance_bound_to_the_spawn() -> None:
    """The sandbox tier bounds the harm; the clearance's PLACEMENT bounds the window.

    ``_run_cmd`` hands ``_GIT_TRUSTED_HELPERS`` only to ``standard``, which its own
    comment calls the gateway-controlled tier. A foreign checkout is repo-controlled,
    so its reads belong in ``strict``: a filter driver that wins the race then runs
    with no credential store visible and no helper config, which is the part no
    check-then-spawn can achieve. The clearance sits in ``pre_spawn``, evaluated
    after sandbox preparation with the spawn as the only await that follows, so the
    config proved clean is the config the child reads. Taking it ahead of the call
    instead would leave the preparation hop in between, and that hop is unbounded.
    """
    body = inspect.getsource(repository._git)
    assert 'mode = "strict"' in body
    assert "pre_spawn=_clear" in body
    # The clearance lives inside the hook, not in a bare await ahead of the call.
    bare = body.index("_assert_read_cleared")
    assert "except RepoUnreadable" in body[bare : bare + 300]


@pytest.mark.asyncio
async def test_the_clearance_uses_the_disposition_its_caller_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent switch must not turn the probe off for a read aimed at the old path.

    ``_git`` samples the read-only disposition to choose its branch, and the spawn's
    target path is fixed at that moment. Sampling the same global again inside the
    clearance can answer differently, because a poll re-resolving to the app's own
    managed checkout clears it -- and the probe would then be skipped entirely while
    the child still runs git inside the foreign checkout.
    """
    probed: list[tuple[str, bool]] = []

    async def record(path: str, *, read_only: bool) -> None:
        probed.append((path, read_only))

    monkeypatch.setattr(repository, "_assert_read_cleared", record)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")

    async def run_cmd(argv, *, timeout=6, mode="standard", pre_spawn=None, **_kw):
        # The switch the operator's own remedy produces, landing between the
        # disposition sample and the moment the hook runs.
        monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
        if pre_spawn is not None:
            await pre_spawn()
        return 0, "ok", ""

    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)

    assert await repository._git("/foreign/checkout", "status", "--porcelain") == "ok"
    assert probed == [("/foreign/checkout", True)]


@pytest.mark.asyncio
async def test_the_clearance_honours_its_argument_over_the_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe runs on the caller's ``read_only=True`` even with the global cleared.

    This is the other half: ``_git`` passing the captured disposition buys nothing if
    the clearance reads the global anyway. Driven directly, with the global already
    switched, the probe must still run and still refuse a configured driver.
    """

    def drivers(_path: str) -> tuple[set[str], str | None]:
        return {"filter.lfs.clean"}, None

    monkeypatch.setattr(repository, "_configured_filter_commands", drivers)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)

    with pytest.raises(repository.RepoUnreadable, match="filter drivers"):
        await repository._assert_read_cleared("/foreign/checkout", read_only=True)

    # And the opposite direction: the app's own checkout is not probed at all.
    def unreachable(_path: str) -> tuple[set[str], str | None]:
        raise AssertionError("the managed checkout must not be probed")

    monkeypatch.setattr(repository, "_configured_filter_commands", unreachable)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    await repository._assert_read_cleared("/our/own/checkout", read_only=False)


@pytest.mark.asyncio
async def test_a_read_whose_checkout_moved_is_refused_before_it_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clearance proved against the old checkout says nothing about the new one.

    A read already in flight carries the path it captured, so once a different
    checkout resolves, spawning would run git inside the one being left behind. The
    refusal is transient by design -- nothing latches, and the next poll reads the
    checkout now configured.
    """
    spawned: list[list[str]] = []

    async def cleared(path: str, *, read_only: bool) -> None:
        return None

    monkeypatch.setattr(repository, "_assert_read_cleared", cleared)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")

    async def run_cmd(argv, *, timeout=6, mode="standard", pre_spawn=None, **_kw):
        repository._drop_checkout_derived_state()
        if pre_spawn is not None:
            await pre_spawn()
        spawned.append(list(argv))
        return 0, "ok", ""

    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)

    with pytest.raises(repository.RepoUnreadable, match="checkout changed while reading"):
        await repository._git("/foreign/checkout", "status", "--porcelain")


def test_every_foreign_repo_probe_is_sandboxed() -> None:
    """No probe reads a checkout this app does not own outside the sandbox.

    git parses the target repository's config on EVERY command and follows
    ``include.path`` while doing so, so a foreign repo can point a probe at any
    file, a credential store included. ``--includes`` decides only whether an
    include is followed for the value PRINTED, not whether the file is opened, and
    the fence gates the checkout path rather than the files its config names.
    """
    src = inspect.getsource(repository)
    # Every spawn in this module goes through the helper that wraps the chokepoint.
    assert "subprocess.run(" not in src
    assert src.count("_probe_git(") == 5  # the helper plus its four call sites
    helper = inspect.getsource(repository._probe_git)
    assert 'sandboxed_spawn_argv(argv, mode="strict", env=env)' in helper
    assert "run_limited(" in helper
    assert "os.unlink(cleanup)" in helper


def test_an_unbuildable_sandbox_reads_as_unread_not_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that could not run has not answered ``no``.

    The filter cascade must report the scope unread, and the extension question
    must keep its third answer -- ``False`` there would drop ``--worktree`` and
    admit a repo whose worktree scope holds a driver.
    """

    def _no_sandbox(*_args, **_kwargs):
        raise RuntimeError("sandbox unavailable: no backend")

    monkeypatch.setattr(repository, "sandboxed_spawn_argv", _no_sandbox)
    drivers, unread = repository._scope_filter_keys("/repo", "/usr/bin/git", {}, "--local")
    assert drivers == []
    assert unread and "sandbox" in unread
    assert repository._worktree_config_scope_live("/repo", "/usr/bin/git", {}) is None


def test_permanent_security_refusals_are_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each permanent refusal is a permission decision, so it leaves a record.

    The sibling denials this app takes at its HTTP boundaries are audited; these two
    are taken during discovery and surfaced only as banner text, so without an event
    the decision is invisible. The UNMEASURED verdicts stay unaudited on purpose:
    an unresolved path gate and an unread config scope decided nothing.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert '_audit_security_refusal("path-fenced"' in body
    # Wrapped by the formatter, so the kind sits on its own line.
    assert '"filter-drivers", discovered' in body
    assert body.count("_audit_security_refusal(") == 2
    # The two unmeasured branches compose their message and audit nothing.
    unverified_at = body.index("unverified = True")
    assert "_audit_security_refusal" not in body[unverified_at : body.index("else:", unverified_at)]

    events: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    repository._audit_security_refusal("path-fenced", "/home/someone/.aws", "protected")
    assert events and events[0]["outcome"] == "denied"
    assert events[0]["tool_name"] == "dev-fleet:repo-path-fenced"

    # A failing sink must not mask the refusal it describes.
    class _Boom:
        def log_tool_invocation(self, **_kwargs):
            raise RuntimeError("sink down")

    monkeypatch.setattr(runtime, "_sel", lambda: _Boom())
    repository._audit_security_refusal("filter-drivers", "/repo", "filter.evil.process")


def test_the_path_gate_is_asked_in_both_directions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path that CONTAINS a protected location is refused too.

    ``sensitive_path_refusal`` answers "is this inside a protected location", which
    leaves the ancestor open: a home directory carrying a ``.git`` is not itself
    protected yet holds ``~/.aws`` and ``~/.ssh`` underneath, and ``/api/disk`` walks
    every worktree root recursively.
    """
    monkeypatch.setattr(repository, "sensitive_path_refusal", lambda _p: None)
    monkeypatch.setattr(repository, "path_contains_sensitive", lambda _p: True)
    reason = repository._fenced_reason("/home/someone", "")
    assert reason and "contains a protected location" in reason

    monkeypatch.setattr(repository, "path_contains_sensitive", lambda _p: False)
    assert repository._fenced_reason("/srv/other-project", "") is None


def test_a_fenced_path_is_never_read_before_it_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must come before any probe that reads INSIDE the path.

    ``git config --includes`` follows ``include.path``, so a probe of a protected
    location is exactly the read the fence exists to prevent: a repo-controlled
    include can name a credential file and git will read it as config. Walking the
    path and resolving its primary read inside it too. So a fenced candidate must be
    refused with NOTHING having touched it.
    """

    def _boom(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("a fenced path was read before it was refused")

    monkeypatch.setattr(repository, "_discover_main_repo", _boom)
    monkeypatch.setattr(repository, "_resolve_primary_checkout", _boom)
    monkeypatch.setattr(repository, "_configured_filter_commands", _boom)
    monkeypatch.setattr(repository, "_is_kirocrew_checkout", _boom)
    monkeypatch.setattr(repository, "_is_git_checkout", _boom)
    monkeypatch.setattr(repository, "_repo_source_hint", lambda: "Configured in dev_fleet.repo.")
    monkeypatch.setattr(
        repository,
        "_configured_main_repo_checked",
        lambda: ("/home/someone/.aws", True),
    )
    monkeypatch.setattr(
        repository, "sensitive_path_refusal", lambda p: "protected: credential store"
    )
    monkeypatch.setattr(repository, "_DISCOVERY_DONE", False)
    monkeypatch.setattr(repository, "_DISCOVERY_LOCK", None)
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)

    asyncio.run(repository.ensure_main_repo_discovered())

    assert repository._REPO_INVALID_MSG is not None
    assert "protected location" in repository._REPO_INVALID_MSG
    # Served as unreadable, never as a read-only fleet.
    assert repository._REPO_READ_ONLY_MSG is None


def test_a_fenced_path_is_refused_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fenced verdict publishes an unreadable refusal, never a read-only fleet."""
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/fenced")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", "refused: protected location")

    assert repository._read_only_reason() is None
    with pytest.raises(repository.RepoUnreadable):
        repository._repo_read()


def test_both_path_spellings_reach_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The named path and the primary it normalizes to are asked separately."""
    asked: list[str] = []

    def _refusal(path: str, base_dir=None):
        asked.append(path)
        return "fenced" if path == "/named/linked-worktree" else None

    monkeypatch.setattr(repository, "sensitive_path_refusal", _refusal)
    assert repository._fenced_reason("/named/linked-worktree", "/other/primary") == "fenced"
    assert asked == ["/named/linked-worktree"]

    asked.clear()
    assert repository._fenced_reason("/clean/named", "/clean/primary") is None
    assert asked == ["/clean/named", "/clean/primary"]


def test_an_unverified_gate_answer_does_not_latch() -> None:
    """A resolver stall is a measurement nobody took, so the next poll retries.

    Latched, one transient timeout would stand as a permanent refusal asserting a
    verdict that was never reached, and the retry the gate itself advises could
    never fire.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert "is_unverifiable_path_refusal(fenced)" in body
    assert "_DISCOVERY_DONE = bool(discovered) and not unverified" in body
    stall = body.index("is_unverifiable_path_refusal(fenced)")
    fence = body.index("is a protected location")
    assert stall < fence, "the stall branch must be taken before the protected-location wording"


def test_a_read_only_verdict_reopens_on_a_changed_config() -> None:
    """A read-only resolution is against a path the operator named and may correct.

    Both reopen gates key on the verdict pair, so a corrected `repo_path` re-resolves
    instead of serving the stranger's repository until the gateway restarts.
    """
    for source in (
        inspect.getsource(repository._invalid_resolution_is_stale),
        inspect.getsource(repository.ensure_main_repo_discovered),
    ):
        assert "(_REPO_INVALID_MSG or _REPO_READ_ONLY_MSG) and MAIN_REPO" in source


# --- a repository that would execute code on a read is refused ---------------


def test_executable_filter_drivers_refuse_the_adoption() -> None:
    """A filter driver is a COMMAND, and ``git status`` is what would run it.

    The env neutralizers pin the named execution vectors but cannot enumerate driver
    names, so a repo-local filter is the one that stays reachable on a read. Reading
    config executes nothing, which is what makes asking first the whole remedy.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert "_configured_filter_commands(discovered)" in body
    assert "if filters:" in body
    # The unread scope is its own branch: refusing a repository for a driver nobody
    # saw asserts a measurement that was never taken.
    assert "elif filters_unread:" in body
    assert repository._FILTER_COMMAND_SUFFIXES == (".clean", ".smudge", ".process")


def _unwrapped_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a probe reach the scripted git answer on a host with no sandbox backend.

    ``_probe_git`` fail-closes when no backend exists (CI containers and Windows
    have none), which is the production behaviour these tests are NOT about: they
    are about which question git is asked and what its answer means. The chokepoint
    itself is pinned by ``test_every_foreign_repo_probe_is_sandboxed``, and the
    fail-closed mapping by ``test_an_unbuildable_sandbox_reads_as_unread_not_as_clean``,
    so neutralising the wrapper here cannot hide either.
    """
    monkeypatch.setattr(
        repository,
        "sandboxed_spawn_argv",
        lambda argv, mode="standard", **kw: (list(argv), dict(kw.get("env") or {}), None),
    )


def _filter_probe_calls(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, tuple[int, str]]
) -> list[list[str]]:
    """Run the probe against scripted git answers, returning the argv it used.

    ``answers`` maps a distinguishing argv token to the ``(returncode, stdout)`` git
    should answer with. Nothing executes: the point is which questions are asked.
    """
    seen: list[list[str]] = []

    def _run(argv, **_kwargs):
        seen.append(list(argv))
        for token, (rc, out) in answers.items():
            if token in argv:
                return SimpleNamespace(returncode=rc, stdout=out)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    _unwrapped_sandbox(monkeypatch)
    monkeypatch.setattr(repository.subprocess, "run", _run)
    return seen


def test_filter_probe_follows_includes_on_every_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``include.path`` resolves during a read, so the probe must follow it too.

    For a SPECIFIC scope git defaults include-following OFF, so a driver reached
    through ``[include] path = other.cfg`` answers an empty list to a probe that
    omits ``--includes`` while still executing on the next content-touching read.
    """
    seen = _filter_probe_calls(monkeypatch, {"extensions.worktreeConfig": (0, "true\n")})
    drivers, unread = repository._configured_filter_commands("/somewhere/other-project")

    assert (drivers, unread) == ([], None)
    config_reads = [argv for argv in seen if "config" in argv]
    assert config_reads
    for argv in config_reads:
        assert "--includes" in argv


def test_filter_probe_asks_the_worktree_scope_only_when_it_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--worktree`` off is not a scope git reads, and asking anyway errors.

    Without ``extensions.worktreeConfig`` git ignores ``config.worktree`` entirely
    and refuses the query outright on any repository that has a linked worktree —
    the ordinary shape of what this app enumerates — so an unconditional ask turns
    a filter-free repository into a refusal.
    """
    off = _filter_probe_calls(monkeypatch, {"extensions.worktreeConfig": (1, "")})
    assert repository._configured_filter_commands("/repo") == ([], None)
    assert not [argv for argv in off if "--worktree" in argv]

    on = _filter_probe_calls(monkeypatch, {"extensions.worktreeConfig": (0, "true\n")})
    assert repository._configured_filter_commands("/repo") == ([], None)
    assert [argv for argv in on if "--worktree" in argv]


def test_filter_probe_reports_an_unread_scope_apart_from_a_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable scope is a measurement nobody took, not a configured driver."""
    _filter_probe_calls(
        monkeypatch,
        {
            "extensions.worktreeConfig": (0, "true\n"),
            "--get-regexp": (128, ""),
        },
    )
    drivers, unread = repository._configured_filter_commands("/repo")

    assert drivers == []
    assert unread and "could not be read" in unread


def test_filter_probe_reports_a_reason_when_git_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: None)
    drivers, unread = repository._configured_filter_commands("/somewhere/other-project")
    assert drivers == []
    assert unread and "unverified" in unread


def test_every_read_on_a_read_only_checkout_re_takes_the_clearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery's answer is about the repository as it was THEN.

    A driver written into the config after the adoption is read by the next git
    invocation and by nothing else, so the clearance is re-taken at the chokepoint
    every read passes through, and the repository stops being served at once. It is
    re-taken in ``pre_spawn``, so the stub below has to honour that hook the way
    ``_run_cmd`` does: the gate's whole point is that it sits after sandbox
    preparation with only the spawn behind it.
    """
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: /repo ...")
    monkeypatch.setattr(
        repository,
        "_configured_filter_commands",
        lambda _path: (["filter.evil.process"], None),
    )

    async def _never(*_args, pre_spawn=None, **_kwargs):
        if pre_spawn is not None:
            refusal = await pre_spawn()
            if refusal is not None:
                return -1, "", refusal
        raise AssertionError("git ran against a checkout that would execute its driver")

    monkeypatch.setattr(runtime, "_run_cmd", _never)

    with pytest.raises(repository.RepoUnreadable) as caught:
        asyncio.run(repository._git("/repo", "status", "--porcelain"))
    assert "filter.evil.process" in str(caught.value)


def test_the_product_s_own_checkout_pays_no_clearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trust boundary predates this mode: its config is the operator's own."""
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)

    def _refuse(_path):  # pragma: no cover - must not be reached
        raise AssertionError("probed the checkout this app owns")

    monkeypatch.setattr(repository, "_configured_filter_commands", _refuse)

    async def _ok(*_args, **_kwargs):
        return 0, "clean\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", _ok)
    assert asyncio.run(repository._git("/repo", "status", "--porcelain")) == "clean"


def test_an_unanswerable_worktree_scope_question_is_not_a_clean_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extension question failing must not silently drop the scope.

    A repository with ``extensions.worktreeConfig`` ON and ``filter.x.process`` in
    ``config.worktree`` would otherwise be admitted as filter-free, and the next
    ``git status`` would run that driver. Both failure shapes answer unread: a
    non-zero exit git does not use for "key absent", and a spawn that raises.
    """
    _filter_probe_calls(monkeypatch, {"extensions.worktreeConfig": (128, "")})
    drivers, unread = repository._configured_filter_commands("/repo")
    assert drivers == []
    assert unread and "--worktree" in unread

    def _raise(_argv, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    _unwrapped_sandbox(monkeypatch)
    monkeypatch.setattr(repository.subprocess, "run", _raise)
    drivers, unread = repository._configured_filter_commands("/repo")
    assert drivers == []
    assert unread


def test_a_local_driver_outranks_an_unread_worktree_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A measurement beats an unread verdict: the refusal names what was SEEN."""
    _filter_probe_calls(
        monkeypatch,
        {
            "--get-regexp": (0, "filter.evil.process\n"),
            "extensions.worktreeConfig": (128, ""),
        },
    )
    drivers, unread = repository._configured_filter_commands("/repo")
    assert drivers == ["filter.evil.process"]
    assert unread is None


def test_a_changed_checkout_drops_every_cache_derived_from_the_old_one() -> None:
    """A memo about checkout A is an answer about a repository this app left.

    The read-only mode is what makes this reachable: a foreign checkout now
    RESOLVES, so `_upstream_remote` and `fleet_state._get_owner_repo` latch against
    it, where before they declined because `_repo_read()` raised. Carried across a
    corrected `repo_path`, that remote name reaches `git rebase {remote}/{base}` and
    the prune ancestry gate against the NEW checkout.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert "if discovered != _LATCHED_RESOLVED:" in body
    assert "_drop_checkout_derived_state()" in body
    # Before the resolvers below re-read: each returns early on a latched value, so a
    # reset placed after them would leave the old answer standing.
    assert body.index("_drop_checkout_derived_state()") < body.index("_resolve_base_branch()")

    # fleet_state's caches live above repository in the component DAG, so they are
    # reached through the registry rather than by an import that would invert it.
    assert fleet_state._reset_checkout_derived_caches in repository._CHECKOUT_RESETS

    repository._UPSTREAM_REMOTE = "their-remote"
    repository._FALLBACK_REPOS = ["them/theirs"]
    repository.BASE_BRANCH = "trunk"
    fleet_state._OWNER_REPO = "them/theirs"
    fleet_state._PR_CACHE["branch"] = {"number": 1}
    fleet_state._FLEET_CACHE["data"] = {"worktrees": []}
    fleet_state._HTML_BASE = "https://github.com/them/theirs"
    fleet_state._CTX_CACHE["branch"] = {"issues": []}
    fleet_state._DISK.update({"status": "done", "total_mb": 42, "per": {"theirs": 42}})
    generation = repository._checkout_generation()

    repository._drop_checkout_derived_state()

    assert repository._UPSTREAM_REMOTE is None
    assert repository._FALLBACK_REPOS is None
    assert repository.BASE_BRANCH == "main"
    assert fleet_state._OWNER_REPO is None
    assert fleet_state._PR_CACHE == {}
    assert fleet_state._FLEET_CACHE["data"] is None
    # `_HTML_BASE` has no expiry, so nothing but this clear ends A's issue links.
    assert fleet_state._HTML_BASE is None
    assert fleet_state._CTX_CACHE == {}
    # The per-worktree map names A's worktrees, so freshness alone is not enough:
    # the numbers go back to the pre-measurement state, not merely to stale.
    assert fleet_state._DISK == {"status": "idle", "total_mb": None, "per": {}}
    assert fleet_state._DISK_COMPUTED_AT == 0.0
    # The counter is what lets a read already in flight see that it finished
    # against a different checkout than it started against.
    assert repository._checkout_generation() > generation


def test_a_fleet_build_that_lands_on_another_checkout_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing a cache does not stop a read already in flight from re-seeding it.

    The rows this build read describe checkout A. The reset that moved the counter
    emptied the cache, and this write lands after it, so storing the rows would put
    A's fleet back and serving them would answer a request about B with A's
    worktrees. The accessor resolves B by now, so building again reads the right one.
    """
    calls: list[str] = []

    async def build_then_switch() -> dict:
        calls.append("build")
        if len(calls) == 1:
            repository._drop_checkout_derived_state()
            return {"worktrees": [{"name": "theirs"}]}
        return {"worktrees": [{"name": "ours"}]}

    monkeypatch.setattr(fleet_state, "_build_fleet", build_then_switch)
    fleet_state._FLEET_CACHE["data"] = None
    fleet_state._FLEET_CACHE["ts"] = 0.0

    data = asyncio.run(fleet_state._fleet_build())

    assert len(calls) == 2
    assert data == {"worktrees": [{"name": "ours"}]}
    assert fleet_state._FLEET_CACHE["data"] == {"worktrees": [{"name": "ours"}]}


def test_a_checkout_that_keeps_moving_reports_instead_of_serving_an_empty_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry is bounded, and the bound does not fall back to a false answer.

    An empty snapshot asserts an empty fleet, which is a measurement nobody took, so
    the exhausted case raises. ``RepoUnavailable`` is the base the degrade sites
    catch, and the `/fleet` route renders it as a banner the next poll clears.
    """

    async def always_switch() -> dict:
        repository._drop_checkout_derived_state()
        return {"worktrees": [{"name": "theirs"}]}

    monkeypatch.setattr(fleet_state, "_build_fleet", always_switch)
    fleet_state._FLEET_CACHE["data"] = None
    fleet_state._FLEET_CACHE["ts"] = 0.0

    with pytest.raises(repository.RepoUnavailable):
        asyncio.run(fleet_state._fleet_build())

    assert fleet_state._FLEET_CACHE["data"] is None


def test_a_disk_aggregation_that_lands_on_another_checkout_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Withholding the freshness stamp is not enough: the numbers are A's worktrees.

    ``_disk`` serves stale-while-revalidate, so an unstamped result keeps being
    handed out under its own worktree names. The cleared state is restored instead,
    and the status must not stay "computing" or every later read short-circuits.
    """

    async def discover_then_switch() -> list[dict]:
        repository._drop_checkout_derived_state()
        return [{"path": "/elsewhere/theirs"}]

    async def measure(path: str, timeout: int = 60) -> int:
        return 42

    monkeypatch.setattr(repository, "_discover_worktrees", discover_then_switch)
    monkeypatch.setattr(fleet_state, "_measure_dir_mb", measure)

    async def scenario() -> dict:
        fleet_state._DISK.update({"status": "idle", "total_mb": None, "per": {}})
        fleet_state._DISK_COMPUTING = False
        fleet_state._DISK_COMPUTED_AT = 0.0
        await fleet_state._disk()
        for _ in range(100):
            if not fleet_state._DISK_COMPUTING:
                break
            await asyncio.sleep(0.01)
        return dict(fleet_state._DISK)

    snapshot = asyncio.run(scenario())

    assert snapshot["per"] == {}
    assert snapshot["total_mb"] is None
    assert snapshot["status"] == "idle"
    assert fleet_state._DISK_COMPUTED_AT == 0.0


def test_no_post_await_cache_write_survives_a_checkout_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every cache here is written AFTER an await, so clearing is only half the guard.

    The owner/repo name and the browser base URL have no expiry at all, and the PR
    verdict keyed under the wrong repository is what the unattended reaper reads as
    MERGED before deleting a worktree. Each write therefore asks whether the checkout
    it started against is still the resolved one, and declines when it is not.
    """
    switched: list[str] = []

    def switch() -> None:
        repository._drop_checkout_derived_state()
        switched.append("switched")

    # 1. owner/repo -- cached permanently on success, so a stale write never expires.
    async def owner_then_switch() -> str | None:
        switch()
        return "them/theirs"

    monkeypatch.setattr(fleet_state, "_repo_owner_name", owner_then_switch)
    fleet_state._OWNER_REPO = None
    fleet_state._OWNER_REPO_RETRY_AT = 0.0
    assert asyncio.run(fleet_state._get_owner_repo()) is None
    assert fleet_state._OWNER_REPO is None
    # The retry deadline is a write too: a failure against the old checkout must not
    # suppress the new one's first attempt.
    assert fleet_state._OWNER_REPO_RETRY_AT == 0.0

    # 2. the PR verdict -- `_prunable` reads MERGED as permission to delete.
    async def pr_then_switch(branch: str) -> dict | None:
        switch()
        return {"number": 7, "state": "MERGED", "_head_oid": "abc"}

    monkeypatch.setattr(fleet_state, "_fetch_pr_status", pr_then_switch)
    fleet_state._PR_CACHE.clear()
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    assert asyncio.run(fleet_state._pr_status_cached("wt-branch")) is None
    assert fleet_state._PR_CACHE == {}

    # 3. the browser base URL -- no TTL, so A's issue links would stand until restart.
    # Both of its writes are covered: the remote's own URL, and the owner/repo fallback.
    async def owner_for_html() -> str | None:
        return "them/theirs"

    async def remote_then_switch() -> str:
        switch()
        return "origin"

    async def get_url(argv, **_kwargs):
        # Stubbed rather than left to real git: on a host where the accessor is
        # unresolved this call raises and the fallback below is the only write
        # exercised, which would leave the first one unmeasured.
        assert argv[-1] == "origin", argv
        return 0, "git@github.com:them/theirs.git\n", ""

    monkeypatch.setattr(fleet_state, "_repo_owner_name", owner_for_html)
    monkeypatch.setattr(repository, "_upstream_remote", remote_then_switch)
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/theirs")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    monkeypatch.setattr(runtime, "_run_cmd", get_url)
    fleet_state._HTML_BASE = None
    fleet_state._OWNER_REPO = None
    fleet_state._OWNER_REPO_RETRY_AT = 0.0
    assert asyncio.run(fleet_state._html_repo_base()) is None
    assert fleet_state._HTML_BASE is None

    # 4. the per-branch context -- issues and tickets resolved against A's repository.
    async def context_then_switch(branch: str, path: str, pr: dict | None) -> dict:
        switch()
        return {"issues": [{"number": 1}], "tickets": [], "summary": "theirs"}

    monkeypatch.setattr(fleet_state, "_build_context", context_then_switch)
    fleet_state._CTX_CACHE.clear()
    got = asyncio.run(fleet_state._context_cached("wt-branch", "/wt", None))
    assert got == {"issues": [], "tickets": [], "summary": None}
    assert fleet_state._CTX_CACHE == {}

    assert len(switched) == 4


def test_the_poll_path_reopens_a_read_only_latch() -> None:
    """The reopen-on-changed-config path runs through the fleet poll, not only /fleet.

    ``_ensure_repo_resolved`` returns before ``ensure_main_repo_discovered`` for a
    resolved checkout, so a short-circuit that reads only the invalid verdict keeps
    serving a stranger's repository — base branch and upstream remote resolved
    against it — until the gateway restarts.
    """
    source = inspect.getsource(worktree_ops._ensure_repo_resolved)
    assert "not repository._REPO_INVALID_MSG" in source
    assert "not repository._REPO_READ_ONLY_MSG" in source


# --- a read-only checkout reports its live state as unknown -------------------


def test_read_only_checkout_reports_live_state_unknown() -> None:
    """`null` badges are falsy in the page, so the refusal needs the unknown flag.

    Make live is refused on a read-only checkout, and the page already disables the
    control and states the reason when the live state is unknown. Without this the
    row renders as "nothing is live" and still offers the button that answers 409.
    """
    body = inspect.getsource(fleet_state._build_fleet)
    assert "if repository._read_only_reason():" in body
    gate = body.index("if repository._read_only_reason():")
    assert body.index("live_state_known = False", gate) > gate


# --- the read-only denial reaches the audit trail ----------------------------


def test_read_only_denial_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal on an AUTHENTICATED request is a permission decision.

    Recorded on BOTH surfaces that take it: the backend middleware, whose HMAC
    denials beside it are already audited, and the gateway's own resolution step,
    which returns before the route reaches its ``_audit`` call.
    """
    for source in (
        inspect.getsource(http_api.hmac_proxy_middleware),
        inspect.getsource(gateway_routes._ensure_repo),
    ):
        assert "log_tool_invocation" in source
        assert 'outcome="denied"' in source
        assert 'tool_name="dev-fleet:repo-read-only"' in source
        # The 409 must survive a failing audit sink: auditing may not mask the answer.
        assert "except Exception" in source
