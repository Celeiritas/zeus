// tests/root_fixtures/make_chromium_fixtures.mjs - make chromium-es256.json and chromium-rs256.json once, then commit them.
//
//   node tests/root_fixtures/make_chromium_fixtures.mjs <folder whose node_modules holds playwright>
//
// For each algorithm (ES256, RS256) a Chromium virtual authenticator (CDP WebAuthn: ctap2, internal, resident key,
// user verification, backup eligible and backed up, like a synced phone passkey) makes one key with
// navigator.credentials.create and signs one root-action challenge with navigator.credentials.get. The page is
// https://h.example:4443/ (the test config's origin), served by Playwright's request routing, so nothing listens on a
// port and nothing leaves the machine.
//
// The get challenge is computed in the page the way the key page computes it (contract §8b): SHA-256 of
// "zeus-root/v1\n" + the canonical JSON (keys sorted, no spaces, ASCII only) of {action, exp, list, nonce, origin,
// params}, with crypto.subtle. The fixture keeps the page's own result, so test_root_webauthn also proves that the
// page's canonical JSON equals Python's.
//
// No real key, login, device or address: the login, device names and address below are placeholders.
import { createRequire } from "node:module";
import { writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const from = process.argv[2];
if (!from) {
  console.error("usage: node tests/root_fixtures/make_chromium_fixtures.mjs <folder whose node_modules holds playwright>");
  process.exit(2);
}
const require = createRequire(join(from, "package.json"));
const { chromium } = require("playwright");
const playwrightVersion = require("playwright/package.json").version;

const HERE = dirname(fileURLToPath(import.meta.url));
const ORIGIN = "https://h.example:4443";
const RP_ID = "h.example";
// The list's hash is any 64 hex here: verify() takes the installed list's hash as an argument.
const LIST = "5f1a6c0e3b9d4a7782c1e0f9d3b6a5c4e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7";
const SEEN = { login: "owner@example.com", node: "phone", node_id: "nTEST1CNTRL", ip: "192.0.2.7" };
const ALGS = [["es256", -7], ["rs256", -257]];

// Runs inside the page: everything the key page will do, minus the gate's HTTP.
async function ceremony({ alg, list, action }) {
  const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const rand = (n) => crypto.getRandomValues(new Uint8Array(n));
  // Canonical JSON: keys sorted by UTF-16 code units (Python's sort for ASCII keys), no spaces, every character above
  // 0x7e written as a lowercase \uXXXX escape, the way Python's json.dumps(ensure_ascii=True) writes it.
  const canon = (x) => {
    if (x === null || typeof x === "boolean") return JSON.stringify(x);
    if (typeof x === "number") {
      if (!Number.isSafeInteger(x)) throw new Error("only safe integers");
      return String(x);
    }
    if (typeof x === "string") return JSON.stringify(x).replace(/[\u007f-￿]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
    if (Array.isArray(x)) return "[" + x.map(canon).join(",") + "]";
    return "{" + Object.keys(x).sort().map((k) => canon(k) + ":" + canon(x[k])).join(",") + "}";
  };

  const userId = rand(16);
  const made = await navigator.credentials.create({ publicKey: {
    rp: { id: location.hostname, name: "Zeus" },
    user: { id: userId, name: "owner", displayName: "owner" },
    challenge: rand(32),
    pubKeyCredParams: [{ type: "public-key", alg }],
    authenticatorSelection: { residentKey: "required", userVerification: "required" },
    attestation: "none",
    timeout: 60000,
  } });

  const fields = { action, params: {}, list, nonce: b64u(rand(32)), exp: Math.floor(Date.now() / 1000) + 120, origin: location.origin };
  const text = canon(fields);
  const challenge = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("zeus-root/v1\n" + text));
  const got = await navigator.credentials.get({ publicKey: {
    challenge, rpId: location.hostname, userVerification: "required", timeout: 60000,
    allowCredentials: [{ type: "public-key", id: made.rawId }],
  } });
  const r = got.response;
  return {
    registration: {
      credentialId: b64u(made.rawId), userId: b64u(userId), publicKey: b64u(made.response.getPublicKey()),
      alg: made.response.getPublicKeyAlgorithm(), authenticatorData: b64u(made.response.getAuthenticatorData()),
      clientDataJSON: b64u(made.response.clientDataJSON),
    },
    page: { canonical: text, challenge: b64u(challenge) },
    record: {
      type: "run", action, params: {}, list, nonce: fields.nonce, exp: fields.exp,
      credentialId: b64u(got.rawId), clientDataJSON: b64u(r.clientDataJSON), authenticatorData: b64u(r.authenticatorData),
      signature: b64u(r.signature), userHandle: r.userHandle ? b64u(r.userHandle) : null,
    },
  };
}

const browser = await chromium.launch({ args: ["--ignore-certificate-errors"] });
try {
  for (const [name, alg] of ALGS) {
    const context = await browser.newContext({ ignoreHTTPSErrors: true });
    const page = await context.newPage();
    await page.route(ORIGIN + "/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html><title>key page fixture</title>" }));
    await page.goto(ORIGIN + "/");
    const cdp = await context.newCDPSession(page);
    await cdp.send("WebAuthn.enable");
    await cdp.send("WebAuthn.addVirtualAuthenticator", { options: {
      protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true,
      automaticPresenceSimulation: true, defaultBackupEligibility: true, defaultBackupState: true,
    } });
    const out = await page.evaluate(ceremony, { alg, list: LIST, action: "close-media-lan" });
    out.record.seen = SEEN;
    const doc = {
      _source: "made by tests/root_fixtures/make_chromium_fixtures.mjs on " + new Date().toISOString().slice(0, 10) +
        " with Playwright " + playwrightVersion + " and " + browser.version() + " (CDP WebAuthn virtual authenticator: ctap2, internal, resident key, UV, backup eligible and backed up)",
      origin: ORIGIN, rp_id: RP_ID, alg, ...out,
    };
    writeFileSync(join(HERE, "chromium-" + name + ".json"), JSON.stringify(doc, null, 1) + "\n");
    console.log("chromium-" + name + ".json: alg " + out.registration.alg + ", credential " + out.registration.credentialId.length + " chars");
    await context.close();
  }
} finally {
  await browser.close();
}
