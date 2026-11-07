"""Check real tab rendering while genuine assessment responses are delayed.

This explicit browser gate reuses the root runtime check's isolated launcher and
imports a frozen fictional case. It clicks ordinary controls, holds unmodified
HTTP responses, and checks the rendered DOM. It never posts callbacks directly,
changes browser stores, runs a worker, or creates an analyst decision.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .check_runtime import (
        RuntimeCheck,
        assert_idle,
        package_contents,
        require,
        source_hashes,
    )
else:
    from check_runtime import (
        RuntimeCheck,
        assert_idle,
        package_contents,
        require,
        source_hashes,
    )

PAGES = {"cases": "Cases", "review": "Review", "compare": "Compare", "brief": "Briefing"}
FIXTURE = "evidence-trail-reviewed-swarm.zip"
FIXTURE_SHA256 = "db29d709848bfbc74e83b6a79dd99dd795e3532fccc23eed005b58bcb0379268"
STATE_KEYS = (
    "case_id",
    "case_fingerprint",
    "finding_id",
    "record_id",
    "scenario_id",
    "scenario_version",
    "comparison_scenario_id",
    "job_id",
    "revision",
)
DOM_STATE = """() => {
    const visible = node => !!node && node.getClientRects().length > 0 &&
        getComputedStyle(node).display !== 'none' &&
        getComputedStyle(node).visibility !== 'hidden';
    let session = null;
    try { session = JSON.parse(sessionStorage.getItem('rv-session') || 'null'); } catch (_) {}
    return {
        selected: [...document.querySelectorAll('#rv-page .rv-navigation-tab--selected')]
            .map(node => node.textContent.trim()),
        pages: ['cases','review','compare','brief'].map(key => {
            const node = document.getElementById('rv-' + key + '-page');
            return {key, visible: visible(node), style: node?.getAttribute('style')};
        }),
        session
    };
}"""


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def evidence_state(state: dict) -> dict:
    return {key: state.get(key) for key in STATE_KEYS}


def fixture_selection(bundle: dict) -> dict:
    """Use the saved review's declared assessment, without moving it to baseline."""
    reviewed_ids = {
        review.get("scenario_id")
        for review in bundle["reviews"]
        if review.get("disposition") in {"use", "qualify"}
    }
    require(
        len(reviewed_ids) == 1 and None not in reviewed_ids,
        "Fixture needs one explicitly reviewed assessment",
    )
    reviewed_id = reviewed_ids.pop()
    reviewed = next(item for item in bundle["scenarios"] if item["id"] == reviewed_id)
    native = [
        item
        for item in bundle["scenarios"]
        if item.get("status") == "completed"
        and item.get("result_run_id") == reviewed["base_run_id"]
        and not item.get("source_scenario_id")
    ]
    require(len(native) == 1, "Fixture must declare one native result for the reviewed run")
    return {
        "case_id": bundle["case"]["id"],
        "reviewed_assessment_id": reviewed_id,
        "native_assessment_id": native[0]["id"],
    }


def callback_values(body: dict) -> dict:
    """Read ordinary properties without assuming pattern inputs are flat."""
    if not isinstance(body, dict):
        raise ValueError("Callback payload must be an object")
    values = {}

    def visit(items):
        for item in items:
            if isinstance(item, list):
                visit(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                values[item["id"]] = item.get("value")
            elif not isinstance(item, dict):
                raise ValueError("Callback properties must be objects or pattern-input arrays")

    for name in ("inputs", "state"):
        items = body.get(name, [])
        if not isinstance(items, list):
            raise ValueError(f"Callback {name} must be an array")
        visit(items)
    return values


def runtime_versions() -> dict:
    """Record effective installed versions, including an unavailable optional tool."""
    versions = {"python": platform.python_version(), "python_executable": sys.executable}
    for package in ("dash", "playwright"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


async def bounded_capture(label: str, operation, report: dict, *, timeout: float = 3):
    """Keep diagnostic failures separate from the original browser assertion."""
    try:
        return await asyncio.wait_for(operation, timeout=timeout)
    except Exception as exc:
        report.setdefault("capture_errors", []).append(f"{label}: {type(exc).__name__}: {exc}")
        return None


class ResponseBarrier:
    """Hold one genuine response without rewriting its status, headers, or body."""

    def __init__(self, origin: str, events: list[dict]):
        self.origin = origin
        self.events = events
        self.mode = None
        self.target = None
        self.claimed = False
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.error = None
        self.recording_errors = []

    def event(self, stage: str, **values) -> None:
        self.events.append({"stage": stage, "at_monotonic": time.monotonic(), **values})

    def arm(self, mode: str, target: str | None) -> None:
        self.mode, self.target, self.claimed = mode, target, False
        self.ready, self.release, self.finished = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.error = None
        self.event("armed", mode=mode, assessment=target)

    async def route(self, route) -> None:
        request = route.request
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != self.origin:
            self.event("external-request-blocked", url=request.url)
            await route.abort()
            return
        if url.path != "/_dash-update-component":
            await route.continue_()
            return
        try:
            body = request.post_data_json
            values = callback_values(body)
            output = body.get("output", "")
            changed = body.get("changedPropIds", [])
            state = values.get("rv-session") or {}
            if (
                not isinstance(output, str)
                or not isinstance(changed, list)
                or not isinstance(state, dict)
            ):
                raise ValueError("Unrecognized callback metadata shape")
            self.event(
                "callback",
                output=output,
                changed=changed,
                assessment=state.get("scenario_id"),
                page=values.get("rv-page"),
            )
            matches = (
                self.mode == "session"
                and "rv-action-message.children" in output
                and "rv-scenario.value" in changed
                and (
                    values.get("rv-scenario") == self.target
                    if self.target is not None
                    else values.get("rv-scenario") not in {None, "baseline"}
                )
            ) or (
                self.mode == "workspace"
                and "rv-context.children" in output
                and "rv-session.data" in changed
                and state.get("scenario_id") == (None if self.target == "baseline" else self.target)
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.recording_errors.append(error)
            self.event("recorder-error", error=error)
            # Diagnostic parsing must never strand an ordinary application request.
            await route.continue_()
            return
        if not matches or self.claimed:
            await route.continue_()
            return
        self.claimed = True
        if self.mode == "session" and self.target is None:
            self.target = values["rv-scenario"]
        try:
            response = await route.fetch(timeout=15000)
            require(response.status == 200, "Held callback did not return a successful response")
            if self.mode == "session":
                updated = (await response.json())["response"]["rv-session"]["data"]
                require(
                    updated["scenario_id"] == self.target,
                    "Held response selected another assessment",
                )
            self.event(
                "response-held",
                mode=self.mode,
                output=output,
                status=response.status,
                assessment=self.target,
            )
            self.ready.set()
            await self.release.wait()
            await route.fulfill(response=response)
            self.event("response-released", mode=self.mode, output=output)
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()
            self.release.set()
            self.event("response-error", error=self.error)
            try:
                await asyncio.wait_for(route.abort(), timeout=3)
            except Exception as abort_error:
                self.event("response-abort-error", error=str(abort_error))
        finally:
            self.finished.set()


async def visible_page(page, key: str) -> dict:
    """Require both selected-tab styling and exactly one visible page section."""
    await page.wait_for_function(
        """({key, label}) => {
            const selected = [...document.querySelectorAll('#rv-page .rv-navigation-tab--selected')];
            const pages = ['cases','review','compare','brief'].filter(name => {
                const node = document.getElementById('rv-' + name + '-page');
                return node && node.getClientRects().length &&
                    getComputedStyle(node).display !== 'none' &&
                    getComputedStyle(node).visibility !== 'hidden';
            });
            return selected.length === 1 && selected[0].textContent.trim() === label &&
                pages.length === 1 && pages[0] === key;
        }""",
        arg={"key": key, "label": PAGES[key]},
        timeout=15000,
    )
    return await page.evaluate(DOM_STATE)


async def click_tab(page, key: str) -> dict:
    await page.locator("#rv-page .rv-navigation-tab").get_by_text(PAGES[key], exact=True).click()
    return await visible_page(page, key)


async def browser_checks(origin: str, selection: dict, artifacts: Path) -> dict:
    from playwright.async_api import async_playwright

    case_id = selection["case_id"]
    report = {
        "checks": [],
        "events": [],
        "errors": [],
        "viewport": {"width": 1280, "height": 900},
        "dash_renderer_script_urls": [],
        "fixture_selection": selection,
        "observation_errors": [],
    }
    labels = {}
    options_ready = asyncio.Event()
    observations = set()

    async def record_response(response) -> None:
        try:
            if response.status != 200:
                return
            path = urlsplit(response.url).path
            if response.request.resource_type == "script" and "dash_renderer" in path:
                if response.url not in report["dash_renderer_script_urls"]:
                    report["dash_renderer_script_urls"].append(response.url)
            elif path == "/_dash-update-component":
                body = response.request.post_data_json
                if not isinstance(body, dict) or "rv-scenario.options" not in str(
                    body.get("output", "")
                ):
                    return
                data = await response.json()
                options = data.get("response", {}).get("rv-scenario", {}).get("options")
                if options is not None:
                    labels.update({option["value"]: option["label"] for option in options})
                    report["served_assessment_labels"] = dict(labels)
                    options_ready.set()
        except Exception as exc:
            report["observation_errors"].append(f"{type(exc).__name__}: {exc}")
            options_ready.set()

    def observe(response) -> None:
        task = asyncio.create_task(record_response(response))
        observations.add(task)
        task.add_done_callback(observations.discard)

    async def choose_assessment(page, assessment_id: str) -> None:
        require(assessment_id in labels, "Pinned assessment is absent from served picker options")
        await page.locator("#rv-scenario").click()
        option = page.get_by_role("option", name=labels[assessment_id], exact=True)
        await option.first.wait_for(state="visible")
        require(await option.count() == 1, "Expected one unambiguous assessment option")
        await option.click()

    async def assessment_reached(page, assessment_id: str) -> None:
        await page.wait_for_function(
            "value => JSON.parse(sessionStorage.getItem('rv-session') || 'null')?.scenario_id === value",
            arg=None if assessment_id == "baseline" else assessment_id,
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        report["browser_version"] = browser.version
        context = await browser.new_context(viewport=report["viewport"], service_workers="block")
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await context.new_page()
        page.set_default_timeout(15000)
        barrier = ResponseBarrier(origin, report["events"])
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        page.on("response", observe)
        await context.route("**/*", barrier.route)
        try:
            await page.goto(origin, wait_until="domcontentloaded")
            await page.wait_for_function(
                """id => {
                    let state = null;
                    try { state = JSON.parse(sessionStorage.getItem('rv-session') || 'null'); }
                    catch (_) { return false; }
                    return state?.case_id === id && document.getElementById('rv-page') &&
                        document.getElementById('rv-scenario');
                }""",
                arg=case_id,
            )
            initial = (await visible_page(page, "review"))["session"]
            require(initial.get("scenario_id") is None, "Fixture must start on its baseline")
            await asyncio.wait_for(options_ready.wait(), timeout=15)
            require(not report["observation_errors"], "Could not read served assessment options")
            for key in PAGES:
                state = await click_tab(page, key)
                require(
                    evidence_state(state["session"]) == evidence_state(initial),
                    "Tab navigation changed evidence selection",
                )
                report["checks"].append({"kind": "ordinary-tab", "page": key, "state": state})

            await choose_assessment(page, selection["reviewed_assessment_id"])
            await assessment_reached(page, selection["reviewed_assessment_id"])
            await visible_page(page, "brief")
            citations = page.locator('#rv-brief-preview button[id*="rv-citation"]')
            await citations.first.wait_for(state="visible")
            require(await citations.count() > 0, "Saved reviewed fixture has no citation to open")
            citation = citations.first
            target = json.loads(await citation.get_attribute("id"))
            await citation.click()
            state = await visible_page(page, "review")
            require(
                state["session"]["record_id"] == target["rid"]
                and state["session"]["finding_id"] == target["fid"],
                "Citation opened another evidence reference",
            )
            report["checks"].append(
                {"kind": "citation-navigation", "target": target, "state": state}
            )

            next_action = page.locator('#rv-next-action-slot button[id*="rv-next-action"]')
            await next_action.wait_for(state="visible")
            action = json.loads(await next_action.get_attribute("id"))
            require(
                action["action"] in {"review", "source"} and action["fid"] and action["rid"],
                "Reviewed fixture must offer a concrete evidence/review next action",
            )
            focus_id = "rv-source" if action["action"] == "source" else "rv-review-reason"
            await next_action.click()
            await visible_page(page, "review")
            await page.wait_for_function(
                """target => {
                    const state = JSON.parse(sessionStorage.getItem('rv-session') || 'null');
                    return state?.finding_id === target.finding && state.record_id === target.record &&
                        document.activeElement?.id === target.focus;
                }""",
                arg={"finding": action["fid"], "record": action["rid"], "focus": focus_id},
            )
            report["checks"].append(
                {
                    "kind": "next-action-focus",
                    "target": action,
                    "focused_id": focus_id,
                    "state": await page.evaluate(DOM_STATE),
                }
            )

            await choose_assessment(page, "baseline")
            await assessment_reached(page, "baseline")
            for mode in ("session", "workspace"):
                before = (await click_tab(page, "compare"))["session"]
                target_id = selection["native_assessment_id"] if mode == "session" else "baseline"
                barrier.arm(mode, target_id)
                await choose_assessment(page, target_id)
                await asyncio.wait_for(barrier.ready.wait(), timeout=20)
                require(not barrier.error, f"Response barrier failed: {barrier.error}")
                held = (await page.evaluate(DOM_STATE))["session"]
                if mode == "session":
                    require(
                        evidence_state(held) == evidence_state(before),
                        "Held session response reached the browser early",
                    )
                try:
                    state = await click_tab(page, "brief")
                    require(
                        not barrier.release.is_set(),
                        "Response was released before the held-state assertion",
                    )
                    require(
                        evidence_state(state["session"]) == evidence_state(held),
                        "Tab click changed the held evidence state",
                    )
                    report["checks"].append(
                        {"kind": "tab-during-held-response", "mode": mode, "state": state}
                    )
                finally:
                    barrier.release.set()
                await asyncio.wait_for(barrier.finished.wait(), timeout=20)
                require(not barrier.error, f"Response release failed: {barrier.error}")
                await assessment_reached(page, target_id)
                state = await visible_page(page, "brief")
                require(
                    state["session"]["case_id"] == case_id, "Assessment navigation changed the case"
                )
                require(
                    state["session"]["case_fingerprint"] == initial["case_fingerprint"],
                    "Assessment navigation changed saved evidence",
                )
                report["checks"].append(
                    {"kind": "tab-after-response-release", "mode": mode, "state": state}
                )
                barrier.mode = None
            require(not report["errors"], f"Browser JavaScript errors: {report['errors']}")
            require(
                not report["observation_errors"],
                f"Response observation failed: {report['observation_errors']}",
            )
            require(
                not barrier.recording_errors, f"Request recorder failed: {barrier.recording_errors}"
            )
            require(
                report["dash_renderer_script_urls"], "No served Dash renderer script was recorded"
            )
            report["passed"] = True
            return report
        except Exception:
            report["failure"] = traceback.format_exc()
            report["dom_state"] = await bounded_capture(
                "DOM state", page.evaluate(DOM_STATE), report
            )
            content = await bounded_capture("HTML", page.content(), report)
            if content is not None:
                (artifacts / "failure.html").write_text(content)
            await bounded_capture(
                "viewport screenshot",
                page.screenshot(path=str(artifacts / "failure.png"), full_page=False, timeout=2500),
                report,
            )
            raise
        finally:
            barrier.release.set()
            page.remove_listener("response", observe)
            if barrier.claimed and not barrier.finished.is_set():
                await bounded_capture("response drain", barrier.finished.wait(), report, timeout=5)
            if observations:
                await bounded_capture(
                    "response observations", asyncio.gather(*observations), report
                )
            await bounded_capture(
                "trace",
                context.tracing.stop(path=str(artifacts / "trace.zip")),
                report,
                timeout=8,
            )
            await bounded_capture("context close", context.close(), report, timeout=5)
            await bounded_capture("browser close", browser.close(), report, timeout=5)
            cleanup_failed = report.get("passed") and (
                report.get("capture_errors")
                or report["observation_errors"]
                or barrier.error
                or barrier.recording_errors
            )
            if cleanup_failed:
                report["passed"] = False
                report["failure"] = "Browser diagnostics, response drain, or cleanup failed"
            write_json(artifacts / "browser.json", report)
            require(not cleanup_failed, "Browser diagnostics, response drain, or cleanup failed")


def check(root: Path, artifacts: Path, report: dict) -> None:
    require(
        importlib.util.find_spec("playwright") is not None,
        "Run make browser-setup before the explicit browser check",
    )
    fixture = root / "evidence-explorer/tests/fixtures/case_packages" / FIXTURE
    require(
        hashlib.sha256(fixture.read_bytes()).hexdigest() == FIXTURE_SHA256,
        "Frozen fixture bytes changed",
    )
    selection = fixture_selection(json.loads(package_contents(fixture)["bundle.json"]))
    case_id = selection["case_id"]
    before = source_hashes(root)
    before["tools/check_browser.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_json(artifacts / "source-before.json", before)
    with tempfile.TemporaryDirectory(prefix="traced-browser-check-") as temporary:
        runtime = RuntimeCheck(root, Path(temporary))
        store = Path(temporary) / "reviewed fictional case"
        try:
            require(
                runtime.run(store, "review", "import", str(fixture)).strip() == case_id,
                "Fixture import changed case identity",
            )
            exported = Path(temporary) / "before.zip"
            runtime.run(store, "review", "export", case_id, str(exported))
            saved = package_contents(exported)
            with runtime.server(store, worker=False) as port:
                report["browser"] = asyncio.run(
                    browser_checks(f"http://127.0.0.1:{port}", selection, artifacts)
                )
            reopened = Path(temporary) / "after.zip"
            runtime.run(store, "review", "export", case_id, str(reopened))
            require(
                saved == package_contents(reopened),
                "Browser navigation changed saved case contents",
            )
            assert_idle(store / "evidence.sqlite3")
            report["saved_evidence_unchanged"] = True
        finally:
            report["commands"], report["cleanup"] = runtime.commands, runtime.cleanup
    after = source_hashes(root)
    after["tools/check_browser.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_json(artifacts / "source-after.json", after)
    require(before == after, "Source changed during the browser gate")
    report["source_unchanged"] = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("dist/browser-check.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report_path = args.report.resolve()
    artifacts = report_path.parent / (report_path.stem + "-artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    report = {
        "passed": False,
        "scope": "Automated fictional browser navigation; no worker, detector, live collection, model, or human review.",
        "artifacts": str(artifacts),
        "runtime_versions": runtime_versions(),
    }
    try:
        check(root, artifacts, report)
        report["passed"] = True
    except Exception:
        report["failure"] = traceback.format_exc()
    finally:
        write_json(report_path, report)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
