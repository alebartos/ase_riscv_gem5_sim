// End-to-end test of ASE Studio in headless Chrome, against fake_portal.py.
//
// Local only: needs Google Chrome, puppeteer-core and a working build
// (toolchain + gem5), because it builds and simulates the "example" project.
//
//   npm install --prefix /tmp/ase-ui puppeteer-core@22.15.0
//   python3 utils/macOS/tests/fake_portal.py 18921 &
//   ASE_STUDIO_DEVELOPER=1 ASE_STUDIO_PORTAL_API=http://127.0.0.1:18921/api \
//     ASE_STUDIO_HOST_ROOT=$PWD XDG_CONFIG_HOME=/tmp/ase-ui/config \
//     python3 /path/to/ase-studio/backend.py 18922 &
//   UIDEPS=/tmp/ase-ui STUDIO_URL=http://127.0.0.1:18922 OUT_DIR=/tmp/ase-ui/out \
//     node utils/macOS/tests/ui_test.js
//
// It creates submissions/ui_test and submissions/ui_manual in the host root.
const path = require("path");
const fs = require("fs");
const puppeteer = require(path.join(process.env.UIDEPS, "node_modules/puppeteer-core"));

const BASE = process.env.STUDIO_URL;
const OUT = process.env.OUT_DIR;
const DOWNLOADS = path.join(OUT, "downloads");
fs.mkdirSync(DOWNLOADS, {recursive: true});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const step = message => console.log("• " + message);

(async () => {
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true, args: ["--no-first-run", "--window-size=1440,900"]
  });
  const page = await browser.newPage();
  await page.setViewport({width: 1440, height: 900});
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) errors.push(message.text());
  });
  page.on("response", response => {
    if (response.status() >= 400) console.log("  HTTP " + response.status() + " " + response.url().replace(BASE, ""));
  });
  const cdp = await page.createCDPSession();
  await cdp.send("Browser.setDownloadBehavior", {behavior: "allow", downloadPath: DOWNLOADS});
  const shot = name => page.screenshot({path: path.join(OUT, name + ".png")});
  const waitOpen = (selector, timeout = 30000) =>
    page.waitForFunction(s => document.querySelector(s).open, {timeout}, selector);
  const waitClosed = (selector, timeout = 30000) =>
    page.waitForFunction(s => !document.querySelector(s).open, {timeout}, selector);
  const waitLog = (text, timeout = 240000) =>
    page.waitForFunction(t => document.querySelector("#output").textContent.includes(t), {timeout}, text);

  await page.goto(BASE, {waitUntil: "networkidle0"});
  await page.click('.project[data-name="example"]');
  await page.waitForFunction(() => !document.querySelector("#submit").disabled);
  step("project opened");

  // Elaborati before sign-in opens the sign-in dialog.
  await page.click("#elaborati");
  await waitOpen("#portal-login-dialog");
  await page.type("#portal-username", "s123456");
  await page.type("#portal-password", "wrong");
  await page.click("#portal-login-confirm");
  await page.waitForFunction(() => !document.querySelector("#portal-login-message").hidden);
  const loginError = await page.$eval("#portal-login-message", element => element.textContent);
  if (!loginError.includes("Invalid credentials")) throw Error("login error not shown: " + loginError);
  step("wrong password shown");
  await page.$eval("#portal-password", input => { input.value = ""; });
  await page.type("#portal-password", "secret");
  await page.click("#portal-login-confirm");
  await waitClosed("#portal-login-dialog");
  await page.waitForFunction(() => document.querySelector("#elaborati-course").textContent.includes("02GOLYG"));
  await page.waitForFunction(() => document.querySelector("#elaborati-list").textContent.includes("No Elaborati"));
  step("Elaborati: ASE course, empty list");
  await shot("01-elaborati-empty");
  await page.click("#elaborati-close");

  async function submit(assignment, mode = "submit") {
    await page.waitForFunction(() => !document.querySelector("#submit").disabled, {timeout: 240000});
    await page.click("#submit");
    await waitOpen("#submission-dialog");
    await page.$eval("#submission-assignment", input => { input.value = ""; });
    await page.type("#submission-assignment", assignment);
    if (mode === "manual") {
      await page.click("#submission-manual");
      return;
    }
    if (!(await page.$eval("#portal-upload", input => input.checked))) await page.click("#portal-upload");
    await page.click("#submission-confirm");
  }

  await submit("ui_test");
  await waitOpen("#portal-review-dialog", 240000);
  const review = await page.evaluate(() => ({
    course: document.querySelector("#portal-review-course").textContent,
    file: document.querySelector("#portal-review-file").textContent,
    entries: document.querySelectorAll("#portal-review-entries .submission-file-entry").length,
    existingHidden: document.querySelector("#portal-review-existing").hidden
  }));
  console.log("  review:", JSON.stringify(review));
  if (!review.existingHidden || !review.entries) throw Error("unexpected review");
  await shot("02-review");
  await page.click("#portal-review-confirm");
  await waitLog("Uploaded to Elaborati");
  step("uploaded after review");

  await submit("ui_test");
  await waitOpen("#portal-review-dialog", 240000);
  if (await page.$eval("#portal-review-existing", element => element.hidden)) throw Error("duplicate not shown");
  await page.click('[name="portal-review-replace"][value="keep"]');
  await page.click("#portal-review-confirm");
  await waitLog("Uploaded to Elaborati");
  step("second copy kept");

  await page.click("#elaborati");
  await waitOpen("#elaborati-dialog");
  await page.waitForFunction(() => document.querySelectorAll(".elaborato").length === 2);
  await page.click(".elaborato button.danger");
  await waitOpen("#message-dialog");
  await page.click("#message-confirm");
  await page.waitForFunction(() => document.querySelectorAll(".elaborato.withdrawn").length === 1);
  step("withdrawn");
  await page.click(".elaborato.withdrawn button");
  await page.waitForFunction(() => document.querySelectorAll(".elaborato.withdrawn").length === 0);
  step("restored");
  await shot("03-elaborati");
  await page.click("#elaborati-close");

  await submit("ui_test");
  await waitOpen("#portal-review-dialog", 240000);
  await page.click("#portal-review-confirm");
  await waitLog("Withdrew 2 previous copy(ies).");
  step("replaced and withdrew 2 copies");

  await submit("ui_manual", "manual");
  await waitOpen("#message-dialog", 240000);
  await page.click("#message-cancel");
  for (let i = 0; i < 50 && !fs.readdirSync(DOWNLOADS).some(f => f.endsWith(".zip")); i++) await sleep(200);
  if (!fs.readdirSync(DOWNLOADS).some(name => name.endsWith("ui_manual.zip"))) throw Error("ZIP not downloaded");
  step("manual ZIP saved");

  // Pipeline after a real run: rows and cycle columns are painted.
  await page.evaluate(() => showTab("pipeline"));
  await sleep(300);
  const pipeline = await page.evaluate(() => ({
    rows: document.querySelectorAll(".pipeline-row").length,
    stages: document.querySelectorAll(".pipeline-row .cell[class*='stage-']").length
  }));
  console.log("  pipeline:", JSON.stringify(pipeline));
  if (!pipeline.rows || !pipeline.stages) throw Error("pipeline not painted");
  await shot("04-pipeline");
  step("pipeline painted");

  if (errors.length) throw Error("page errors: " + errors.join(" | "));
  console.log("UI OK");
  await browser.close();
})().catch(error => { console.error("FAILED:", error.message); process.exit(1); });
