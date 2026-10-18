"use strict";

const RDT_CONNECT_RELEASES_API =
  "https://api.github.com/repos/RolanDorisTech/rdt-voice/releases?per_page=100";
const RDT_CONNECT_REPO_PATH = "/RolanDorisTech/rdt-voice";
const RDT_CONNECT_TAG_RE =
  /^rdt-connect-v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$/;

function selectLatestEligibleConnectDownload(releases) {
  if (!Array.isArray(releases)) return null;

  // Contract: choose the newest published_at timestamp among eligible Connect
  // releases, not the numerically highest semantic version.
  const ordered = releases
    .filter(r => r && r.draft === false &&
      typeof r.published_at === "string" &&
      Number.isFinite(Date.parse(r.published_at)) &&
      typeof r.tag_name === "string" && RDT_CONNECT_TAG_RE.test(r.tag_name))
    .sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at));

  for (const release of ordered) {
    const match = RDT_CONNECT_TAG_RE.exec(release.tag_name);
    if (!match) continue;
    const version = match[1];
    const expectedName = `RDT_Connect_${version}_aarch64.dmg`;
    const releaseAssets = Array.isArray(release.assets) ? release.assets : [];
    const matchingNames = releaseAssets.filter(a => a && a.name === expectedName);

    // Duplicate expected filenames are ambiguous even if only one duplicate
    // otherwise looks valid. Do not pick an arbitrary asset.
    if (matchingNames.length !== 1) continue;
    const asset = matchingNames[0];
    if (asset.state !== "uploaded" ||
        typeof asset.size !== "number" ||
        !Number.isSafeInteger(asset.size) || asset.size <= 0 ||
        typeof asset.digest !== "string" ||
        !/^sha256:[a-f0-9]{64}$/i.test(asset.digest) ||
        typeof asset.browser_download_url !== "string") continue;

    const expectedPath =
      `${RDT_CONNECT_REPO_PATH}/releases/download/${release.tag_name}/${expectedName}`;
    let url;
    try {
      url = new URL(asset.browser_download_url);
    } catch (_) {
      continue;
    }

    if (url.origin !== "https://github.com" ||
        url.username || url.password ||
        url.pathname !== expectedPath || url.search || url.hash) continue;

    return {
      tag: release.tag_name,
      version,
      name: expectedName,
      size: asset.size,
      digest: asset.digest,
      url: url.href
    };
  }
  return null;
}

function formatFileSize(bytes) {
  const mib = bytes / (1024 * 1024);
  return `${mib.toFixed(1)} MiB (${bytes.toLocaleString("en-US")} bytes)`;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { selectLatestEligibleConnectDownload, formatFileSize };
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  (() => {
    const status = document.getElementById("status");
    const downloadLink = document.getElementById("download-link");
    const releaseDetails = document.getElementById("release-details");
    const retryButton = document.getElementById("retry-button");
    const progress = document.getElementById("download-progress");
    const downloadHelp = document.getElementById("download-help");
    if (!status || !downloadLink || !releaseDetails || !retryButton || !progress || !downloadHelp) return;

    let activeController = null;
    let timerId = null;

    function showFailure(message) {
      status.textContent = message ||
        "We couldn't find a verified RDT Connect installer. Use the official release list below.";
      downloadLink.hidden = true;
      releaseDetails.hidden = true;
      progress.hidden = true;
      downloadHelp.hidden = true;
      retryButton.hidden = false;
      retryButton.disabled = false;
    }

    async function resolveDownload() {
      if (activeController) activeController.abort();
      if (timerId !== null) window.clearTimeout(timerId);

      const controller = new AbortController();
      activeController = controller;
      retryButton.disabled = true;
      retryButton.hidden = false;
      downloadLink.hidden = true;
      releaseDetails.hidden = true;
      progress.hidden = true;
      downloadHelp.hidden = true;
      status.textContent = "Checking GitHub for the newest eligible Apple-silicon installer…";
      timerId = window.setTimeout(() => controller.abort(), 10000);

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

        downloadLink.href = result.url;
        downloadLink.setAttribute("aria-label", `Download RDT Connect ${result.version} for macOS`);
        releaseDetails.textContent = `Version ${result.version} · ${formatFileSize(result.size)}`;
        downloadLink.hidden = false;
        releaseDetails.hidden = false;
        retryButton.hidden = true;
        status.textContent = "A verified RDT Connect installer is ready.";
      } catch (error) {
        showFailure("We couldn't resolve a verified RDT Connect installer automatically. You can retry or use the official release list below.");
        console.error("[RDT Connect download]", error);
      } finally {
        if (timerId !== null) window.clearTimeout(timerId);
        timerId = null;
        if (activeController === controller) activeController = null;
        if (!retryButton.hidden) retryButton.disabled = false;
      }
    }

    retryButton.addEventListener("click", () => { void resolveDownload(); });
    downloadLink.addEventListener("click", () => {
      // GitHub's direct asset download does not expose byte progress to this
      // page. Keep the claim indeterminate and point users to browser Downloads.
      status.textContent = "Starting the download. For actual transfer progress, check your browser’s Downloads panel.";
      downloadHelp.hidden = false;
      progress.hidden = false;
      progress.setAttribute("aria-valuetext", "Download handed off to your browser; exact transfer progress is shown in the browser Downloads panel.");
    });

    void resolveDownload();
  })();
}
