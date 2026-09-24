"use strict";
//
// Pure, injectable gateway-readiness logic, extracted from main.js so it can be
// unit-tested without Electron (mirrors gateway-stop.js). main.js binds the
// real checkBackend / window / status / failure-flag to these helpers.
//
// The headline behavior is FAIL-FAST: a bundled gateway that EXITS during
// startup (e.g. ModuleNotFoundError, or SIGKILL from Gatekeeper) will never
// become healthy, so polling its port for the full timeout is dead time behind
// a misleading "Waiting for gateway…" message. As soon as the spawn watcher
// reports a terminal exit we reject immediately (~1s) and surface the cause +
// the launch log. The maxWaitMs timeout stays purely as the backstop for a
// gateway that is alive but slow to bind its port.

const DEFAULT_GATEWAY_WAIT_MS = 30_000;
// Importing a freshly installed bundled Python tree can exceed the ordinary
// connection deadline on Windows. Keep the splash responsive through that cold
// start without weakening the fail-fast child-exit path above.
const WINDOWS_LOCAL_GATEWAY_WAIT_MS = 120_000;

/**
 * Pick the readiness deadline for the connection being opened.
 *
 * Only the primary local gateway gets the extended Windows cold-start budget.
 * Connection tabs still fail on the ordinary deadline, and every spawned child
 * exit still wins immediately through `getFailure`.
 *
 * @param {{platform?: string, watchSpawn?: boolean}} [o]
 * @returns {number}
 */
function gatewayWaitTimeoutMs({ platform = process.platform, watchSpawn = false } = {}) {
  return platform === "win32" && watchSpawn
    ? WINDOWS_LOCAL_GATEWAY_WAIT_MS
    : DEFAULT_GATEWAY_WAIT_MS;
}

/**
 * Poll `checkBackend` until the gateway answers, the spawned child reports a
 * terminal failure, the window closes, or we hit maxWaitMs.
 *
 * @param {object}   o
 * @param {() => Promise<void>} o.checkBackend  resolve = healthy, reject = not yet
 * @param {() => (object|null)} [o.getFailure]  returns a truthy failure record
 *                                              ({code,signal} or {error}) once the
 *                                              spawned gateway has exited; null while
 *                                              it is still alive / never spawned
 * @param {() => boolean} [o.isWindowAlive]     false => abort (window closed)
 * @param {(msg: string) => void} [o.onStatus]  progress callback
 * @param {() => number} [o.now]                injectable clock (tests)
 * @param {(fn: Function, ms: number) => any} [o.setTimeoutFn]  injectable timer (tests)
 * @param {number} [o.maxWaitMs=30000]
 * @param {number} [o.pollIntervalMs=500]
 * @returns {Promise<void>} resolves when healthy; rejects with a tagged Error
 *   where `err.kind` is one of 'failed' | 'timeout' | 'window-closed', and for
 *   'failed' `err.failure` carries the exit record.
 */
function waitForGateway({
  checkBackend,
  getFailure = () => null,
  isWindowAlive = () => true,
  onStatus = () => {},
  now = Date.now,
  setTimeoutFn = setTimeout,
  maxWaitMs = DEFAULT_GATEWAY_WAIT_MS,
  pollIntervalMs = 500,
}) {
  const start = now();
  return new Promise((resolve, reject) => {
    const poll = () => {
      if (!isWindowAlive()) {
        const e = new Error("Window closed");
        e.kind = "window-closed";
        return reject(e);
      }
      // Check the spawned child's terminal state BEFORE the timeout: a process
      // that already exited is never coming back, so fail now rather than
      // waiting out the clock.
      const failure = getFailure();
      if (failure) {
        const e = new Error(describeGatewayFailure(failure));
        e.kind = "failed";
        e.failure = failure;
        return reject(e);
      }
      if (now() - start > maxWaitMs) {
        const e = new Error("Backend timeout");
        e.kind = "timeout";
        return reject(e);
      }
      onStatus(`Waiting for gateway… ${Math.round((now() - start) / 1000)}s`);
      checkBackend()
        .then(() => { onStatus("Connected ✓"); resolve(); })
        .catch(() => setTimeoutFn(poll, pollIntervalMs));
    };
    poll();
  });
}

/**
 * One-line, human-readable reason for a gateway start failure. The SIGKILL case
 * carries the Gatekeeper hint because an unsigned/quarantined nested executable
 * being killed on launch is the most common "works for me, not my friend" mode.
 *
 * @param {{code?: number|null, signal?: string|null, error?: string, disabled?: boolean, port?: number, remoteHost?: string, remotePort?: string, canStartHere?: boolean, localStartFailed?: boolean, localStartPortBusy?: number}|null} failure
 * @returns {string}
 */
function describeGatewayFailure(failure) {
  if (!failure) return "The gateway failed to start.";
  // Nothing was launched, so there is no exit code and no log to read: the port
  // was silent and this app is set not to start a gateway here. Naming both
  // halves matters because either one alone is a normal, working state.
  //
  // Deliberately does not send the user to Settings for the fix: the page
  // holding that switch is served by a gateway, which is the thing not running.
  // The error dialog carries a button instead. Settings is named only as the way
  // back afterwards, which is reachable precisely because a gateway is serving
  // by then. On a remote crew's port that button cannot start a gateway in
  // place, so it restarts the app and lets port selection pick a local port;
  // this message names the host to check first, because reaching the crew is
  // what the user asked for.
  if (failure.disabled) {
    if (failure.remoteHost) {
      // The crew binds its own port on its own machine; this app only holds the
      // local end of the link. Naming the local port here would send the user to
      // check a port nothing over there was ever expected to serve.
      const target = failure.remotePort
        ? `${failure.remoteHost}:${failure.remotePort}, reached through local port ${failure.port},`
        : `${failure.remoteHost} on port ${failure.port},`;
      // One exit per message, not a conditional the reader has to resolve. The
      // composer knows whether it is rendering the button and says so on the
      // record, so this names the route that is actually open. The two ways the
      // button closes get their own wording: one never offered the restart, the
      // other ran it and it did not finish, and a user who just watched that
      // restart must not be told restarting is unavailable.
      //
      // The clause about the setting has to follow it. Using the button turns the
      // setting on, so after that "set not to start a gateway" is false, and a
      // paragraph asserting both that and "the setting stays on" cannot be
      // reconciled by the reader. Both post-click states describe the launch
      // instead, which stays true either way.
      const startedHere = failure.localStartPortBusy || failure.localStartFailed
        ? "this launch did not start one here"
        : "Kiro Crew is set not to start a gateway on this machine";
      const reachTheCrew = `Nothing is answering at ${target} and ${startedHere}. `
        + `Start the gateway on `
        + `${failure.remoteHost}, or re-establish the tunnel or port-forward that `
        + `reaches it, then retry to reach ${failure.remoteHost} again. If that `
        + "address is wrong, choose Edit Remote Crew to correct it.";
      if (failure.localStartFailed) {
        // A reopened dialog whose opening sentences are identical reads as
        // nothing having happened, so the branch that exists to acknowledge the
        // attempt leads with it and the unchanged crew advice follows.
        //
        // Ahead of the busy-port branch deliberately: a record survives every
        // attempt, so a run that reached a restart and lost it is the newer fact
        // and must win over a port that was busy on an earlier click. The
        // supervisor clears the earlier field as well, and this ordering means a
        // path that forgets to still cannot describe a withdrawn button.
        return "Starting a local gateway here did not finish: the restarted app "
          + "never served one. The \"Run a local gateway\" setting stays on, so "
          + "choose Start Local Gateway to try again -- it will pick a free port. "
          + `${reachTheCrew}`;
      }
      if (failure.localStartPortBusy) {
        // Nothing was restarted: the port the restart would have used is already
        // served by something else, so this names that port instead of reporting
        // an attempt. It leads with what happened for the same reason the failed
        // attempt does. The button is still rendered, because freeing that port
        // is outside this app and makes the same click work.
        //
        // "on this computer" because the title names the crew's port and these two
        // numbers are easy to read as one.
        return `Starting a local gateway here did not begin: port `
          + `${failure.localStartPortBusy} on this computer is already served by `
          + "something else, so restarting would not have produced a gateway this "
          + `app can tell apart from it. That is usually another Kiro Crew `
          + `started from a terminal, so quit that one -- then choose Start Local `
          + `Gateway again. `
          + `The "Run a local gateway" setting stays on. ${reachTheCrew}`;
      }
      if (failure.canStartHere) {
        return `${reachTheCrew} To run one on this machine instead, choose Start `
          + "Local Gateway: it turns the setting back on and restarts the app so it "
          + `picks a free port on this computer. Your sessions on ${failure.remoteHost} `
          + "stay there; the local gateway is a separate one. Once it is running you "
          + "can turn that setting back off in Settings.";
      }
      return `${reachTheCrew} Restarting the app to pick a local port is not `
        + "available here, so to run one on this machine instead, quit Kiro Crew "
        + "and start it from a terminal with the environment variable "
        + "KIROCREW_PORT set to a port number that has no remote host configured.";
    }
    return `No gateway is answering on port ${failure.port}, and Kiro Crew is set `
      + "not to start one on this machine. Start the gateway you connect to (or "
      + "the connection that reaches it) and retry, or start one here. If your "
      + "crew runs on another machine, choose Add Remote Crew to name it.";
  }
  // An incomplete bundle is not a launch failure — the installer is still writing
  // the backend. That message already explains the state and names Retry, so pass
  // it through bare; prefixing "could not be launched" would open with failure
  // vocabulary under a title saying the install is still finishing.
  if (failure.incompleteBundle && failure.error) return failure.error;
  if (failure.error) return `The gateway could not be launched: ${failure.error}`;
  if (failure.signal === "SIGKILL") {
    return "The gateway was killed on launch (SIGKILL). On macOS this usually "
      + "means Gatekeeper blocked an unsigned or quarantined executable — try: "
      + "xattr -cr <path to KiroCrew.app>";
  }
  if (failure.signal) {
    return `The gateway exited on launch (signal ${failure.signal}).`;
  }
  return `The gateway exited on launch (code ${failure.code}). `
    + "The cause is in the launch log below.";
}

/**
 * Last `n` lines of `text`, with trailing blank lines trimmed — for inlining a
 * launch-log tail into an error dialog. Pure (no fs) so it is trivially tested.
 *
 * @param {string} text
 * @param {number} [n=20]
 * @returns {string}
 */
function tailLines(text, n = 20) {
  if (!text) return "";
  const lines = String(text).replace(/\s+$/, "").split("\n");
  return lines.slice(Math.max(0, lines.length - n)).join("\n");
}

/**
 * True if the log text indicates the gateway could not bind its port because
 * something already holds it (a wedged or other KiroCrew gateway). This is a
 * distinct, recoverable failure from a crash: a plain retry can't help while
 * the holder is still there, but force-stopping it can. Pure (no fs/network).
 *
 * @param {string} text  launch-log tail
 * @returns {boolean}
 */
function isPortInUse(text) {
  if (!text) return false;
  return /address already in use|already in use|EADDRINUSE/i.test(String(text));
}

module.exports = {
  DEFAULT_GATEWAY_WAIT_MS,
  WINDOWS_LOCAL_GATEWAY_WAIT_MS,
  gatewayWaitTimeoutMs,
  waitForGateway,
  describeGatewayFailure,
  tailLines,
  isPortInUse,
};
