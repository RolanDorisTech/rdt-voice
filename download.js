"use strict";

const RDT_CONNECT_RELEASES_API =
  "https://api.github.com/repos/RolanDorisTech/rdt-voice/releases?per_page=100";
const RDT_CONNECT_REPO_PATH = "/RolanDorisTech/rdt-voice";
const RDT_CONNECT_DOWNLOAD_PROXY_ORIGIN = "https://rdt-connect-download-proxy.rolandoristech.workers.dev";
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
      url: url.href,
      proxyUrl: `${RDT_CONNECT_DOWNLOAD_PROXY_ORIGIN}/download/${release.tag_name}`
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
    const cancelButton = document.getElementById("cancel-download-button");
    const progress = document.getElementById("download-progress");
    const progressFill = document.getElementById("download-progress-fill");
    const downloadHelp = document.getElementById("download-help");
    if (!status || !downloadLink || !releaseDetails || !retryButton || !cancelButton || !progress || !progressFill || !downloadHelp) return;

    let activeController = null;
    let timerId = null;
    let activeDownloadController = null;
    let activeDownloadReader = null;
    let cancelRequested = false;

    function showFailure(message) {
      status.textContent = message ||
        "We couldn't find a eligible RDT Connect installer. Use the official release list below.";
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
        downloadLink.dataset.proxyUrl = result.proxyUrl;
        downloadLink.dataset.filename = result.name;
        downloadLink.dataset.size = String(result.size);
        downloadLink.dataset.forceNative = "false";
        downloadLink.textContent = "Download (.dmg)";
        downloadLink.setAttribute("aria-label", `Download RDT Connect ${result.version} for macOS`);
        releaseDetails.textContent = `Version ${result.version} · ${formatFileSize(result.size)}`;
        downloadLink.hidden = false;
        releaseDetails.hidden = false;
        retryButton.hidden = true;
        status.textContent = "An eligible RDT Connect installer is ready.";
      } catch (error) {
        showFailure("We couldn't resolve a eligible RDT Connect installer automatically. You can retry or use the official release list below.");
        console.error("[RDT Connect download]", error);
      } finally {
        if (timerId !== null) window.clearTimeout(timerId);
        timerId = null;
        if (activeController === controller) activeController = null;
        if (!retryButton.hidden) retryButton.disabled = false;
      }
    }

    retryButton.addEventListener("click", () => { void resolveDownload(); });
    function updateDownloadProgress(downloaded, total) {
      const percent = total > 0 ? Math.min(100, Math.floor(downloaded * 100 / total)) : 0;
      progressFill.style.width = `${percent}%`;
      progress.setAttribute("aria-valuenow", String(percent));
      progress.setAttribute("aria-valuetext", total > 0
        ? `${percent}% — ${formatFileSize(downloaded)} of ${formatFileSize(total)}`
        : `${formatFileSize(downloaded)} written`);
      status.textContent = total > 0
        ? `Downloading RDT Connect… ${percent}% (${formatFileSize(downloaded)} of ${formatFileSize(total)})`
        : `Downloading RDT Connect… ${formatFileSize(downloaded)} written`;
    }

    async function downloadWithProgress() {
      let writable = null;
      let reader = null;

      try {
        status.textContent = "Choose where to save the installer…";
        const fileHandle = await window.showSaveFilePicker({
          suggestedName: downloadLink.dataset.filename || "RDT_Connect.dmg",
          types: [{
            description: "RDT Connect macOS installer",
            accept: { "application/octet-stream": [".dmg"] }
          }]
        });

        const controller = new AbortController();
        activeDownloadController = controller;
        cancelRequested = false;
        cancelButton.hidden = false;
        cancelButton.disabled = false;
        cancelButton.textContent = "Cancel download";

        status.textContent = "Connecting to the download stream…";
        const response = await fetch(downloadLink.dataset.proxyUrl, {
          method: "GET",
          headers: { Accept: "application/octet-stream" },
          cache: "no-store",
          signal: controller.signal
        });
        if (!response.ok || !response.body) {
          throw new Error(`Download stream returned HTTP ${response.status}`);
        }

        const headerSize = Number(response.headers.get("Content-Length"));
        const metadataSize = Number(downloadLink.dataset.size);
        const total = Number.isSafeInteger(headerSize) && headerSize > 0
          ? headerSize
          : (Number.isSafeInteger(metadataSize) && metadataSize > 0 ? metadataSize : 0);

        writable = await fileHandle.createWritable();
        progressFill.style.width = "0%";
        progress.setAttribute("aria-valuenow", "0");
        progress.setAttribute("aria-valuetext", "Download starting");
        progress.hidden = false;
        downloadHelp.hidden = true;

        let downloaded = 0;
        reader = response.body.getReader();
        activeDownloadReader = reader;

        while (true) {
          const part = await reader.read();
          if (cancelRequested) {
            throw new DOMException("Download canceled", "AbortError");
          }
          if (part.done) break;

          await writable.write(part.value);
          if (cancelRequested) {
            throw new DOMException("Download canceled", "AbortError");
          }
          downloaded += part.value.byteLength;
          updateDownloadProgress(downloaded, total);
        }

        if (cancelRequested) {
          throw new DOMException("Download canceled", "AbortError");
        }

        await writable.close();
        writable = null;

        progressFill.style.width = "100%";
        progress.setAttribute("aria-valuenow", "100");
        progress.setAttribute("aria-valuetext",
          `Download complete — ${formatFileSize(downloaded)} written`);
        status.textContent = `Download complete — ${formatFileSize(downloaded)} saved.`;
        progress.hidden = false;
      } catch (error) {
        const wasCanceled = cancelRequested ||
          (error && error.name === "AbortError");

        if (reader) {
          try { await reader.cancel(); } catch {}
        }
        if (writable) {
          try { await writable.abort(); } catch {}
        }

        progress.hidden = true;
        progressFill.style.width = "0%";
        progress.setAttribute("aria-valuenow", "0");

        if (wasCanceled) {
          downloadHelp.hidden = true;
          status.textContent = "Download canceled. The incomplete file was discarded.";
        } else {
          console.error("[RDT Connect download progress]", error);
          downloadLink.dataset.forceNative = "true";
          downloadLink.textContent = "Download directly (browser progress)";
          downloadLink.hidden = false;
          downloadHelp.hidden = false;
          status.textContent =
            "In-page streaming is unavailable right now. Use the browser-managed download instead.";
        }
      } finally {
        activeDownloadController = null;
        activeDownloadReader = null;
        cancelRequested = false;
        cancelButton.hidden = true;
        cancelButton.disabled = false;
        cancelButton.textContent = "Cancel download";
      }
    }

    cancelButton.addEventListener("click", () => {
      if (!activeDownloadController) return;
      cancelRequested = true;
      cancelButton.disabled = true;
      cancelButton.textContent = "Canceling…";
      status.textContent = "Canceling download…";

      activeDownloadController.abort();
      if (activeDownloadReader) {
        void activeDownloadReader.cancel().catch(() => {});
      }
    });

    downloadLink.addEventListener("click", event => {
      if (typeof window.showSaveFilePicker !== "function" ||
          downloadLink.dataset.forceNative === "true") {
        status.textContent =
          "Starting the browser-managed download. Check your browser’s Downloads panel for progress.";
        downloadHelp.hidden = false;
        progress.hidden = true;
        return;
      }
      event.preventDefault();
      void downloadWithProgress();
    });

    void resolveDownload();
  })();
}
