const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  isSelectablePort,
  legacyMigrationPort,
  migrateRemoteHostConfig,
  remoteHostPort,
  getRemoteHostConfig,
  selectLaunchPort,
  setRemoteHostConfig,
} = require("../host-config");

// Minimal mock of electron-store (get/set/delete on a plain object)
function mockStore(initial = {}) {
  const data = { ...initial };
  return {
    get: (k) => data[k],
    set: (k, v) => { data[k] = v; },
    delete: (k) => { delete data[k]; },
    _data: data,
  };
}

describe("migrateRemoteHostConfig", () => {
  it("migrates legacy remoteHost to remoteHosts[port]", () => {
    const store = mockStore({ remoteHost: "myhost.corp.example.com", kirocrewBinPath: "~/.local/bin/kirocrew", remoteHosts: {} });
    const result = migrateRemoteHostConfig(store, 7778);
    assert.equal(result, true);
    assert.deepEqual(store._data.remoteHosts, { 7778: { host: "myhost.corp.example.com", binPath: "~/.local/bin/kirocrew" } });
    assert.equal(store._data.remoteHost, undefined);
    assert.equal(store._data.kirocrewBinPath, undefined);
  });



  it("uses DEFAULT_REMOTE_BIN when kirocrewBinPath is empty", () => {
    const store = mockStore({ remoteHost: "host.com", kirocrewBinPath: "", remoteHosts: {} });
    migrateRemoteHostConfig(store, 7777);
    assert.equal(store._data.remoteHosts[7777].binPath, "~/.local/bin/kirocrew");
  });

  it("does not migrate when remoteHosts already has entries", () => {
    const store = mockStore({ remoteHost: "old.com", remoteHosts: { 7777: { host: "existing.com" } } });
    const result = migrateRemoteHostConfig(store, 7777);
    assert.equal(result, false);
    assert.equal(store._data.remoteHost, "old.com"); // not deleted
  });

  it("does not migrate when remoteHost is empty", () => {
    const store = mockStore({ remoteHost: "", remoteHosts: {} });
    const result = migrateRemoteHostConfig(store, 7777);
    assert.equal(result, false);
  });
});

describe("legacyMigrationPort", () => {
  // A legacy entry names a host and no port, so this key has to be the port the
  // launch will actually bind. Keying it anywhere else leaves the launch running
  // against a port whose remote host is recorded somewhere else.
  it("takes an explicit KIROCREW_PORT over the configured port", () => {
    // The override short-circuits selection, so that value is bound whether or
    // not this module would have picked it.
    const store = mockStore({ remoteHosts: {} });
    assert.equal(legacyMigrationPort({ store, envPort: 9100, configuredPort: 7778 }), 9100);
  });

  it("refuses to key a legacy crew under an unselectable override", () => {
    // remoteHosts["80"] is the entry whose missed lookup makes a tunnelled crew
    // read as local, because the shell's own URL drops the :80. The launch cannot
    // bind 80 either, so keying the crew there would also break this helper's one
    // invariant: the key is the port the launch targets.
    const store = mockStore({ remoteHosts: {} });
    assert.notEqual(legacyMigrationPort({ store, envPort: 80, configuredPort: 7778 }), 80);
    assert.equal(legacyMigrationPort({ store, envPort: 80, configuredPort: 7778 }), 7778);
  });

  it("falls back to the configured port when no override is set", () => {
    const store = mockStore({ remoteHosts: {} });
    assert.equal(legacyMigrationPort({ store, envPort: 0, configuredPort: 7778 }), 7778);
  });

  it("refuses an unselectable configured port and uses the fallback", () => {
    const store = mockStore({ remoteHosts: {} });
    assert.equal(legacyMigrationPort({ store, envPort: 0, configuredPort: 80 }), 5476);
    assert.equal(legacyMigrationPort({ store, envPort: 0, configuredPort: null }), 5476);
  });

  it("agrees with selection whenever selection is what decides the port", () => {
    // With no override, the launch target is selection's answer, and a legacy
    // entry implies an empty crew store -- so the two must match.
    for (const configured of [7778, 5476, 9999, 80]) {
      const store = mockStore({ remoteHosts: {} });
      const selected = selectLaunchPort({
        store: mockStore({ remoteHosts: {} }),
        configuredPort: configured,
        localGatewayEnabled: true,
      });
      assert.equal(
        legacyMigrationPort({ store, envPort: 0, configuredPort: configured }),
        selected,
        String(configured),
      );
    }
  });
});

describe("getRemoteHostConfig", () => {
  it("returns config for a known port", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "a.com", binPath: "/bin/m" } } });
    assert.deepEqual(getRemoteHostConfig(store, 7778), { host: "a.com", binPath: "/bin/m" });
  });

  it("returns null for unknown port", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "a.com" } } });
    assert.equal(getRemoteHostConfig(store, 9999), null);
  });

  it("coerces numeric port to string for lookup", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "a.com" } } });
    assert.ok(getRemoteHostConfig(store, 7778));
  });
});

describe("setRemoteHostConfig", () => {
  it("sets config for a new port", () => {
    const store = mockStore({ remoteHosts: {} });
    setRemoteHostConfig(store, 7778, { host: "new.com", binPath: "~/bin/m" });
    assert.equal(store._data.remoteHosts["7778"].host, "new.com");
    assert.equal(store._data.remoteHosts["7778"].binPath, "~/bin/m");
  });

  it("preserves defaultName when clearing host", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "old.com", binPath: "/b", defaultName: "Cloud" } } });
    setRemoteHostConfig(store, 7778, { host: "" });
    assert.deepEqual(store._data.remoteHosts["7778"], { defaultName: "Cloud" });
  });

  it("deletes port entry entirely when clearing with no defaultName", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "old.com", binPath: "/b" } } });
    setRemoteHostConfig(store, 7778, { host: "" });
    assert.equal(store._data.remoteHosts["7778"], undefined);
  });

  it("preserves existing fields (like defaultName) when setting host", () => {
    const store = mockStore({ remoteHosts: { "7778": { defaultName: "Cloud" } } });
    setRemoteHostConfig(store, 7778, { host: "x.com", binPath: "/b" });
    assert.equal(store._data.remoteHosts["7778"].host, "x.com");
    assert.equal(store._data.remoteHosts["7778"].defaultName, "Cloud");
  });

  it("defaults binPath to DEFAULT_REMOTE_BIN when omitted", () => {
    const store = mockStore({ remoteHosts: {} });
    setRemoteHostConfig(store, 7777, { host: "h.com" });
    assert.equal(store._data.remoteHosts["7777"].binPath, "~/.local/bin/kirocrew");
  });
});

// #6138: with "Run a local gateway" off, the launch has to aim at the remote
// crew the user configured instead of the local default nothing will bind.
describe("remoteHostPort", () => {
  it("returns null when nothing is configured", () => {
    assert.equal(remoteHostPort(mockStore()), null);
    assert.equal(remoteHostPort(mockStore({ remoteHosts: {} })), null);
  });

  it("returns the port of the only configured remote host", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("picks the lowest port, whatever order the keys were written in", () => {
    const store = mockStore({
      remoteHosts: {
        "9001": { host: "c.example.com" },
        "5477": { host: "a.example.com" },
        "7778": { host: "b.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 5477);
  });

  it("skips entries that carry only a window name", () => {
    const store = mockStore({
      remoteHosts: {
        "5477": { defaultName: "Laptop" },
        "7778": { host: "a.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("skips an entry whose host was cleared", () => {
    const store = mockStore({
      remoteHosts: {
        "5477": { host: "", binPath: "~/.local/bin/kirocrew" },
        "7778": { host: "a.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("skips keys that are not usable port numbers", () => {
    const store = mockStore({
      remoteHosts: {
        "0": { host: "a.example.com" },
        "70000": { host: "b.example.com" },
        "not-a-port": { host: "c.example.com" },
        "7778": { host: "d.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("skips a key that only STARTS with digits", () => {
    // parseInt would read "5477-old" as 5477 and dial a port whose own entry
    // does not exist, so the launch would carry no host for that port.
    const store = mockStore({
      remoteHosts: {
        "5477-old": { host: "a.example.com" },
        "7778": { host: "b.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("skips non-canonical spellings of a port number", () => {
    for (const key of ["05477", " 5477", "5477 ", "+5477", "5477.0", "0x1565"]) {
      const store = mockStore({ remoteHosts: { [key]: { host: "a.example.com" } } });
      assert.equal(remoteHostPort(store), null, key);
    }
  });

  it("returns null when every entry is unusable", () => {
    const store = mockStore({
      remoteHosts: { "5477": { defaultName: "Laptop" }, "70000": { host: "a.example.com" } },
    });
    assert.equal(remoteHostPort(store), null);
  });

  it("tolerates a malformed entry instead of throwing", () => {
    const store = mockStore({ remoteHosts: { "5477": null, "7778": { host: "a.example.com" } } });
    assert.equal(remoteHostPort(store), 7778);
  });

  // Security: `new URL("http://localhost:80").port` is "", so a target of 80
  // defeats every per-port lookup keyed off that URL -- including the
  // host-presence classifier, which would then read a tunnelled crew as local
  // and send this machine's internal secret over the tunnel.
  it("never selects port 80, even as the only configured crew", () => {
    const store = mockStore({ remoteHosts: { "80": { host: "a.example.com" } } });
    assert.equal(remoteHostPort(store), null);
  });

  it("skips port 80 and takes the next selectable crew", () => {
    const store = mockStore({
      remoteHosts: {
        "80": { host: "a.example.com" },
        "7778": { host: "b.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });
});

describe("isSelectablePort", () => {
  it("refuses port 80 and accepts its neighbours", () => {
    assert.equal(isSelectablePort(80), false);
    assert.equal(isSelectablePort(79), true);
    assert.equal(isSelectablePort(81), true);
  });

  it("refuses anything that is not a port number in range", () => {
    for (const value of [0, -1, 65536, 1.5, NaN, null, undefined, "5476"]) {
      assert.equal(isSelectablePort(value), false, String(value));
    }
  });

  it("accepts the ordinary gateway ports", () => {
    for (const value of [1, 443, 5476, 7778, 65535]) {
      assert.equal(isSelectablePort(value), true, String(value));
    }
  });
});

describe("selectLaunchPort", () => {
  function select(store, { configuredPort = null, localGatewayEnabled = true } = {}) {
    const logged = [];
    const port = selectLaunchPort({
      store,
      configuredPort,
      localGatewayEnabled,
      log: (message) => logged.push(message),
    });
    return { port, logged: logged.join("\n") };
  }

  describe("with the local gateway off", () => {
    it("honours a dashboard record that names a configured crew", () => {
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: 7778, localGatewayEnabled: false }).port, 7778);
    });

    it("ignores a dashboard record no host can serve and takes the crew", () => {
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: 5476, localGatewayEnabled: false }).port, 7778);
    });

    it("takes the configured crew when there is no dashboard record", () => {
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: null, localGatewayEnabled: false }).port, 7778);
    });

    it("keeps the dashboard record when no crew is configured at all", () => {
      const store = mockStore({ remoteHosts: {} });
      assert.equal(select(store, { configuredPort: 9999, localGatewayEnabled: false }).port, 9999);
    });

    it("refuses a record on port 80 whose only crew sits there too", () => {
      // The crew scan skips 80, so no remote target is found and the record
      // reaches the shared exit. Honouring it would hand the launch the one port
      // whose URL drops it, which is what makes a remote link read as local.
      const store = mockStore({ remoteHosts: { "80": { host: "a.example.com" } } });
      assert.equal(
        select(store, { configuredPort: 80, localGatewayEnabled: false }).port,
        5476,
      );
    });

    it("falls back to the default with neither a record nor a crew", () => {
      const store = mockStore({ remoteHosts: {} });
      assert.equal(select(store, { configuredPort: null, localGatewayEnabled: false }).port, 5476);
    });
  });

  describe("with the local gateway on", () => {
    it("declines a dashboard record whose port has a configured crew", () => {
      // This launch stands a gateway up on the port it picks. Binding a crew's
      // port shadows that crew, and the supervisor's conflict resolver reads the
      // same entry, so it calls the gateway this app started foreign.
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      const { port, logged } = select(store, { configuredPort: 7778 });
      assert.equal(port, 5476);
      assert.match(logged, /7778/);
      assert.match(logged, /remote crew is configured there/);
    });

    it("declines it on a port it would not select either", () => {
      const store = mockStore({ remoteHosts: { "80": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: 80 }).port, 5476);
    });

    it("keeps a dashboard record whose own port has no crew", () => {
      // A crew configured somewhere else is not a reason to move a local launch.
      const store = mockStore({ remoteHosts: { "9999": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: 7778 }).port, 7778);
    });

    it("keeps a record whose entry is only a window-title setting", () => {
      const store = mockStore({ remoteHosts: { "7778": { defaultName: "Staging" } } });
      assert.equal(select(store, { configuredPort: 7778 }).port, 7778);
    });

    it("falls back to the default when a crew is configured and no record exists", () => {
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: null }).port, 5476);
    });

    it("keeps a record whose entry carries an empty or non-string host", () => {
      // Neither shape names a machine to reach, so neither is a crew to shadow.
      for (const entry of [{ host: "" }, { host: 5 }, {}]) {
        const store = mockStore({ remoteHosts: { "7778": entry } });
        assert.equal(select(store, { configuredPort: 7778 }).port, 7778);
      }
    });

    // The product default is also the likeliest local end of a tunnel, so
    // declining a crew-named record and then handing back 5476 would return a
    // crew's port for the very reason the record was refused.
    it("declines a crew-named record without falling back onto another crew", () => {
      const store = mockStore({
        remoteHosts: {
          "7778": { host: "a.example.com" },
          "5476": { host: "b.example.com" },
        },
      });
      const { port, logged } = select(store, { configuredPort: 7778 });
      assert.notEqual(port, 7778);
      assert.notEqual(port, 5476);
      assert.equal(port, 5477);
      assert.match(logged, /remote crew is configured on 5476/);
    });

    it("skips a run of crew-claimed ports to reach a free one", () => {
      const remoteHosts = {};
      for (let p = 5476; p <= 5479; p += 1) remoteHosts[String(p)] = { host: "c.example.com" };
      const store = mockStore({ remoteHosts });
      assert.equal(select(store, { configuredPort: null }).port, 5480);
    });

    it("keeps the default and leaves the collision visible when the window is full", () => {
      // Sixty-four consecutive crews is a configuration to report, not one to
      // out-guess: a defined answer beats one that depends on the search width.
      const remoteHosts = {};
      for (let p = 5476; p < 5476 + 64; p += 1) remoteHosts[String(p)] = { host: "d.example.com" };
      const store = mockStore({ remoteHosts });
      const { port, logged } = select(store, { configuredPort: null });
      assert.equal(port, 5476);
      assert.match(logged, /names a configured crew/);
    });

    // A dashboard record on a scheme's default port is refused whether or not a
    // crew is configured there. Every per-port lookup keyed off the window URL
    // reads "" for 80, so a launch that lands there cannot be told apart from a
    // link to a remote crew, and the heartbeat sends the internal secret on that
    // answer. Which port is selected is the part this module controls.
    it("refuses a crew-free dashboard record on port 80", () => {
      const store = mockStore({ remoteHosts: {} });
      const { port, logged } = select(store, { configuredPort: 80 });
      assert.equal(port, 5476);
      assert.match(logged, /No usable dashboard\.url port/);
      assert.match(logged, /falling back to 5476/);
    });
  });
});
