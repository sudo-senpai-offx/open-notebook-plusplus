"""Residency: which models are loaded, and what that does to admission.

What is being tested and why each test exists
---------------------------------------------
The phone measured 2517 MB of swap in use with both model servers nominally
"up", and llama-server's resident set read 3288 kB ten minutes after launch
having been 1222380 kB. A server that answers /health is not a model that is
resident - it can be paged out to zram entirely - so "up" was never the
question that mattered, and nothing in the codebase could answer the question
that did.

These tests pin the three decisions that follow from that:

1. A load is *refused*, not attempted, when it will not fit. ADR-014 says a
   job that will not fit is held rather than shrunk; this is the same rule one
   level up, in that a model that will not fit is not loaded at all.
2. A failure to load is a **hold**, never a promotion against a model that is
   not there.
3. Ordering by class priority only happens when a supervisor actually supplied
   policy. With residency off the queue must be byte-for-byte the old
   oldest-first order, because a device with no supervisor must not have its
   queue reordered by a feature it cannot see.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from open_notebook.residency import client as residency
from open_notebook.scheduler.dispatcher import (
    _order_held,
    _role_counts,
    _roles_of,
    dispatch_once,
)
from tests.test_scheduler_dispatcher import (
    GENERATION_BYTES,
    FakeDB,
    _job,
    _memory,
    _patch_repo,
)

MIB = 1024 * 1024
GIB = 1024 * MIB


def _load_supervisor():
    """Import the Termux supervisor from scripts/.

    It lives outside the package because it runs in Termux, not in the guest -
    there is no importable module here. Registered in `sys.modules` before exec
    because `@dataclass` resolves its own module through that mapping.
    """
    path = Path(__file__).resolve().parents[1] / "scripts" / "termux-supervisor.py"
    spec = importlib.util.spec_from_file_location("termux_supervisor", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["termux_supervisor"] = module
    spec.loader.exec_module(module)
    return module


sup = _load_supervisor()


class TestProbeContract:
    def test_parses_the_probe_answer(self):
        parsed = sup.parse_kv("role=chat resident=1 pid=19057 model=qwen2.5.gguf")
        assert parsed == {
            "role": "chat",
            "resident": "1",
            "pid": "19057",
            "model": "qwen2.5.gguf",
        }

    def test_ignores_a_logged_line_before_the_answer(self):
        """A launcher that warns before answering must not have the warning
        parsed as residency state. The launchers print progress lines."""
        parsed = sup.parse_kv(
            "[onb] llama-server starting (2 threads, 4096 ctx)\n"
            "role=chat resident=0 pid=- model=-\n"
        )
        assert parsed["resident"] == "0"
        assert "ctx" not in parsed

    def test_absent_output_is_not_resident(self):
        """Silence must read as "not loaded".

        Carrying the last known answer forward would let the supervisor claim a
        model is resident after the process died - the exact state this whole
        mechanism exists to detect.
        """
        assert sup.parse_kv("") == {}
        assert sup.parse_kv("everything is fine\n") == {}


class TestLoadCeiling:
    def test_refuses_an_unknown_model_size(self, monkeypatch):
        monkeypatch.setattr(sup, "memory", lambda: {"MemAvailable": 8 * GIB})
        fits, why = sup.Manager().fits(sup.ROLES["chat"], 0)
        assert not fits
        assert "unknown" in why

    def test_refuses_when_it_would_break_the_reserve(self, monkeypatch):
        """1.4 GB free, a 1.2 GB chat model, 512 MB of reserve to keep.

        1400 MB available minus 1100 MB resident leaves 300 MB, under the
        reserve - so this load is refused. Loading it and hoping is how the
        Android low-memory killer decides which process dies, and it never
        chooses the one that just allocated.
        """
        monkeypatch.setattr(sup, "memory", lambda: {"MemAvailable": 1400 * MIB})
        monkeypatch.setattr(sup, "reserve_bytes", lambda: 512 * MIB)
        fits, why = sup.Manager().fits(sup.ROLES["chat"], 1000 * MIB)
        assert not fits
        assert "held, not shrunk" in why

    def test_the_measured_phone_case_still_loads_on_demand(self, monkeypatch):
        """A deliberate number, pinned so it cannot drift by accident.

        Measured on the device with the stack nominally healthy: MemAvailable
        2073 MB, 2517 MB of swap already in use. A 1 GB chat model needs ~1100 MB
        resident, which clears a 512 MB reserve at that figure - so chat *does*
        load when asked, and stops being resident when not.

        That is the whole design. The swap was caused by a 1.2 GB model resident
        permanently, not by chat being loadable; refusing chat outright at 2 GB
        free would trade a background problem for an interactive one.
        """
        monkeypatch.setattr(sup, "memory", lambda: {"MemAvailable": 2073 * MIB})
        monkeypatch.setattr(sup, "reserve_bytes", lambda: 512 * MIB)
        fits, why = sup.Manager().fits(sup.ROLES["chat"], 1000 * MIB)
        assert fits, why

    def test_allows_a_load_that_clears_the_reserve(self, monkeypatch):
        monkeypatch.setattr(sup, "memory", lambda: {"MemAvailable": 8 * GIB})
        monkeypatch.setattr(sup, "reserve_bytes", lambda: 512 * MIB)
        fits, why = sup.Manager().fits(sup.ROLES["chat"], 1_000 * MIB)
        assert fits, why

    def test_refuses_when_memory_cannot_be_read(self, monkeypatch):
        """An unreadable MemAvailable is not an unlimited one."""
        monkeypatch.setattr(sup, "memory", lambda: {"MemAvailable": 0})
        fits, why = sup.Manager().fits(sup.ROLES["chat"], 1_000 * MIB)
        assert not fits
        assert "unreadable" in why


class TestRoleMapping:
    def test_generation_needs_chat_and_embedding_needs_embed(self):
        assert residency.role_for_model("generation") == "chat"
        assert residency.role_for_model("embedding") == "embed"

    def test_sentinels_need_no_role(self):
        """`__no_model__` is plumbing.

        `__unknown__` is an unresolved job whose class is not knowable, and
        mapping it to a role would make an uncertain job look schedulable -
        which is how a job ends up promoted against a model nobody loaded.
        """
        assert residency.role_for_model("__no_model__") is None
        assert residency.role_for_model("__unknown__") is None

    def test_a_jobs_roles_come_from_its_footprint(self):
        assert _roles_of({"name": "run_transformation"}, GENERATION_BYTES) == ["chat"]
        assert _roles_of({"name": "embed_source"}, GENERATION_BYTES) == ["embed"]
        assert _roles_of({"name": "process_source"}, GENERATION_BYTES) == []

    def test_counts_per_class(self):
        rows = [
            {"name": "run_transformation"},
            {"name": "run_transformation"},
            {"name": "embed_source"},
            {"name": "process_source"},
        ]
        assert _role_counts(rows, GENERATION_BYTES) == {"chat": 2, "embed": 1}


def _policy(chat_priority: int = 10, embed_priority: int = 50) -> Dict[str, Any]:
    return {
        "chat": residency.RolePolicy("chat", chat_priority, 1),
        "embed": residency.RolePolicy("embed", embed_priority, 2),
    }


class TestOrdering:
    def test_unchanged_when_no_policy(self):
        """Strict oldest-first is the default and must survive untouched.

        A deployment with no supervisor must not have its queue reordered by a
        feature it cannot see.
        """
        held = [_job("embed_source", 1), _job("run_transformation", 2)]
        assert [row["id"] for row in _order_held(held, None, GENERATION_BYTES)] == [
            "command:embed_source1",
            "command:run_transformation2",
        ]

    def test_chat_overtakes_an_older_embedding_job(self):
        held = [_job("embed_source", 1), _job("run_transformation", 2)]
        ordered = _order_held(held, _policy(), GENERATION_BYTES)
        assert [row["name"] for row in ordered] == [
            "run_transformation",
            "embed_source",
        ]

    def test_cheap_plumbing_goes_first(self):
        """No-model work is 64 MB and finishes fastest, so it unblocks the most."""
        held = [_job("run_transformation", 1), _job("process_source", 2)]
        ordered = _order_held(held, _policy(), GENERATION_BYTES)
        assert [row["name"] for row in ordered] == [
            "process_source",
            "run_transformation",
        ]

    def test_no_model_work_leads_even_with_no_policy(self):
        """The regression this tier's ungating fixes, stated as a rule.

        Gating the no-model tier on the supervisor meant a PC - residency off,
        which is the default everywhere - fell back to strict creation order. A
        `create_insight` submitted by a transformation that had just finished
        then queued behind every older transformation, so generated text sat
        finished and unwritable. Measured: positions 40, 41 and 42 of 42.

        The tier reads the command name off the row. It never needed a
        supervisor, and gating it bought nothing.
        """
        held = [_job("run_transformation", i) for i in range(1, 6)]
        held.append(_job("create_insight", 7))
        ordered = _order_held(held, None, GENERATION_BYTES)
        assert ordered[0]["name"] == "create_insight"
        assert [row["name"] for row in ordered[1:]] == ["run_transformation"] * 5

    def test_no_policy_still_orders_model_work_oldest_first(self):
        """Ungating tier 0 must not disturb tier 1.

        With no policy there are no class priorities to publish, so every model
        job shares one and oldest-first decides - the behaviour the
        `unchanged_when_no_policy` test has always pinned, now stated on its own
        so a future change to tier 0 cannot quietly reorder this.
        """
        held = [
            _job("run_transformation", 3),
            _job("run_transformation", 1),
            _job("run_transformation", 2),
        ]
        ordered = _order_held(held, None, GENERATION_BYTES)
        assert [row["id"] for row in ordered] == [
            "command:run_transformation1",
            "command:run_transformation2",
            "command:run_transformation3",
        ]

    def test_a_chain_drains_rather_than_serialising_behind_itself(self):
        """The whole point: transformation -> create_insight, in that order.

        Modelling the queue as it actually stood - N generations queued, then
        the insight writes their predecessors produced - the writes must be
        admitted first. Otherwise every job in a regeneration waits for the
        entire backlog ahead of it, and the pipeline advances one link per
        full drain rather than one link per free slot.
        """
        held = [_job("run_transformation", i) for i in range(1, 10)]
        held += [_job("create_insight", i) for i in range(10, 13)]
        ordered = _order_held(held, None, GENERATION_BYTES)
        names = [row["name"] for row in ordered]
        assert names[:3] == ["create_insight"] * 3
        assert set(names[3:]) == {"run_transformation"}


class TestOrderingIsActuallyApplied:
    """The call site, not the function.

    The first attempt at ungating the no-model tier changed `_order_held` and
    left the caller behind `if policy:`. Every unit test passed, because they
    call the function directly — and production never called it, so a 45-job
    regeneration behaved exactly as before. These tests drive `dispatch_once`,
    which is the only place the ordering can actually take effect.
    """

    @staticmethod
    def _repo(monkeypatch, held):
        """A repository whose held queue is `held` and whose live queue is empty."""
        from open_notebook.database import repository

        promoted: List[str] = []

        async def repo_query(sql, params=None):
            text = str(sql)
            if "status = 'held'" in text and "ORDER BY" in text:
                return list(held)
            if "status IN" in text and "SELECT name" in text:
                return []
            if "UPDATE" in text:
                if (params or {}).get("status"):
                    promoted.append(str((params or {}).get("id")))
                return []
            return []

        monkeypatch.setattr(repository, "repo_query", repo_query)
        monkeypatch.setattr(repository, "ensure_record_id", lambda v: v)
        return promoted

    @pytest.mark.asyncio
    async def test_a_pass_with_residency_off_still_orders(self, monkeypatch):
        """The regression, end to end.

        Residency off is the PC's default and the phone's before the guest
        enables it, so this is the configuration in which the ordering was
        silently absent.
        """
        from open_notebook.scheduler import dispatcher as disp

        held = [_job("run_transformation", i) for i in range(1, 6)]
        held += [_job("create_insight", i) for i in range(6, 9)]
        promoted = self._repo(monkeypatch, held)

        async def no_policy():
            return None

        monkeypatch.setattr(disp.residency, "policies", no_policy)
        monkeypatch.setattr(disp.residency, "enabled", lambda: False)

        outcome = await disp.dispatch_once(
            generation_bytes=GENERATION_BYTES,
            memory=_memory(total=15 * GIB, available=8 * GIB),
            window=3,
        )

        assert outcome.promoted_count == 3, "the window should have been filled"
        promoted_names = [str(row["id"]) for row in held if str(row["id"]) in promoted]
        assert len(promoted_names) == 3
        chosen = [row["name"] for row in held if str(row["id"]) in promoted]
        assert chosen == ["create_insight"] * 3, (
            f"with residency off, the no-model tier must still lead; promoted {chosen}"
        )

    def test_oldest_first_within_a_class(self):
        held = [
            _job("run_transformation", 3),
            _job("run_transformation", 1),
            _job("run_transformation", 2),
        ]
        ordered = _order_held(held, _policy(), GENERATION_BYTES)
        assert [row["id"] for row in ordered] == [
            "command:run_transformation1",
            "command:run_transformation2",
            "command:run_transformation3",
        ]


def _enable_residency(monkeypatch, *, ensure=None, policies=None):
    monkeypatch.setenv("OPEN_NOTEBOOK_RESIDENCY_ENABLED", "true")
    monkeypatch.setattr(residency, "enabled", lambda: True)

    async def fake_ensure(role: str):
        if ensure is None:
            return residency.ResidencyResult(ok=True, role=role)
        return ensure(role)

    async def fake_policies():
        return policies

    monkeypatch.setattr(residency, "ensure_resident", fake_ensure)
    monkeypatch.setattr(residency, "policies", fake_policies)


class TestAdmissionUnderResidency:
    @pytest.mark.asyncio
    async def test_a_refused_load_holds_the_job(self, monkeypatch):
        """The load-bearing case: no model, no promotion.

        Promoting here produces a job that runs against an absent server and
        reports success, which is the worst outcome available - worse than
        holding, because it is invisible.
        """
        db = FakeDB(held=[_job("run_transformation", 1)])
        _patch_repo(monkeypatch, db)
        _enable_residency(
            monkeypatch,
            ensure=lambda role: residency.ResidencyResult(
                ok=False, role=role, error="needs 1258 MB, 900 MB available"
            ),
        )

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert outcome.promoted == []
        assert db.promoted == []
        assert any("residency:chat" in reason for reason in outcome.held_reasons)
        assert db.holds and "residency:chat" in db.holds[0][1]

    @pytest.mark.asyncio
    async def test_an_unreachable_supervisor_holds_rather_than_guesses(
        self, monkeypatch
    ):
        db = FakeDB(held=[_job("embed_source", 1)])
        _patch_repo(monkeypatch, db)
        _enable_residency(
            monkeypatch,
            ensure=lambda role: residency.ResidencyResult(
                ok=False, role=role, error="supervisor unreachable: ConnectError"
            ),
        )

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert outcome.promoted == []
        assert any("supervisor unreachable" in r for r in outcome.held_reasons)

    @pytest.mark.asyncio
    async def test_a_successful_load_promotes(self, monkeypatch):
        db = FakeDB(held=[_job("run_transformation", 1)])
        _patch_repo(monkeypatch, db)
        _enable_residency(
            monkeypatch,
            ensure=lambda role: residency.ResidencyResult(
                ok=True, role=role, loaded=True, pid="19057"
            ),
        )

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert outcome.promoted == ["command:run_transformation1"]
        assert outcome.residency["chat"]["ok"] is True
        assert outcome.residency["chat"]["pid"] == "19057"

    @pytest.mark.asyncio
    async def test_a_class_at_its_limit_is_held(self, monkeypatch):
        """One chat job in flight, chat's limit is 1, a second arrives."""
        db = FakeDB(
            held=[_job("run_transformation", 2)],
            live=[{"name": "run_transformation", "args": {}}],
        )
        _patch_repo(monkeypatch, db)
        _enable_residency(monkeypatch, policies=_policy())

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert outcome.promoted == []
        assert "class-limit:chat" in outcome.held_reasons

    @pytest.mark.asyncio
    async def test_a_saturated_class_does_not_hold_the_whole_queue(self, monkeypatch):
        """The invariant `continue` used to rely on, and no longer may.

        A chat job at its limit must not stop an embedding job from starting.
        Under a single total order "nothing behind it has a better claim" was
        sound; with class priority it is not, and following it would deadlock a
        busy chat class against a busy embed queue.
        """
        db = FakeDB(
            held=[_job("run_transformation", 2), _job("embed_source", 3)],
            live=[{"name": "run_transformation", "args": {}}],
        )
        _patch_repo(monkeypatch, db)
        _enable_residency(monkeypatch, policies=_policy())

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert outcome.promoted == ["command:embed_source3"]
        assert "class-limit:chat" in outcome.held_reasons

    @pytest.mark.asyncio
    async def test_the_limit_applies_within_a_single_pass(self, monkeypatch):
        """Counts must move as the pass promotes.

        Reading the in-flight set once per pass is how three individually valid
        promotions overshoot the device - the same mistake the memory gate was
        fixed for, repeated one level down.
        """
        db = FakeDB(
            held=[_job("run_transformation", 1), _job("run_transformation", 2)],
        )
        _patch_repo(monkeypatch, db)
        _enable_residency(monkeypatch, policies=_policy())

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert len(outcome.promoted) == 1
        assert "class-limit:chat" in outcome.held_reasons

    @pytest.mark.asyncio
    async def test_embedding_gets_its_own_second_slot(self, monkeypatch):
        db = FakeDB(
            held=[_job("embed_source", 2)],
            live=[{"name": "embed_source", "args": {}}],
        )
        _patch_repo(monkeypatch, db)
        _enable_residency(monkeypatch, policies=_policy())

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert outcome.promoted == ["command:embed_source2"]

    @pytest.mark.asyncio
    async def test_off_means_exactly_the_old_behaviour(self, monkeypatch):
        """Residency disabled: no policy fetch, no load, oldest-first, and
        the class-limit path never taken even with a chat job already running."""
        monkeypatch.delenv("OPEN_NOTEBOOK_RESIDENCY_ENABLED", raising=False)
        monkeypatch.setattr(residency, "enabled", lambda: False)
        called: List[str] = []

        async def never(role: str):
            called.append(role)
            return residency.ResidencyResult(ok=True, role=role)

        monkeypatch.setattr(residency, "ensure_resident", never)
        db = FakeDB(
            held=[_job("run_transformation", 2)],
            live=[{"name": "run_transformation", "args": {}}],
        )
        _patch_repo(monkeypatch, db)

        outcome = await dispatch_once(
            generation_bytes=GENERATION_BYTES, memory=_memory()
        )

        assert called == []
        assert outcome.residency == {}
        assert outcome.role_counts == {}
        assert outcome.promoted == ["command:run_transformation2"]


class TestReaperOnlyReapsWhatItLoaded:
    """The supervisor unloaded the stack's own boot-time models.

    Observed on the device: `~/bin/onb` starts chat and embed deliberately,
    then `[residency] idle-unloaded chat, embed` appeared in the log five
    minutes later and both were gone. Nothing else in any log said what
    happened.

    The cause is not a bad threshold. It is that no guest ever called
    `ensure_resident` - residency was never enabled - so `last_used` never
    advanced, and the reaper saw two models it had never touched as merely
    idle. Two owners of one lifecycle.

    The fix is narrow and is the only safe one: the supervisor reaps only what
    it started. The launcher remains the authority on what exists at boot.
    """

    def test_a_launcher_started_role_is_not_owned(self):
        state = sup.Residency(role="chat", serving=True, resident=True, owned=False)
        assert state.owned is False

    def test_nothing_is_owned_until_something_loads_it(self):
        """Ownership is not implied by the role existing or by `serving`."""
        assert sup.Residency(role="chat", serving=True).owned is False
        assert sup.Residency(role="chat", resident=True).owned is False

    def test_ownership_is_published(self):
        """Otherwise a model vanishing has nothing to point at."""
        catalogue = sup.Manager().catalogue()
        assert "owned" in catalogue["roles"]["chat"]

    def test_the_reaper_filters_on_ownership(self):
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "termux-supervisor.py"
        ).read_text(encoding="utf-8")
        start = source.index("def reap_idle")
        body = source[start : source.index("\n    def _reap_loop", start)]
        assert "state.owned" in body, (
            "the reaper must skip roles it did not load; otherwise it tears "
            "down whatever the launcher started"
        )
        # `serving` alone is what the old filter used, and it is not enough.
        assert "not state.owned or not state.serving" in body

    def test_ensure_claims_ownership_only_after_a_successful_load(self):
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "termux-supervisor.py"
        ).read_text(encoding="utf-8")
        start = source.index("def ensure")
        body = source[start : source.index("\n    def release", start)]
        assert "state.owned = True" in body
        after_probe = body.index(
            "state = self.probe(role)", body.index("run(role.load")
        )
        assert body.index("state.owned = True") > after_probe, (
            "claiming ownership before the load is verified means a failed load "
            "leaves a role the reaper will chase"
        )

    def test_release_gives_up_ownership_even_when_the_stop_failed(self):
        """Otherwise a process it no longer controls is retried forever."""
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "termux-supervisor.py"
        ).read_text(encoding="utf-8")
        start = source.index("def release(self, name")
        body = source[start : source.index("\n    def release_all", start)]
        assert "self._state[name].owned = False" in body
        # Placed on the unload path, after the stop has been attempted, so a
        # failed unload cannot leave the role permanently reapable. The early
        # return for an already-stopped role also contains "unloaded", so the
        # anchor is the `run` call rather than the first occurrence.
        unload = body.index("run(role.unload")
        assert body.index("self._state[name].owned = False") > unload

    def test_serving_alone_does_not_make_a_role_reapable(self):
        """The exact pre-fix condition: up, healthy, and still not ours."""
        state = sup.Residency(role="chat", serving=True, resident=True, owned=False)
        assert state.serving and state.resident
        assert state.owned is False


class TestLauncherCanActuallyBeStopped:
    """SIGTERM does not reach the launcher.

    `onb stop` runs in its own shell and signals the launcher holding the stack
    up, but that launcher spends its life blocked on a foreground
    `proot-distro login`, and a POSIX shell defers traps until the foreground
    command returns. The guest does not return while the guest is running, so
    the `trap ... exit 0` never fires.

    Measured on the device: `kill 19015` left the process alive, state `S`, with
    `SigCgt` bit 14 set - a handler installed that could not be reached. So
    SIGKILL is the only thing that works, and `stop_all` has to do it.
    """

    @staticmethod
    def _launcher() -> str:
        return (
            Path(__file__).resolve().parents[1] / "scripts" / "termux-launch.sh"
        ).read_text(encoding="utf-8")

    def test_stop_all_kills_stragglers(self):
        code = self._launcher()
        start = code.index("stop_all() {")
        body = code[start : code.index("\n}\n", start)]
        assert "kill_stragglers" in body, (
            "the launcher's own SIGTERM trap is deferred behind a foreground "
            "proot-distro login, so stopping it needs more than a signal"
        )

    def test_escalates_to_sigkill(self):
        code = self._launcher()
        start = code.index("kill_stragglers() {")
        body = code[start : code.index("\n}\n", start)]
        assert "kill -9" in body, (
            "SIGTERM demonstrably does not work here; without escalation the "
            "stop reports success and leaves the launcher holding the stack"
        )

    def test_does_not_kill_itself(self):
        """`onb stop` runs inside a launcher; that one must survive the sweep."""
        code = self._launcher()
        # The exclusion lives in the caller, not in the scan: `launcher_pids`
        # reports every match honestly and each caller filters out `$$`.
        sweep = code[
            code.index("kill_stragglers() {") : code.index(
                "\n}\n", code.index("kill_stragglers() {")
            )
        ]
        assert 'grep -v "^$$\\$"' in sweep, (
            "the stop path matches every ~/bin/onb, which includes the shell "
            "doing the stopping"
        )
        assert sweep.count('grep -v "^$$\\$"') >= 3, (
            "every pid list has to drop $$ - the initial sweep, the SIGTERM wait "
            "and the post-SIGKILL confirmation - or the stop kills itself"
        )

    def test_says_so_when_one_survives(self):
        """A launcher that outlives SIGKILL has to be named, not hidden."""
        code = self._launcher()
        start = code.index("kill_stragglers() {")
        body = code[start : code.index("\n}\n", start)]
        assert "survived SIGKILL" in body and "kill -9" in body


class TestGuestTeardown:
    """`onb stop` never stopped the guest, because proot-distro cannot.

    `stop_all` guarded its teardown with

        if proot-distro ps 2>/dev/null | grep -q ubuntu; then
            proot-distro kill ubuntu
        fi

    proot-distro 5.3.0 has neither command. Its full list is install, list,
    login, run, remove, rename, reset, backup, restore, clear-cache, copy,
    sync, build, push. `proot-distro ps` printed `unknown command 'ps'` to
    stderr - discarded by `2>/dev/null` - leaving stdout empty, so grep failed,
    so the kill was never attempted once.

    The guard could not report failure because it never ran, which is worse
    than having no guard: it read as protection that was in place.
    """

    @staticmethod
    def _launcher() -> str:
        return (
            Path(__file__).resolve().parents[1] / "scripts" / "termux-launch.sh"
        ).read_text(encoding="utf-8")

    @classmethod
    def _code(cls) -> str:
        """`stop_all`'s body with comments stripped.

        The old call is named in the comment explaining why it was replaced, so
        an assertion over raw text fails against its own explanation - which is
        what happened the first time this was written.
        """
        text = cls._launcher()
        start = text.index("stop_all() {")
        # The body's last statement is the straggler sweep, indented two spaces
        # and closing on the next line.
        body = text[start : text.index("\n  kill_stragglers\n}", start)]
        return "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )

    def test_does_not_call_commands_that_do_not_exist(self):
        code = self._code()
        assert "proot-distro kill" not in code, (
            "proot-distro 5.3.0 has no `kill` command"
        )
        assert "proot-distro ps" not in code, (
            "proot-distro 5.3.0 has no `ps` command either, so the guard that "
            "wrapped the kill could never succeed"
        )

    def test_asks_the_guest_script_to_tear_itself_down(self):
        """One list of service names, owned by the script that starts them."""
        code = self._code()
        assert "termux-stack.sh down" in code, (
            "the guest services are killed by name and termux-stack.sh owns "
            "those names; copying the list here is a second thing to drift"
        )
        assert "proot-distro login ubuntu" in code

    def test_kills_orphaned_proot_sessions(self):
        """A SIGKILLed launcher orphans its proot and the guest keeps running.

        Observed: two proot processes for one container after a manual
        recovery, with the new session reporting `already running` four times
        against the old one's services. There is no `proot-distro ps` to ask,
        so the command line is the only available answer.
        """
        code = self._launcher()
        assert "kill_proot_sessions" in code
        start = code.index("proot_pids() {")
        body = code[start : code.index("\n}\n", start)]
        assert "/containers/ubuntu/rootfs" in body, (
            "the rootfs path is what identifies a container's proot processes"
        )
        assert "ROOTFS_UBUNTU" in code

    def test_proot_sweep_escalates_and_reports(self):
        code = self._launcher()
        start = code.index("kill_proot_sessions() {")
        body = code[start : code.index("\n}\n", start)]
        assert "kill -9" in body and "survived SIGKILL" in body

    def test_rootfs_is_derived_not_hardcoded(self):
        """A non-default Termux prefix must still be found."""
        code = self._launcher()
        assert 'PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"' in code
        assert 'ROOTFS_UBUNTU="$PREFIX/var/lib/proot-distro' in code


class TestPagedOutIsNotAFailure:
    """A model in zram is slow, not broken.

    Measured on the device: a chat server holding 1748 kB of a 1066 MB model
    answered /health and /v1/models normally, and the first generation took
    6.9s and took RSS to 972016 kB. It worked. The kernel faults the pages
    back in; that is what `ensure` must be allowed to rely on.

    If admission keyed off `resident`, this state would hold every job - and
    the job would be indistinguishable from a genuine load failure, so the
    operator would be told to reboot a phone that was working.
    """

    def test_a_serving_but_swapped_out_role_is_ok(self):
        state = sup.Residency(role="chat", serving=True, resident=False, pid="19057")
        assert state.serving is True
        assert state.resident is False

    def test_resident_bytes_is_zero_when_not_resident(self):
        """The estimate is of RAM held, so a paged-out model costs none."""
        warm = sup.Residency(role="chat", serving=True, resident=True, disk_bytes=1000)
        cold = sup.Residency(role="chat", serving=True, resident=False, disk_bytes=1000)
        assert warm.resident_bytes > 0
        assert cold.resident_bytes == 0

    def test_client_carries_resident_separately_from_ok(self):
        result = residency.ResidencyResult(ok=True, role="chat", resident=False)
        assert result.ok is True
        assert result.resident is False
        assert result.hold_reason() == ""

    def test_a_warm_role_reports_resident(self):
        result = residency.ResidencyResult(ok=True, role="chat", resident=True)
        assert result.ok is True and result.resident is True


class TestClient:
    def test_disabled_by_default(self, monkeypatch):
        """A fresh checkout must behave exactly as it did before.

        Defaulting on would make "no supervisor running" a silent behaviour
        change - which is the failure mode this flag exists to prevent.
        """
        monkeypatch.delenv("OPEN_NOTEBOOK_RESIDENCY_ENABLED", raising=False)
        assert residency.enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_reads_the_usual_truthy_spellings(self, monkeypatch, raw):
        monkeypatch.setenv("OPEN_NOTEBOOK_RESIDENCY_ENABLED", raw)
        assert residency.enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe"])
    def test_anything_else_is_off(self, monkeypatch, raw):
        monkeypatch.setenv("OPEN_NOTEBOOK_RESIDENCY_ENABLED", raw)
        assert residency.enabled() is False

    def test_hold_reason_is_tagged_and_bounded(self):
        """Shown verbatim in the status panel beside every other reason, so it
        has to be short and carry the role it failed on."""
        result = residency.ResidencyResult(
            ok=False, role="chat", error="x" * 500 + "\nsecond line"
        )
        reason = result.hold_reason()
        assert reason.startswith("residency:chat:")
        assert "\n" not in reason
        assert len(reason) <= 160

    def test_a_success_has_no_hold_reason(self):
        assert residency.ResidencyResult(ok=True, role="chat").hold_reason() == ""

    @pytest.mark.asyncio
    async def test_policies_are_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(residency, "enabled", lambda: False)
        assert await residency.policies() is None

    @pytest.mark.asyncio
    async def test_an_unreachable_catalogue_yields_no_policy(self, monkeypatch):
        """No policy means "do not enforce per-class limits".

        Enforcing from a stale copy would hold work for limits that no longer
        exist, with nothing in the outcome to explain it.
        """
        monkeypatch.setattr(residency, "enabled", lambda: True)
        monkeypatch.setattr(residency, "base_url", lambda: "http://127.0.0.1:1")

        assert await residency.policies() is None


class TestSupervisorPolicyOwnership:
    def test_chat_outranks_embedding(self):
        """The user's rule: chat is interactive, embedding is bulk."""
        assert sup.ROLES["chat"].priority < sup.ROLES["embed"].priority

    def test_each_role_has_its_own_port(self):
        ports = [role.port for role in sup.ROLES.values()]
        assert len(set(ports)) == len(ports)

    def test_probe_load_and_unload_are_all_real_verbs(self):
        """The supervisor shells out; it implements nothing.

        A second implementation of "start the chat server" in the supervisor
        would be free to disagree with the launcher about which pid is the chat
        server, and :8080 and :8081 are the same executable - that disagreement
        has already had `start_llama` kill the embedder it meant to replace.
        """
        for role in sup.ROLES.values():
            for verb in (role.probe, role.load, role.unload, role.model_path):
                assert verb[0] == "sh"
                assert verb[-1] in (
                    "chat-status",
                    "chat-up",
                    "chat-down",
                    "model-path",
                    "probe",
                    "up",
                    "down",
                )


class TestShellContract:
    """The probe verbs are a contract between two files in two runtimes.

    Asserted here because a rename on either side would otherwise only show up
    as a supervisor that reports every role as unloaded - which looks exactly
    like a phone that is simply out of memory.
    """

    @staticmethod
    def _script(name: str) -> str:
        return (Path(__file__).resolve().parents[1] / "scripts" / name).read_text(
            encoding="utf-8"
        )

    def _code(self, text: str) -> str:
        """Strip comments, so assertions cannot match prose describing old code.

        Three times now a test passed against a comment quoting the previous
        implementation while the code underneath said something else.
        """
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if (
                stripped.startswith("#")
                or stripped.startswith('echo "[')
                or (stripped.startswith('echo "'))
            ):
                continue
            lines.append(line)
        return "\n".join(lines)

    def test_launcher_exposes_the_residency_verbs(self):
        code = self._code(self._script("termux-launch.sh"))
        for verb in ("chat-up)", "chat-down)", "chat-status)", "model-path)"):
            assert verb in code, f"termux-launch.sh lost {verb}"

    def test_embedder_exposes_probe_and_model_path(self):
        code = self._code(self._script("termux-embed.sh"))
        assert "  probe)" in code
        assert "  model-path)" in code

    def test_probe_reports_resident_only_when_the_weights_are_in_ram(self):
        """The bug this replaces, pinned.

        `resident=1` used to require a pid and a /health answer. On the device
        that reported a model holding 1748 kB of a 1066 MB model as loaded,
        because a swapped-out llama-server answers /health perfectly - those
        endpoints touch no weights. So the flag was false exactly when it
        mattered, and the panel, the dispatcher's load decisions and the idle
        reaper were all reading it.

        The fix reads VmRSS, which is the only local measurement of the thing
        the flag is named after.
        """
        code = self._code(self._script("termux-launch.sh"))
        start = code.index("probe_chat()")
        body = code[start : code.index("\n}\n", start)]
        assert "vm_rss_kb" in body, (
            "residency has to be measured from /proc/<pid>/status, not inferred "
            "from an endpoint a swapped-out server still answers"
        )
        assert "VmRSS" in self._script("termux-launch.sh")
        assert "-ge 500000" in body, (
            "a pid plus a health check is not residency; the threshold is what "
            "separates 1748 kB from 972016 kB"
        )

    def test_probe_separates_serving_from_resident(self):
        """Three states, not two.

        A process that is not there, a process answering with its weights in
        zram, and a warm process are different situations needing different
        responses. Reporting the middle one as either extreme is what made the
        original flag useless.
        """
        for script, fn in (
            ("termux-launch.sh", "probe_chat"),
            ("termux-embed.sh", "probe_embed"),
        ):
            code = self._code(self._script(script))
            start = code.index(fn)
            body = code[
                start : code.index("\n  }\n", start)
                if script.endswith("embed.sh")
                else code.index("\n}\n", start)
            ]
            assert "serving=1" in body, f"{fn} cannot report a paged-out server"
            assert "serving=0" in body, f"{fn} cannot report a stopped server"
            assert "rss_kb=" in body, f"{fn} does not publish the measurement"

    def test_embedder_threshold_scales_with_its_model(self):
        """bge-small is 35 MB; the chat floor of 500 MB would call it
        permanently unloaded."""
        code = self._code(self._script("termux-embed.sh"))
        start = code.index("probe_embed")
        body = code[start : code.index("\n  }\n", start)]
        assert "-ge 8192" in body
        assert "500000" not in body

    def test_the_supervisor_binds_loopback_only(self):
        """The guest shares Termux's loopback, so that is the whole of the
        access control. Binding 0.0.0.0 would expose model start/stop to the
        LAN."""
        assert sup.HOST == "127.0.0.1"

    def test_loads_are_serialised(self):
        """One process-wide lock, not one per role.

        Two roles loading at once is the same race as two launchers racing,
        and llama-server binds with SO_REUSEPORT - so the failure mode is
        several processes holding the port with none holding the model. That
        was observed on the device as five processes claiming :8081.
        """
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "termux-supervisor.py"
        ).read_text(encoding="utf-8")
        assert "self._lock = threading.Lock()" in source
        assert source.count("with self._lock:") >= 2
        assert (
            "threading.Lock()"
            not in source.split("self._lock")[0].split("__init__")[-1]
        )
