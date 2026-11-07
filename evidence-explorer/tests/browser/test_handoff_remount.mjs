// Run against the ordinary review app loaded with the frozen fictional demo fixture.
// Hold the ordinary responses in the browser test. A wrapper forces the unchanged handoff buttons to remount.
import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";

const [origin, manifestPath, output] = process.argv.slice(2);
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.TRACED_PLAYWRIGHT_MODULE || "playwright");
const fixture = JSON.parse(await readFile(manifestPath, "utf8"));
const [, comment] = fixture.exact_swarm_citations;
const browser = await chromium.launch({
  channel: "chromium", headless: true, chromiumSandbox: true,
  args: ["--no-proxy-server", "--disable-quic", "--disable-background-networking",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE 127.0.0.2, EXCLUDE [::1], EXCLUDE ::1, EXCLUDE 192.0.2.1"],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.setDefaultTimeout(35000);
const report = { status: "failed", requests: [], order: [], errors: [] };
let holdHandoff = false;
let precedingRecord;
let targetIntent;
let handoffResponse;
let selectionResponse;
const handoff = Promise.withResolvers();
const selected = Promise.withResolvers();
const waitFor = (promise, label) => {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(label)), 35000);
  })]).finally(() => clearTimeout(timer));
};
const stateProp = (body, id) => [...(body.inputs ?? []), ...(body.state ?? [])]
  .find(prop => prop?.id === id)?.value;
const stage = name => report.order.push({ name, at: Date.now() });
const findButtons = (value) => {
  if (Array.isArray(value)) return value.flatMap(findButtons);
  if (!value?.props) return [];
  const { id, n_clicks: clicks, children } = value.props;
  return [
    ...(id?.type === "rv-next-action" && id.location.startsWith("handoff-") ? [{ id, clicks }] : []),
    ...findButtons(children),
  ];
};

try {
  page.on("pageerror", error => report.errors.push(error.message));
  await page.route(url => new URL(url).pathname === "/_dash-update-component", async route => {
    const body = route.request().postDataJSON();
    const outputKey = body.output;
    const session = stateProp(body, "rv-session");
    const intent = stateProp(body, "rv-click-intent");
    if (outputKey.includes("rv-action-message.children")) {
      report.requests.push({ changed: body.changedPropIds, sessionRecord: session?.record_id, intent });
    }
    if (holdHandoff && outputKey.includes("trail-handoff-summary.children") &&
        body.changedPropIds.includes("trail-handoff-refresh.n_clicks") &&
        session?.record_id === precedingRecord) {
      const response = await route.fetch();
      const data = await response.json();
      const payload = data.response["trail-handoff-summary"];
      const buttons = findButtons(payload.children);
      if (!buttons.length) throw new Error("Expected genuine handoff controls in the held response");
      handoffResponse = { route, response, data, buttons };
      stage("handoff-response-held");
      handoff.resolve();
      return;
    }
    if (!selectionResponse && outputKey.includes("rv-action-message.children") &&
        intent?.trigger?.type === "rv-record" && intent.trigger.rid === comment.record_id &&
        body.changedPropIds.includes("rv-click-intent.data")) {
      const response = await route.fetch();
      const session = (await response.json()).response["rv-session"].data;
      assert.equal(session.record_id, comment.record_id);
      targetIntent = intent;
      selectionResponse = { route, response, session };
      stage("comment-response-held");
      selected.resolve();
      return;
    }
    await route.continue();
  });

  await page.goto(origin, { waitUntil: "domcontentloaded" });
  await page.locator("#rv-case").filter({ hasText: "Fictional core demo · Timing and supplied media" }).waitFor();
  const session = () => page.evaluate(() => window.dash_component_api.getLayout("rv-session")?.props.data);
  await page.locator("#rv-context").filter({ hasText: "Baseline" }).waitFor();
  await page.locator("#rv-finding-heading").filter({ hasText: "across 9 observed post families" }).waitFor();
  precedingRecord = (await session()).record_id;
  assert.ok(precedingRecord && precedingRecord !== comment.record_id);
  holdHandoff = true;
  // Start the ordinary refresh callback from a test control without changing pages.
  await page.evaluate(() => {
    const refresh = window.dash_component_api.getLayout("trail-handoff-refresh").props;
    window.dash_clientside.set_props("trail-handoff-refresh", { n_clicks: (refresh.n_clicks ?? 0) + 1 });
  });
  await waitFor(handoff.promise, "The pending handoff callback never returned");
  assert.equal((await session()).record_id, precedingRecord);
  const record = page.locator("#rv-source-buttons button").filter({ hasText: "syn-message-00002" });
  const recordId = await record.getAttribute("id");
  assert.equal(JSON.parse(recordId).rid, comment.record_id);
  await record.click();
  await waitFor(selected.promise, "The exact comment callback never returned");
  assert.equal((await session()).record_id, precedingRecord, "Selection response was not held");

  const requestsBeforeRemount = report.requests.length;
  const { route, response, data, buttons } = handoffResponse;
  assert.ok(buttons.length >= 4 && buttons.every(button => button.clicks === 0));
  holdHandoff = false;
  const summary = data.response["trail-handoff-summary"];
  summary.children = {
    namespace: "dash_html_components", type: "Div",
    props: { id: "test-handoff-remount", children: summary.children },
  };
  await route.fulfill({ response, json: data });
  await page.waitForFunction(ids => {
    const wrapper = document.getElementById("test-handoff-remount");
    return wrapper && ids.every(id => wrapper.contains(document.getElementById(id)));
  }, buttons.map(button => JSON.stringify(button.id, Object.keys(button.id).sort())));
  stage("zero-click-controls-mounted");
  // The clientside intent filter is synchronous. Yield one browser frame to let callbacks queue.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const zeroClickActionRequests = report.requests.length - requestsBeforeRemount;
  assert.equal(zeroClickActionRequests, 0, "A handoff remount scheduled the session writer");
  assert.equal((await session()).record_id, precedingRecord);
  assert.equal(await page.evaluate(() => window.dash_component_api.getLayout("rv-click-intent")?.props.data?.event_id), targetIntent.event_id);

  const rendered = page.waitForResponse(response => {
    if (new URL(response.url()).pathname !== "/_dash-update-component") return false;
    const body = response.request().postDataJSON();
    return body.output.includes("rv-source.children") &&
      stateProp(body, "rv-session")?.record_id === comment.record_id;
  });
  await selectionResponse.route.fulfill({ response: selectionResponse.response });
  stage("comment-response-released");
  assert.equal((await rendered).status(), 200);
  stage("comment-inspector-response-received");
  await page.locator("#rv-source").getByText(comment.exact_quote, { exact: true }).waitFor();
  await page.locator(`[id=${JSON.stringify(recordId)}][aria-pressed="true"]`).waitFor();
  assert.equal((await session()).record_id, comment.record_id);
  const inspector = await page.locator("#rv-source").textContent();
  assert.ok(inspector.includes(comment.record_id) && inspector.includes(comment.exact_quote));
  report.comment = { precedingRecord, recordId: (await session()).record_id, quote: comment.exact_quote, pressed: true,
    mounted: buttons, zeroClickActionRequests };
  stage("exact-comment-rendered");
  await page.locator("#rv-source").scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(output, "exact-comment.png") });

  assert.deepEqual(report.errors, []);
  report.status = "passed";
} catch (error) {
  report.failure = error.stack;
  await writeFile(join(output, "failure.html"), await page.content()).catch(() => {});
  await page.screenshot({ path: join(output, "failure.png"), timeout: 2500 }).catch(() => {});
  throw error;
} finally {
  await browser.close();
  await writeFile(join(output, "browser-regression.json"), JSON.stringify(report, null, 2) + "\n");
}
