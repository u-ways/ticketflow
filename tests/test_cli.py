"""CLI tests: read-only projections and intent-writing commands (ADR-0004)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ticketflow
from ticketflow.cli.app import app
from ticketflow.cli.factory import open_store
from ticketflow.config import load_config
from ticketflow.domain.model import NodeState

runner = CliRunner()
T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "ticketflow.toml"
    path.write_text(
        f"""
state_dir = "{tmp_path / ".ticketflow"}"

[tracker]
provider = "github"
repo = "o/r"

[codehost]
repo = "o/r"
"""
    )
    return path


def test_version_command_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert ticketflow.__version__ in result.output


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "ticketflow" in result.output


class TestInit:
    def test_writes_starter_config(self, tmp_path: Path) -> None:
        target = tmp_path / "ticketflow.toml"
        result = runner.invoke(app, ["init", str(target)])
        assert result.exit_code == 0
        config = load_config(target.parent / "ticketflow.toml")
        assert config.runner.name == "claude"

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        target = tmp_path / "ticketflow.toml"
        target.write_text("existing")
        result = runner.invoke(app, ["init", str(target)])
        assert result.exit_code == 1
        assert target.read_text() == "existing"


class TestMissingConfig:
    def test_status_without_config_exits_2(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["status", "--config", str(tmp_path / "nope.toml")])
        assert result.exit_code == 2


class TestReadOnlyViews:
    def test_status_lists_nodes_with_refs(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        store = open_store(load_config(config_path))
        store.insert_node(node_id="abc", title="Build it", body="", state=NodeState.READY, now=T0)
        store.link_external("abc", provider="github", external_key="#7")
        store.close()
        result = runner.invoke(app, ["status", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "abc" in result.output
        assert "#7" in result.output
        assert "ready" in result.output

    def test_escalations_show_reason_and_resolution_hint(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        store = open_store(load_config(config_path))
        store.insert_node(
            node_id="abc",
            title="Broken",
            body="",
            state=NodeState.ESCALATED,
            blocked_reason="wall-clock timeout",
            now=T0,
        )
        store.close()
        result = runner.invoke(app, ["escalations", "--config", str(config_path)])
        assert "wall-clock timeout" in result.output
        assert "ticketflow retry abc" in result.output

    def test_events_tail(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        store = open_store(load_config(config_path))
        store.append_event("dispatched", now=T0, node_id="abc", attempt=1)
        store.close()
        result = runner.invoke(app, ["events", "--config", str(config_path)])
        assert "dispatched" in result.output
        assert "node=abc" in result.output


class TestIntentCommands:
    def test_retry_with_feedback_writes_intent(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        result = runner.invoke(
            app,
            ["retry", "abc", "--config", str(config_path), "--feedback", "Mind the gap."],
        )
        assert result.exit_code == 0
        store = open_store(load_config(config_path))
        pending = store.unprocessed_intents()
        store.close()
        assert len(pending) == 1
        assert pending[0].intent_type == "retry"
        assert pending[0].node_id == "abc"
        assert pending[0].payload == {"feedback": "Mind the gap."}
        assert pending[0].source == "cli"

    def test_global_resume_has_no_node(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        runner.invoke(app, ["resume", "--config", str(config_path)])
        store = open_store(load_config(config_path))
        pending = store.unprocessed_intents()
        store.close()
        assert pending[0].intent_type == "resume"
        assert pending[0].node_id is None

    def test_cancel_and_unblock(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        runner.invoke(app, ["cancel", "abc", "--config", str(config_path)])
        runner.invoke(app, ["unblock", "abc", "--config", str(config_path)])
        store = open_store(load_config(config_path))
        kinds = [i.intent_type for i in store.unprocessed_intents()]
        store.close()
        assert kinds == ["cancel", "unblock"]


class TestRunResilience:
    """A transient adapter error must not kill the loop (found live: one
    flaky GitHub response crashed a run mid-epic)."""

    class _Store:
        def close(self) -> None: ...

    @staticmethod
    def _wire(monkeypatch: pytest.MonkeyPatch, orchestrator: object) -> None:
        import ticketflow.cli.factory as factory

        monkeypatch.setattr(factory, "open_store", lambda _cfg: TestRunResilience._Store())
        monkeypatch.setattr(
            factory,
            "build_orchestrator",
            lambda _cfg, _store, *, yolo=False: orchestrator,  # noqa: ARG005
        )

    def test_transient_tick_errors_are_survived(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ticketflow.orchestrator.core import TickReport

        class Flaky:
            calls = 0

            def adopt(self) -> None: ...

            def tick(self) -> TickReport:
                Flaky.calls += 1
                if Flaky.calls <= 2:
                    raise RuntimeError("server disconnected")
                return TickReport(halted=True)  # clean exit path for the test

        self._wire(monkeypatch, Flaky())
        config_path = write_config(tmp_path)
        result = runner.invoke(app, ["run", "--config", str(config_path), "--interval", "0"])
        assert Flaky.calls == 3  # two failures survived, then the halt
        assert result.exit_code == 3  # the halt, not the errors
        assert "tick failed" in result.output

    def test_persistent_tick_errors_stop_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Broken:
            def adopt(self) -> None: ...

            def tick(self) -> None:
                raise RuntimeError("hard down")

        self._wire(monkeypatch, Broken())
        config_path = write_config(tmp_path)
        result = runner.invoke(app, ["run", "--config", str(config_path), "--interval", "0"])
        assert result.exit_code == 4
        assert "5 consecutive" in result.output
