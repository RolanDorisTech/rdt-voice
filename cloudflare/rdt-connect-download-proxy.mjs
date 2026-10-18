const ALLOWED_ORIGIN = "https://rolandoristech.github.io";
const TAG_RE =
  /^rdt-connect-v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$/;

function reply(body, status, origin, extraHeaders = {}) {
  const headers = new Headers(extraHeaders);
  headers.set("Cache-Control", "no-store");
  headers.set("X-Content-Type-Options", "nosniff");
  if (origin === ALLOWED_ORIGIN) {
    headers.set("Access-Control-Allow-Origin", ALLOWED_ORIGIN);
    headers.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
    headers.set("Access-Control-Allow-Headers", "Range");
    headers.set("Access-Control-Expose-Headers",
      "Content-Length, Content-Disposition, Content-Range, Accept-Ranges");
    headers.append("Vary", "Origin");
  }
  return new Response(body, { status, headers });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin");

    if (origin && origin !== ALLOWED_ORIGIN) {
      return reply("Origin not allowed.", 403, null);
    }

    if (url.pathname === "/health" && request.method === "GET") {
      return reply(JSON.stringify({ service: "rdt-connect-download-proxy", ok: true }),
        200, origin, { "Content-Type": "application/json; charset=utf-8" });
    }

    if (!["GET", "HEAD", "OPTIONS"].includes(request.method)) {
      return reply("Method not allowed.", 405, origin, { Allow: "GET, HEAD, OPTIONS" });
    }

    const match = /^\/download\/(rdt-connect-v[^/]+)$/.exec(url.pathname);
    if (!match || url.search || url.hash) {
      return reply("Not found.", 404, origin, { "Content-Type": "text/plain; charset=utf-8" });
    }

    const tag = match[1];
    const versionMatch = TAG_RE.exec(tag);
    if (!versionMatch) {
      return reply("Invalid release tag.", 400, origin);
    }

    if (request.method === "OPTIONS") {
      return reply(null, 204, origin);
    }

    const version = versionMatch[1];
    const filename = `RDT_Connect_${version}_aarch64.dmg`;
    const upstreamUrl =
      `https://github.com/RolanDorisTech/rdt-voice/releases/download/${tag}/${filename}`;

    const upstreamHeaders = new Headers();
    const range = request.headers.get("Range");
    if (range) {
      if (!/^bytes=\d*-\d*$/.test(range) || range === "bytes=-") {
        return reply("Invalid byte range.", 416, origin);
      }
      upstreamHeaders.set("Range", range);
    }

    let upstream;
    try {
      upstream = await fetch(upstreamUrl, {
        method: request.method,
        headers: upstreamHeaders,
        redirect: "follow"
      });
    } catch {
      return reply("Release asset could not be reached.", 502, origin);
    }

    if (![200, 206].includes(upstream.status)) {
      return reply("Release asset unavailable.", upstream.status === 404 ? 404 : 502, origin);
    }

    const headers = new Headers({
      "Content-Type": "application/octet-stream",
      "Content-Disposition": `attachment; filename="${filename}"`,
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff"
    });

    for (const name of ["Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"]) {
      const value = upstream.headers.get(name);
      if (value) headers.set(name, value);
    }

    if (origin === ALLOWED_ORIGIN) {
      headers.set("Access-Control-Allow-Origin", ALLOWED_ORIGIN);
      headers.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
      headers.set("Access-Control-Allow-Headers", "Range");
      headers.set("Access-Control-Expose-Headers",
        "Content-Length, Content-Disposition, Content-Range, Accept-Ranges");
      headers.append("Vary", "Origin");
    }

    return new Response(request.method === "HEAD" ? null : upstream.body, {
      status: upstream.status,
      headers
    });
  }
};
