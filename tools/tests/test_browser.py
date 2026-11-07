"""The diagnostic response recorder must not interrupt normal Dash requests."""

from __future__ import annotations

import copy
import json
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tools.check_browser import FIXTURE, ResponseBarrier, callback_values, fixture_selection


class BrowserFixtureTests(unittest.TestCase):
    def test_saved_review_is_bound_to_its_exact_assessment_instead_of_baseline(self):
        path = (
            Path(__file__).resolve().parents[2]
            / "evidence-explorer/tests/fixtures/case_packages"
            / FIXTURE
        )
        with zipfile.ZipFile(path) as archive:
            bundle = json.loads(archive.read("bundle.json"))
        selected = fixture_selection(bundle)
        self.assertEqual(
            selected["reviewed_assessment_id"], "review:834fd03c-f78f-4904-8f07-213e0dfee054:v1"
        )
        self.assertEqual(
            selected["native_assessment_id"],
            "swarm:2b1d8ee8e7de:case:scenario:e178636a7df2258db87c65b0",
        )
        bundle["reviews"][0]["scenario_id"] = None
        with self.assertRaisesRegex(RuntimeError, "explicitly reviewed assessment"):
            fixture_selection(bundle)


class BrowserRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def record(self, body):
        route = SimpleNamespace(
            request=SimpleNamespace(
                url="http://127.0.0.1:8053/_dash-update-component", post_data_json=body
            ),
            continue_=AsyncMock(),
            fetch=AsyncMock(),
            abort=AsyncMock(),
        )
        events = []
        barrier = ResponseBarrier("http://127.0.0.1:8053", events)
        await barrier.route(route)
        route.continue_.assert_awaited_once_with()
        route.fetch.assert_not_awaited()
        route.abort.assert_not_awaited()
        return barrier, events

    async def test_normal_callback_is_recorded_and_continued(self):
        body = {
            "output": "rv-graph-summary.children",
            "changedPropIds": ["rv-page.value"],
            "inputs": [{"id": "rv-page", "property": "value", "value": "compare"}],
            "state": [{"id": "rv-session", "property": "data", "value": {"scenario_id": "saved"}}],
        }
        barrier, events = await self.record(body)
        self.assertEqual(barrier.recording_errors, [])
        self.assertEqual(events[-1]["page"], "compare")
        self.assertEqual(events[-1]["assessment"], "saved")

    async def test_dash_pattern_input_arrays_do_not_strand_unrelated_callbacks(self):
        # Dash sends each ALL input as an array of property objects, including
        # an empty array when nothing matches. Attempt 2 failed on this shape;
        # these identifiers are synthetic, not a claimed raw-payload capture.
        body = {
            "output": "..live-revision.data...live-action.children..",
            "changedPropIds": [],
            "inputs": [
                {"id": "live-add", "property": "n_clicks", "value": 0},
                {"id": "live-collect-all", "property": "n_clicks", "value": 0},
                [
                    {
                        "id": {"type": "live-collect", "sid": "fixture"},
                        "property": "n_clicks",
                        "value": 0,
                    }
                ],
                [],
            ],
            "state": [{"id": "live-platform", "property": "value", "value": "fixture"}],
        }
        before = copy.deepcopy(body)
        self.assertEqual(
            callback_values(body),
            {"live-add": 0, "live-collect-all": 0, "live-platform": "fixture"},
        )
        barrier, events = await self.record(body)
        self.assertEqual(barrier.recording_errors, [])
        self.assertEqual(events[-1]["stage"], "callback")
        self.assertEqual(body, before)

    async def test_malformed_diagnostics_fail_the_gate_but_continue_the_request(self):
        for body in ([], {"inputs": None}, {"inputs": ["unexpected"]}, {"output": []}):
            with self.subTest(body=body):
                barrier, events = await self.record(body)
                self.assertEqual(len(barrier.recording_errors), 1)
                self.assertEqual(events[-1]["stage"], "recorder-error")


if __name__ == "__main__":
    unittest.main()
