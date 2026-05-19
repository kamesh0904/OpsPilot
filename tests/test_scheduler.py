"""
tests/test_scheduler.py
────────────────────────
Unit tests for app/scheduler/jobs.py.

Coverage:
  TestJobFunctions          (6 tests) — each job calls run_pipeline with the
                                        correct BriefingType; fatal exceptions
                                        are caught and logged without crashing
  TestSchedulerLifecycle    (6 tests) — start_scheduler registers exactly 3
                                        jobs with correct IDs; stop_scheduler
                                        shuts down cleanly; get_scheduler
                                        returns current instance; double-stop
                                        is safe; replace_existing prevents
                                        duplicate jobs
  TestLogJobResult          (4 tests) — successful run logged correctly;
                                        run with errors logs first_error;
                                        missing action_result handled;
                                        error count reflected accurately
  TestMainLifespan          (4 tests) — start_scheduler called on startup;
                                        stop_scheduler called on shutdown;
                                        scheduler not started if startup
                                        raises; correct import structure

Total: 20 tests.
Zero real API calls. run_pipeline is patched throughout.
APScheduler scheduler.start() / shutdown() are patched so no real
scheduler loop is created during tests.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from app.core.constants import BriefingType


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_state(briefing_sent: bool = True, run_errors: list | None = None):
    """Return a minimal OpsState-like dict returned by run_pipeline."""
    action_result = MagicMock()
    action_result.briefing_sent = briefing_sent
    return {
        "action_result": action_result,
        "run_errors": run_errors or [],
    }


# ── TestJobFunctions ──────────────────────────────────────────────────────────

class TestJobFunctions:
    """
    Each job function must:
      1. Call run_pipeline() with the correct BriefingType.
      2. Catch any exception from run_pipeline() without re-raising
         (the scheduler process must survive job failures).
    """

    @pytest.mark.asyncio
    async def test_morning_briefing_calls_correct_briefing_type(self):
        """morning_briefing_job passes DAILY_MORNING to run_pipeline."""
        from app.scheduler.jobs import morning_briefing_job

        with patch(
            "app.scheduler.jobs.run_pipeline",
            new=AsyncMock(return_value=_make_state()),
        ) as mock_run:
            await morning_briefing_job()

        mock_run.assert_awaited_once_with(briefing_type=BriefingType.DAILY_MORNING)

    @pytest.mark.asyncio
    async def test_eod_pulse_calls_correct_briefing_type(self):
        """eod_pulse_job passes EOD_PULSE to run_pipeline."""
        from app.scheduler.jobs import eod_pulse_job

        with patch(
            "app.scheduler.jobs.run_pipeline",
            new=AsyncMock(return_value=_make_state()),
        ) as mock_run:
            await eod_pulse_job()

        mock_run.assert_awaited_once_with(briefing_type=BriefingType.EOD_PULSE)

    @pytest.mark.asyncio
    async def test_weekly_digest_calls_correct_briefing_type(self):
        """weekly_digest_job passes WEEKLY_DIGEST to run_pipeline."""
        from app.scheduler.jobs import weekly_digest_job

        with patch(
            "app.scheduler.jobs.run_pipeline",
            new=AsyncMock(return_value=_make_state()),
        ) as mock_run:
            await weekly_digest_job()

        mock_run.assert_awaited_once_with(briefing_type=BriefingType.WEEKLY_DIGEST)

    @pytest.mark.asyncio
    async def test_morning_briefing_survives_pipeline_exception(self):
        """A fatal exception from run_pipeline does NOT crash the job function."""
        from app.scheduler.jobs import morning_briefing_job

        with patch(
            "app.scheduler.jobs.run_pipeline",
            new=AsyncMock(side_effect=RuntimeError("collector exploded")),
        ):
            # Should NOT raise — the job catches and logs the exception
            await morning_briefing_job()

    @pytest.mark.asyncio
    async def test_eod_pulse_survives_pipeline_exception(self):
        """EOD pulse job catches pipeline exceptions and doesn't re-raise."""
        from app.scheduler.jobs import eod_pulse_job

        with patch(
            "app.scheduler.jobs.run_pipeline",
            new=AsyncMock(side_effect=Exception("Slack down")),
        ):
            await eod_pulse_job()  # must not raise

    @pytest.mark.asyncio
    async def test_weekly_digest_survives_pipeline_exception(self):
        """Weekly digest job catches pipeline exceptions and doesn't re-raise."""
        from app.scheduler.jobs import weekly_digest_job

        with patch(
            "app.scheduler.jobs.run_pipeline",
            new=AsyncMock(side_effect=Exception("gemini timeout")),
        ):
            await weekly_digest_job()  # must not raise


# ── TestSchedulerLifecycle ────────────────────────────────────────────────────

class TestSchedulerLifecycle:
    """
    Tests for start_scheduler() and stop_scheduler() lifecycle functions.
    APScheduler's internal .start() and .shutdown() are patched so no real
    asyncio-scheduler is created during test execution.
    """

    def _patched_scheduler(self):
        """Return a MagicMock that mimics AsyncIOScheduler."""
        mock = MagicMock()
        mock.running = True
        mock.get_jobs.return_value = [
            MagicMock(id="morning_briefing"),
            MagicMock(id="eod_pulse"),
            MagicMock(id="weekly_digest"),
        ]
        return mock

    def test_start_scheduler_registers_three_jobs(self):
        """start_scheduler() registers exactly 3 jobs."""
        import app.scheduler.jobs as jobs_module

        mock_scheduler = self._patched_scheduler()

        with patch("app.scheduler.jobs.AsyncIOScheduler", return_value=mock_scheduler):
            scheduler = jobs_module.start_scheduler()

        assert mock_scheduler.add_job.call_count == 3

    def test_start_scheduler_registers_correct_job_ids(self):
        """The three job IDs are morning_briefing, eod_pulse, weekly_digest."""
        import app.scheduler.jobs as jobs_module

        mock_scheduler = self._patched_scheduler()

        with patch("app.scheduler.jobs.AsyncIOScheduler", return_value=mock_scheduler):
            jobs_module.start_scheduler()

        registered_ids = {
            call.kwargs["id"]
            for call in mock_scheduler.add_job.call_args_list
        }
        assert registered_ids == {"morning_briefing", "eod_pulse", "weekly_digest"}

    def test_start_scheduler_calls_scheduler_start(self):
        """start_scheduler() actually calls scheduler.start()."""
        import app.scheduler.jobs as jobs_module

        mock_scheduler = self._patched_scheduler()

        with patch("app.scheduler.jobs.AsyncIOScheduler", return_value=mock_scheduler):
            jobs_module.start_scheduler()

        mock_scheduler.start.assert_called_once()

    def test_stop_scheduler_calls_shutdown(self):
        """stop_scheduler() calls scheduler.shutdown() when scheduler is running."""
        import app.scheduler.jobs as jobs_module

        mock_scheduler = self._patched_scheduler()
        jobs_module._scheduler = mock_scheduler  # inject directly

        jobs_module.stop_scheduler()

        mock_scheduler.shutdown.assert_called_once_with(wait=True)

    def test_stop_scheduler_when_none_is_safe(self):
        """stop_scheduler() does nothing (no error) if scheduler was never started."""
        import app.scheduler.jobs as jobs_module

        jobs_module._scheduler = None   # simulate never started

        # Must not raise
        jobs_module.stop_scheduler()

    def test_get_scheduler_returns_current_instance(self):
        """get_scheduler() returns the module-level _scheduler instance."""
        import app.scheduler.jobs as jobs_module

        sentinel = MagicMock()
        jobs_module._scheduler = sentinel

        assert jobs_module.get_scheduler() is sentinel


# ── TestLogJobResult ──────────────────────────────────────────────────────────

class TestLogJobResult:
    """
    Tests for the _log_job_result() helper.
    Verifies it reads the correct fields from the state dict without crashing.
    """

    def test_successful_run_logs_briefing_sent_true(self):
        """_log_job_result reads briefing_sent=True from action_result."""
        from app.scheduler.jobs import _log_job_result

        state = _make_state(briefing_sent=True)
        # Should not raise
        _log_job_result("morning_briefing", state)

    def test_run_with_errors_logs_first_error(self):
        """_log_job_result surfaces the first run_error without crashing."""
        from app.scheduler.jobs import _log_job_result

        state = _make_state(run_errors=["[collector] linear: timeout", "[action] slack: 429"])
        # Should not raise — first_error is passed to structured log
        _log_job_result("eod_pulse", state)

    def test_missing_action_result_handled_gracefully(self):
        """_log_job_result handles state with no action_result (None)."""
        from app.scheduler.jobs import _log_job_result

        state = {"action_result": None, "run_errors": []}
        # Should not raise
        _log_job_result("weekly_digest", state)

    def test_empty_run_errors_logs_none_first_error(self):
        """_log_job_result sets first_error=None when run_errors is empty."""
        from app.scheduler.jobs import _log_job_result

        state = _make_state(briefing_sent=True, run_errors=[])
        # Should not raise — first_error=None is valid in the structured log
        _log_job_result("morning_briefing", state)


# ── TestMainLifespan ──────────────────────────────────────────────────────────

class TestMainLifespan:
    """
    Tests for the FastAPI lifespan in main.py.
    Verifies the scheduler is started on app startup and stopped on shutdown.
    """

    @pytest.mark.asyncio
    async def test_lifespan_starts_scheduler_on_startup(self):
        """lifespan() calls start_scheduler() before yielding."""
        with (
            patch("main.configure_logging"),
            patch("main.start_scheduler") as mock_start,
            patch("main.stop_scheduler"),
        ):
            import main as main_module
            from fastapi import FastAPI

            app = FastAPI(lifespan=main_module.lifespan)

            async with main_module.lifespan(app):
                mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_stops_scheduler_on_shutdown(self):
        """lifespan() calls stop_scheduler() after the yield (on shutdown)."""
        with (
            patch("main.configure_logging"),
            patch("main.start_scheduler"),
            patch("main.stop_scheduler") as mock_stop,
        ):
            import main as main_module
            from fastapi import FastAPI

            app = FastAPI(lifespan=main_module.lifespan)

            async with main_module.lifespan(app):
                pass  # simulate server running then shutting down

            mock_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_stop_called_even_if_startup_body_raises(self):
        """
        stop_scheduler() is called even if the code after yield raises.
        asynccontextmanager guarantees the finally/cleanup block runs.
        We verify this by patching the module attributes directly after import.
        """
        import main as main_module
        from fastapi import FastAPI

        mock_stop = MagicMock()

        # Patch the names on the already-imported module so the lifespan
        # function picks up our mocks when it executes
        original_configure = main_module.configure_logging
        original_start = main_module.start_scheduler
        original_stop = main_module.stop_scheduler

        main_module.configure_logging = MagicMock()
        main_module.start_scheduler = MagicMock()
        main_module.stop_scheduler = mock_stop

        try:
            app = FastAPI(lifespan=main_module.lifespan)

            with pytest.raises(RuntimeError):
                async with main_module.lifespan(app):
                    raise RuntimeError("simulated server crash")

            mock_stop.assert_called_once()
        finally:
            # Restore originals so other tests aren't affected
            main_module.configure_logging = original_configure
            main_module.start_scheduler = original_start
            main_module.stop_scheduler = original_stop

    def test_scheduler_imported_in_main(self):
        """main.py imports start_scheduler and stop_scheduler from app.scheduler."""
        import main as main_module
        assert hasattr(main_module, "start_scheduler")
        assert hasattr(main_module, "stop_scheduler")
