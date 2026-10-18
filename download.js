"use strict";

const RDT_CONNECT_RELEASES_API =
  "https://api.github.com/repos/RolanDorisTech/rdt-voice/releases?per_page=100";
const RDT_CONNECT_REPO_PATH = "/RolanDorisTech/rdt-voice";
const RDT_CONNECT_TAG_RE =
  /^rdt-connect-v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$/;

function selectLatestEligibleConnectDownload(releases) {
  if (!Array.isArray(releases)) return null;

  const ordered = releases
    .filter(r => r && r.draft !== true && r.published_at &&
      Number.isFinite(Date.parse(r.published_at)) &&
      typeof r.tag_name === "string" && RDT_CONNECT_TAG_RE.test(r.tag_name))
    .sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at));

  for (const release of ordered) {
    const match = RDT_CONNECT_TAG_RE.exec(release.tag_name);
    if (!match) continue;

    const version = match[1];
    const expectedName = `RDT_Connect_${version}_aarch64.dmg`;
    const assets = (Array.isArray(release.assets) ? release.assets : [])
      .filter(a => a && a.name === expectedName &&
        a.state === "uploaded" &&
        Number.isSafeInteger(Number(a.size)) && Number(a.size) > 0 &&
        /^sha256:[a-f0-9]{64}$/i.test(String(a.digest || "")) &&
        typeof a.browser_download_url === "string");

    // Skip empty, ambiguous or incomplete releases and try the next eligible tag.
    if (assets.length !== 1) continue;

    const expectedPath =
      `${RDT_CONNECT_REPO_PATH}/releases/download/${release.tag_name}/${expectedName}`;
    let url;
    try {
      url = new URL(assets[0].browser_download_url);
    } catch (_) {
      continue;
    }

    // Prevent unexpected hosts, paths, filenames, or non-HTTPS redirects.
    if (url.protocol !== "https:" || url.hostname !== "github.com" ||
        url.pathname !== expectedPath || url.search || url.hash) continue;

    return {
      tag: release.tag_name,
      version,
      name: expectedName,
      size: Number(assets[0].size),
      digest: assets[0].digest,
      url: url.href
    };
  }
  return null;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { selectLatestEligibleConnectDownload };
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  (async () => {
    const status = document.getElementById("status");
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 7000);

    try {
      const response = await fetch(RDT_CONNECT_RELEASES_API, {
        method: "GET",
        headers: { Accept: "application/vnd.github+json" },
        cache: "no-store",
        signal: controller.signal
      });
      if (!response.ok) throw new Error(`GitHub releases API returned HTTP ${response.status}`);

      const result = selectLatestEligibleConnectDownload(await response.json());
      if (!result) throw new Error("No eligible published Connect DMG was found.");

      status.textContent = `Opening RDT Connect ${result.version} installer…`;
      window.location.replace(result.url);
    } catch (error) {
      status.textContent =
        "We couldn't resolve a verified RDT Connect download automatically. Use the release list link below.";
      console.error("[RDT Connect download]", error);
    } finally {
      window.clearTimeout(timeout);
    }
  })();
}
