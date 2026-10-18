import assert from "node:assert/strict";
import worker from "../cloudflare/rdt-connect-download-proxy.mjs";

const originalFetch = globalThis.fetch;
const calls = [];
globalThis.fetch = async (url, options) => {
  calls.push({ url: String(url), options });
  if (options.method === "HEAD") {
    return new Response(null, { status: 200, headers: {
      "Content-Length": "8", "Accept-Ranges": "bytes"
    }});
  }
  return new Response("12345678", { status: 200, headers: {
    "Content-Length": "8", "Accept-Ranges": "bytes"
  }});
};

try {
  const origin = "https://rolandoristech.github.io";
  const tag = "rdt-connect-v0.2.0-dev.10";
  const url = `https://rdt-connect-download-proxy.rolandoristech.workers.dev/download/${tag}`;

  const response = await worker.fetch(new Request(url, {
    headers: { Origin: origin }
  }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), origin);
  assert.equal(response.headers.get("Content-Length"), "8");
  assert.match(response.headers.get("Content-Disposition"), /RDT_Connect_0\.2\.0-dev\.10_aarch64\.dmg/);
  assert.equal(await response.text(), "12345678");
  assert.equal(calls[0].url,
    "https://github.com/RolanDorisTech/rdt-voice/releases/download/rdt-connect-v0.2.0-dev.10/RDT_Connect_0.2.0-dev.10_aarch64.dmg");
  console.log("PASS: fixed tag streams the exact official release asset with CORS and length headers.");

  const head = await worker.fetch(new Request(url, {
    method: "HEAD", headers: { Origin: origin }
  }));
  assert.equal(head.status, 200);
  assert.equal(head.headers.get("Content-Length"), "8");
  console.log("PASS: HEAD exposes content length without returning the file body.");

  const callsBeforeReject = calls.length;
  const badOrigin = await worker.fetch(new Request(url, {
    headers: { Origin: "https://evil.example" }
  }));
  assert.equal(badOrigin.status, 403);
  assert.equal(calls.length, callsBeforeReject);
  console.log("PASS: untrusted origin rejected without an upstream fetch.");

  const badTag = await worker.fetch(new Request(
    "https://rdt-connect-download-proxy.rolandoristech.workers.dev/download/rdt-connect-v1.2"
  ));
  assert.equal(badTag.status, 400);
  assert.equal(calls.length, callsBeforeReject);
  console.log("PASS: invalid tag rejected; no arbitrary URL can be proxied.");

  const badPath = await worker.fetch(new Request(
    "https://rdt-connect-download-proxy.rolandoristech.workers.dev/download/https://evil.example/file"
  ));
  assert.equal(badPath.status, 404);
  console.log("PASS: arbitrary upstream URLs are not accepted.");
} finally {
  globalThis.fetch = originalFetch;
}
