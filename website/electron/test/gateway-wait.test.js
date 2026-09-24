const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const {
  DEFAULT_GATEWAY_WAIT_MS,
  WINDOWS_LOCAL_GATEWAY_WAIT_MS,
  gatewayWaitTimeoutMs,
  waitForGateway,
  describeGatewayFailure,
  tailLines,
  isPortInUse,
} = require("../gateway-wait");

// Synchronous fake clock + timer so poll loops resolve instantly and
// deterministically (no real waiting). setTimeoutFn advances the clock by the
// requested delay, so maxWaitMs is reached after a bounded number of polls.
function harness({
  checkBackend,
  getFailure = () => null,
  isWindowAlive = () => true,
  maxWaitMs = 30_000,
  pollIntervalMs = 500,
} = {}) {
  let t = 0;
  const statuses = [];
  const p = waitForGateway({
    checkBackend,
    getFailure,
    isWindowAlive,
    onStatus: (m) => statuses.push(m),
    now: () => t,
    setTimeoutFn: (fn, ms) => { t += ms; queueMicrotask(fn); },
    maxWaitMs,
    pollIntervalMs,
  });
  return { p, statuses };
}

// ── waitForGateway ──

test("waitForGateway resolves when the backend is healthy", async () => {
  const { p, statuses } = harness({ checkBackend: () => Promise.resolve() });
  await p;
  assert.ok(statuses.includes("Connected ✓"));
});

test("waitForGateway resolves after a few unhealthy polls", async () => {
  let n = 0;
  const { p } = harness({
    checkBackend: () => (++n < 3 ? Promise.reject(new Error("not yet")) : Promise.resolve()),
  });
  await p;
  assert.strictEqual(n, 3);
});

test("Windows local cold start stays on the splash past the ordinary deadline", async () => {
  let polls = 0;
  const maxWaitMs = gatewayWaitTimeoutMs({ platform: "win32", watchSpawn: true });
  const { p } = harness({
    // 110 failed 500ms polls model a 55-second packaged cold start.
    checkBackend: () => (++polls <= 110
      ? Promise.reject(new Error("not yet"))
      : Promise.resolve()),
    maxWaitMs,
  });

  await p;
  assert.strictEqual(polls, 111);
  assert.ok(maxWaitMs > DEFAULT_GATEWAY_WAIT_MS);
});

test("gateway wait policy extends only the primary Windows gateway", () => {
  assert.strictEqual(
    gatewayWaitTimeoutMs({ platform: "win32", watchSpawn: true }),
    WINDOWS_LOCAL_GATEWAY_WAIT_MS,
  );
  assert.strictEqual(
    gatewayWaitTimeoutMs({ platform: "win32", watchSpawn: false }),
    DEFAULT_GATEWAY_WAIT_MS,
  );
  assert.strictEqual(
    gatewayWaitTimeoutMs({ platform: "darwin", watchSpawn: true }),
    DEFAULT_GATEWAY_WAIT_MS,
  );
});

test("the supervisor extends the Windows deadline only for a gateway it spawned", () => {
  const supervisor = fs.readFileSync(
    path.join(__dirname, "..", "gateway-supervisor.js"),
    "utf8",
  );
  assert.match(
    supervisor,
    /watchSpawn: watchSpawn && gatewayOwnership === "spawned"/,
  );
});

test("waitForGateway fails fast when the spawned gateway exited (no health polling)", async () => {
  const failure = { code: 1, signal: null };
  let polls = 0;
  const { p } = harness({
    checkBackend: () => { polls++; return Promise.reject(new Error("dead port")); },
    getFailure: () => failure,
    maxWaitMs: 30_000,
  });
  await assert.rejects(p, (e) => {
    assert.strictEqual(e.kind, "failed");
    assert.deepStrictEqual(e.failure, failure);
    return true;
  });
  // The failure short-circuits before any health probe — we did NOT poll a dead
  // port toward the 30s timeout.
  assert.strictEqual(polls, 0);
});

test("waitForGateway checks the failure flag before the timeout", async () => {
  const { p } = harness({
    checkBackend: () => Promise.reject(new Error("no")),
    getFailure: () => ({ signal: "SIGKILL" }),
    maxWaitMs: -1, // already past the deadline; failure must still win
  });
  await assert.rejects(p, (e) => e.kind === "failed");
});

test("waitForGateway times out when never healthy and no failure", async () => {
  const { p } = harness({
    checkBackend: () => Promise.reject(new Error("no")),
    getFailure: () => null,
    maxWaitMs: 2000,
    pollIntervalMs: 500,
  });
  await assert.rejects(p, (e) => e.kind === "timeout");
});

test("waitForGateway aborts when the window is gone", async () => {
  const { p } = harness({
    checkBackend: () => Promise.resolve(),
    isWindowAlive: () => false,
  });
  await assert.rejects(p, (e) => e.kind === "window-closed");
});

// ── describeGatewayFailure ──

// An incomplete bundle is not a launch failure: the installer is still writing
// the backend. Prefixing "could not be launched" would open with failure
// vocabulary under a title that says the install is still finishing.
test("describeGatewayFailure: an incomplete bundle passes its message through bare", () => {
  const msg = "Kiro Crew's bundled Python runtime is still being installed — retry.";
  assert.strictEqual(describeGatewayFailure({ error: msg, incompleteBundle: true }), msg);
});

test("describeGatewayFailure: an ordinary spawn error keeps the launch-failure prefix", () => {
  assert.match(describeGatewayFailure({ error: "EACCES" }), /could not be launched/);
});

test("describeGatewayFailure: exit code", () => {
  assert.match(describeGatewayFailure({ code: 1, signal: null }), /code 1/);
});

test("describeGatewayFailure: the disabled case names the port and both ways out", () => {
  const s = describeGatewayFailure({ disabled: true, port: 5476 });
  assert.match(s, /5476/);
  assert.match(s, /set not to start one on this machine/);
  assert.match(s, /start one here/);
  // Must NOT send the user to Settings: that page is served by the gateway that
  // is not running, so the instruction would be unreachable exactly when shown.
  assert.doesNotMatch(s, /Settings/);
  // Nothing was launched, so wording that sends the user hunting a crash or a
  // launch log is wrong for this case.
  assert.doesNotMatch(s, /could not be launched|exited on launch|failed to start/);
});

// #6138: with the launch aimed at a configured remote crew, this text must name
// that target and must NOT offer to start a gateway here -- the spawn binds the
// crew's own port, so the offer the dialog hides cannot be promised in words.
test("describeGatewayFailure: a remote target is named, not the generic wording", () => {
  const s = describeGatewayFailure({ disabled: true, port: 7778, remoteHost: "a.example.com" });
  assert.match(s, /a\.example\.com/);
  assert.match(s, /7778/);
  assert.match(s, /set not to start a gateway on this machine/);
  assert.doesNotMatch(s, /start one here/);
  assert.doesNotMatch(s, /Settings/);
  assert.doesNotMatch(s, /could not be launched|exited on launch|failed to start/);
});

// The page that owns the choice is served by a gateway, so the dialog's button is
// the ordinary way back. The message names exactly one exit: the composer already
// knows which one it is rendering, so making the reader resolve a branch at the
// moment of failure hands them work the system had already done.
test("describeGatewayFailure: an offered button is the named exit", () => {
  const s = describeGatewayFailure({
    disabled: true, port: 7778, remoteHost: "a.example.com", canStartHere: true,
  });
  assert.match(s, /choose Start Local Gateway/);
  // The button restarts the app rather than binding this port, and a user who is
  // not told that reads the restart as a crash.
  assert.match(s, /restarts the app/);
  // "a port it can serve here" made a reader carry two senses of "port" and two
  // of "local" through one paragraph, the crew's own and this machine's.
  assert.match(s, /free port on this computer/);
  // The button writes a setting that persists, and a reader who cannot see how to
  // undo it reads the only offered exit as a one-way door. Settings is reachable
  // once a gateway serves, which is exactly the state the button produces, so the
  // way back belongs in the sentence that asks for the click.
  assert.match(s, /turn that setting back off in Settings/);
  // The reader clicked hesitantly, unsure whether their work lives on the other
  // machine and would vanish. The button starts a second, separate gateway, so
  // the sentence that asks for the click says what happens to the crew's sessions.
  assert.match(s, /sessions on a\.example\.com/);
  assert.match(s, /stay there/);
  // Retry is ambiguous once a dialog carries two failures, so the crew advice
  // names what retrying reaches.
  assert.match(s, /retry to reach a\.example\.com/);
  // Naming the manual route beside an offered button is what sent users through
  // it unnecessarily.
  assert.doesNotMatch(s, /KIROCREW_PORT/);
});

// Both remaining states withhold the button, and they must not share wording: a
// user who clicked it and watched the restart run is told something they know to
// be false if the message says restarting is unavailable.
test("describeGatewayFailure: the post-click states drop the clause the click made false", () => {
  // Using the button turns the setting on, so "set not to start a gateway on this
  // machine" is false afterwards. A paragraph asserting that AND "the setting
  // stays on" cannot be reconciled by a reader, and it lands exactly where they
  // need the exit. Both post-click states describe the launch instead.
  const base = { disabled: true, port: 7778, remoteHost: "a.example.com", canStartHere: false };
  for (const [label, extra] of [
    ["busy port", { canStartHere: true, localStartPortBusy: 5476 }],
    ["failed attempt", { localStartFailed: true }],
  ]) {
    const s = describeGatewayFailure({ ...base, ...extra });
    assert.match(s, /setting stays on/, label);
    assert.doesNotMatch(s, /set not to start a gateway/, label);
    assert.match(s, /this launch did not start one here/, label);
  }
  // Before any click the clause is true and stays.
  const offered = describeGatewayFailure({ ...base, canStartHere: true });
  assert.match(offered, /set not to start a gateway/);
  assert.doesNotMatch(offered, /this launch did not start one here/);
});

test("describeGatewayFailure: a lost restart outranks a port that was busy earlier", () => {
  // The record survives every attempt. A run that reached a restart and lost it
  // is the newer fact, so reporting the earlier busy port would send the user to
  // a button this failure has just withdrawn.
  const s = describeGatewayFailure({
    disabled: true, port: 7778, remoteHost: "a.example.com",
    canStartHere: false, localStartFailed: true, localStartPortBusy: 5476,
  });
  assert.match(s, /did not finish/);
  assert.doesNotMatch(s, /did not begin/);
  assert.doesNotMatch(s, /choose Start Local Gateway again/);
});

test("describeGatewayFailure: an occupied port is named, not reported as a failed restart", () => {
  const s = describeGatewayFailure({
    disabled: true, port: 7778, remoteHost: "a.example.com",
    canStartHere: true, localStartPortBusy: 5476,
  });
  // The port is the actionable fact, and it is not this app's port.
  assert.match(s, /port 5476/);
  assert.match(s, /did not begin/);
  // The title names the crew's port, so this one says which machine it is on:
  // two bare numbers in one dialog read as one.
  assert.match(s, /port 5476 on this computer/);
  // "the setting" is a guess at which setting; name it.
  assert.match(s, /"Run a local gateway" setting stays on/);
  // Same reason the failed attempt leads: the reopened dialog must not look
  // unchanged.
  assert.ok(s.startsWith("Starting a local gateway here did not begin"));
  // Nothing restarted, so the wording for a restart that ran must not appear.
  assert.doesNotMatch(s, /did not finish/);
  assert.doesNotMatch(s, /restarted app never served one/);
  // Freeing the port makes the same click work, so the button is still named.
  assert.match(s, /choose Start Local Gateway again/);
  // "Stop whatever is on port 5476" is an instruction only a reader who already
  // knows how to find a listener can carry out; for everyone else it is the one
  // exit this state names and it is unusable. So the likeliest occupant is named
  // -- an orphan from a terminal-started Kiro Crew, which is also what the
  // retired-flag path says produces it -- together with an action anyone can take.
  assert.match(s, /another Kiro Crew started from a terminal/);
  assert.match(s, /quit that one/);
  assert.doesNotMatch(s, /Stop whatever is on port/);
});

test("describeGatewayFailure: a failed local-start attempt is acknowledged, not denied", () => {
  const s = describeGatewayFailure({
    disabled: true, port: 7778, remoteHost: "a.example.com",
    canStartHere: false, localStartFailed: true,
  });
  assert.match(s, /did not finish/);
  assert.match(s, /"Run a local gateway" setting stays on/);
  // The remedy has to name the control that carries it out. "launching Kiro Crew
  // again" left a reader unsure whether Quit was the way to do that, or whether
  // the missing button was an accident.
  assert.match(s, /choose Start Local Gateway to try again/);
  assert.doesNotMatch(s, /choose Quit and then open Kiro Crew again/);
  // The acknowledgement has to LEAD. Every branch shares the crew advice, so a
  // reopened dialog that starts with it repeats three identical sentences and
  // reads as nothing having happened, which buries the one thing that changed.
  const acknowledged = s.indexOf("did not finish");
  const crewAdvice = s.indexOf("Nothing is answering at");
  assert.ok(acknowledged >= 0 && crewAdvice >= 0, "both parts present");
  assert.ok(
    acknowledged < crewAdvice,
    "the acknowledgement must precede the shared crew advice",
  );
  assert.ok(s.startsWith("Starting a local gateway here did not finish"));
  // Denying the attempt is the defect: the restart did happen.
  assert.doesNotMatch(s, /not available here/);
});

test("describeGatewayFailure: a withheld button leaves the explicit-port route", () => {
  const s = describeGatewayFailure({
    disabled: true, port: 7778, remoteHost: "a.example.com", canStartHere: false,
  });
  assert.match(s, /KIROCREW_PORT/);
  assert.match(s, /no remote host configured/);
  // A variable name alone is not an instruction: the reader has to be told where
  // to set it, or the only exit this state offers is one they cannot carry out.
  assert.match(s, /from a terminal/);
  assert.match(s, /environment variable/);
  // Offering a button this dialog is not rendering sends the user hunting for it.
  assert.doesNotMatch(s, /Start Local Gateway/);
  // This state never ran a restart, so it must not borrow the wording that
  // acknowledges one.
  assert.doesNotMatch(s, /did not finish/);
});

test("describeGatewayFailure: the remote-side instruction names the tunnel", () => {
  // "the connection that reaches it" is vague at the moment of failure.
  const s = describeGatewayFailure({ disabled: true, port: 7778, remoteHost: "a.example.com" });
  assert.match(s, /tunnel or port-forward/);
});

test("describeGatewayFailure: title and body agree on 'answering at'", () => {
  // The dialog title reads "nothing answering at host:port"; the body said
  // "for", which reads as two different facts about the same target.
  const s = describeGatewayFailure({ disabled: true, port: 7778, remoteHost: "a.example.com" });
  assert.match(s, /answering at a\.example\.com/);
  assert.doesNotMatch(s, /answering for/);
});

test("describeGatewayFailure: an empty remoteHost keeps the local-start wording", () => {
  // The field is absent on every pre-existing caller, and a cleared host entry
  // stores "", so neither may change which of the two texts is chosen.
  for (const failure of [
    { disabled: true, port: 5476 },
    { disabled: true, port: 5476, remoteHost: "" },
  ]) {
    assert.match(describeGatewayFailure(failure), /start one here/);
  }
});

// #6138: the crew binds its OWN port on its own machine (effectivePort =
// remotePort || tokenPort || PORT), so naming the local end would send the user
// to check a port nothing over there was ever expected to serve.
test("describeGatewayFailure: a distinct remote port is the one to go and check", () => {
  const s = describeGatewayFailure({
    disabled: true,
    port: 5477,
    remoteHost: "a.example.com",
    remotePort: "9000",
  });
  assert.match(s, /a\.example\.com:9000/);
  // The local end stays visible, because that is the port the tunnel must land on.
  assert.match(s, /local port 5477/);
  assert.doesNotMatch(s, /a\.example\.com:5477/);
  assert.doesNotMatch(s, /start one here/);
});

test("describeGatewayFailure: no remotePort falls back to the shared port form", () => {
  // The field is optional and a cleared entry stores "", so both must read as
  // "the crew is on this same port" rather than rendering an empty target.
  for (const remotePort of [undefined, ""]) {
    const s = describeGatewayFailure({
      disabled: true, port: 7778, remoteHost: "a.example.com", remotePort,
    });
    assert.match(s, /a\.example\.com on port 7778/);
    // The two-port target form, not the words in isolation: "local port" also
    // reads naturally in ordinary advice, and a guard that catches that instead
    // reports a collision with prose as a rendering bug.
    assert.doesNotMatch(s, /reached through local port/);
    assert.doesNotMatch(s, /:undefined|: ,|::/);
  }
});

test("describeGatewayFailure: disabled wins over a stale error field", () => {
  // waitForGateway hands over whatever record it was given; the deliberate
  // no-spawn reason must not be reported as a launch failure.
  const s = describeGatewayFailure({ disabled: true, port: 7000, error: "spawn ENOENT" });
  assert.match(s, /7000/);
  assert.doesNotMatch(s, /ENOENT/);
});

test("describeGatewayFailure: SIGKILL carries the Gatekeeper + xattr hint", () => {
  const s = describeGatewayFailure({ signal: "SIGKILL" });
  assert.match(s, /Gatekeeper/);
  assert.match(s, /xattr -cr/);
});

test("describeGatewayFailure: spawn error", () => {
  const s = describeGatewayFailure({ error: "spawn ENOENT" });
  assert.match(s, /could not be launched/);
  assert.match(s, /spawn ENOENT/);
});

test("describeGatewayFailure: other signal", () => {
  assert.match(describeGatewayFailure({ signal: "SIGSEGV" }), /signal SIGSEGV/);
});

test("describeGatewayFailure: null", () => {
  assert.match(describeGatewayFailure(null), /failed to start/);
});

// ── tailLines ──

test("tailLines returns the last n lines", () => {
  assert.strictEqual(tailLines("a\nb\nc\nd\ne", 2), "d\ne");
});

test("tailLines returns all lines when there are fewer than n", () => {
  assert.strictEqual(tailLines("x\ny", 10), "x\ny");
});

test("tailLines trims trailing blank lines", () => {
  assert.strictEqual(tailLines("a\nb\n\n\n", 2), "a\nb");
});

test("tailLines on empty/null input", () => {
  assert.strictEqual(tailLines("", 5), "");
  assert.strictEqual(tailLines(null, 5), "");
});

// ── isPortInUse ──

test("isPortInUse detects a port-bind failure", () => {
  assert.strictEqual(
    isPortInUse("17:57:53 ERROR kiro_crew.dashboard.server: Port 7788 already in use -- is another KiroCrew gateway running?"),
    true,
  );
  assert.strictEqual(isPortInUse("OSError: [Errno 48] Address already in use"), true);
  assert.strictEqual(isPortInUse("Error: listen EADDRINUSE: address already in use :::7788"), true);
});

test("isPortInUse is false for unrelated logs / empty input", () => {
  assert.strictEqual(isPortInUse("ModuleNotFoundError: No module named 'yaml'"), false);
  assert.strictEqual(isPortInUse("gateway child exited code=1 signal=null"), false);
  assert.strictEqual(isPortInUse(""), false);
  assert.strictEqual(isPortInUse(null), false);
});
