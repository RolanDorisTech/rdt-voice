import { invoke, isTauri } from "@tauri-apps/api/core";
import { emit, listen as listenRaw } from "@tauri-apps/api/event";
function listenNative<T>(
  ...args: Parameters<typeof listenRaw<T>>
): ReturnType<typeof listenRaw<T>> {
  if (!isTauri()) return Promise.resolve(() => {});
  return listenRaw<T>(...args);
}

import { check as checkForTauriUpdate, type Update as TauriUpdate, type DownloadEvent as TauriDownloadEvent } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import "./style.css";
import { initializeLocalization, getActiveLocale, getLocaleChoices, setActiveLocale, translateKey } from "./i18n";

// Load the saved/system locale before initializing the rest of the application.
// Async bootstrap preserves the configured ES2020 browser target.
async function bootstrapRdtConnect(): Promise<void> {
await initializeLocalization();

function formatHardwareDeviceText(value: string): string {
  return value
    .replace(/\bmlx-metal-gpu\s*\(\s*Device\s*\(\s*gpu\s*,\s*\d+\s*\)\s*\)/gi, appleGpuDisplayLabel)
    .replace(/\bmlx-metal-gpu\b/gi, appleGpuDisplayLabel)
    .replace(/\bDevice\s*\(\s*gpu\s*,\s*\d+\s*\)/gi, appleGpuDisplayLabel);
}

function formatLifetimeHardwareDeviceLabel(value: string): string {
  const attribution = value.trim();
  if (
    /\bmlx-cpu-(?:selected|fallback)\b/i.test(attribution) ||
    /\bDevice\s*\(\s*cpu\s*,\s*\d+\s*\)/i.test(attribution)
  ) {
    const model = appleGpuDisplayLabel.match(
      /^(Apple M\d+(?: (?:Pro|Max|Ultra))*) GPU · Metal$/i,
    );
    const cpuLabel = translateKey("msg.cpu.1d22");
      return model ? `${model[1]} ${cpuLabel}` : cpuLabel;
  }
  return formatHardwareDeviceText(attribution);
}


const appleGpuDisplayLabelPromise: Promise<void> =
  invoke<string>("get_apple_gpu_display_label").then((label) => {
    const value = label.trim().replace(/\s+/g, " ");
    if (/^Apple M\d+(?: (?:Pro|Max|Ultra))* GPU · Metal$/.test(value)) {
      appleGpuDisplayLabel = value;
      window.dispatchEvent(new Event("rdt-apple-gpu-display-label-ready"));
    }
  }).catch(() => {});


let appleGpuDisplayLabel = "Apple GPU · Metal";


type BackendStatus = {
  running: boolean;
  pid: number | null;
  port: number | null;
  model: string | null;
  localUrl: string | null;
  logPath: string;
};

let backend: BackendStatus = { running: false, pid: null, port: null, model: null, localUrl: null, logPath: "" };

type OrphanBackend = { pid: number; port: number | null; description: string };
type OrphanScan = { managerLockedElsewhere: boolean; instances: OrphanBackend[] };
type InferenceProviderStatus = { provider?: string; device?: string; accelerated?: boolean; requested_device?: string; fallback_reason?: string | null };
type BackendConnectionCheck = { running: boolean; pid: number | null; port: number | null; localUrl: string | null; healthOk: boolean; authOk: boolean | null; authHttpStatus: number | null; message: string; inferenceProvider: InferenceProviderStatus | null };
type TailscaleStatus = {
  available?: boolean;
  ready?: boolean;
  dns_name?: string;
  port?: number;
  public_url?: string;
  message?: string;
};
type PairRequest = {
  id: string;
  device_label: string;
  user_agent: string;
  created_at: number;
  expires_at: number;
};
type PairedDevice = {
  id: string;
  device_label: string;
  user_agent: string;
  created_at: number;
  last_seen: number;
  idle_expires_at: number;
  absolute_expires_at: number;
};
type PairingQr = { pairingUrl: string; qrDataUrl: string; expiresAt: number };
type BrowserOption = { id: string; name: string };
type LocalHttpsStatus = { mode: string; configured: boolean; trusted: boolean; generated: boolean; certificatePath: string | null; privateKeyPath: string | null; caCertificatePath: string | null; rdtAddedTrustCount: number; message: string };
type ModelStorageStatus = { model: string; locations: string[]; locationsAvailability: string[]; activeRoot: string; selectedRoot: string; modelCached: boolean; serverRunning: boolean };
type ModelDownloadStatus = { active: boolean; model: string | null; destinationRoot: string | null; phase: string; message: string; progressPercent: number; downloadedBytes: number; totalBytes: number | null; plannedFiles: number | null; sizeIsEstimate: boolean };
type ModelCacheDeleteResult = { deletedRepositories: number; locationsChecked: number; approximateBytesFreed: number; message: string };
type AppBuildInfo = { version: string; buildId: string };
type LifetimeStatisticsUsage = {
  transcriptions: number;
  audioSeconds?: number | null;
  uploadBytes: number;
  inferenceMilliseconds: number;
};
type LifetimeStatistics = {
  schema: string;
  memberSinceAtMillis: number | null;
  firstTranscriptionAt: string | null;
  lastTranscriptionAt: string | null;
  totalTranscriptions: number;
  totalAudioSeconds: number;
  totalUploadBytes: number;
  totalInferenceMilliseconds: number;
  hardwareUsage: Record<string, LifetimeStatisticsUsage>;
  modelUsage: Record<string, LifetimeStatisticsUsage>;
};

const $ = <T extends HTMLElement>(id: string) => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`Missing UI element: ${id}`);
  return el as T;
};

function formatLifetimeBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit++;
  }
  return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}






function formatLifetimeDuration(valueSeconds: number): string {
  const total = Number.isFinite(valueSeconds) ? Math.max(0, Math.round(valueSeconds)) : 0;
  const YEAR_SECONDS = 31_556_952;
  const MONTH_SECONDS = 2_629_746;
  const DAY_SECONDS = 86_400;
  const pad = (value: number) => String(value).padStart(2, "0");
  const clock = (seconds: number) => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    return `${pad(hours)}:${pad(minutes)}:${pad(remainder)}`;
  };
  const unit = (value: number, name: "second" | "year" | "month" | "day") =>
    new Intl.NumberFormat(getActiveLocale(), {
      style: "unit", unit: name, unitDisplay: "short",
      maximumFractionDigits: 0,
    }).format(value);

  if (total < 60) return unit(total, "second");
  if (total < 3600) return `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
  if (total < DAY_SECONDS) return clock(total);

  let remainder = total;
  const years = Math.floor(remainder / YEAR_SECONDS);
  remainder -= years * YEAR_SECONDS;
  const months = Math.floor(remainder / MONTH_SECONDS);
  remainder -= months * MONTH_SECONDS;
  const days = Math.floor(remainder / DAY_SECONDS);
  remainder -= days * DAY_SECONDS;

  const parts: string[] = [];
  if (years) parts.push(unit(years, "year"));
  if (months) parts.push(unit(months, "month"));
  if (days) parts.push(unit(days, "day"));
  return `${parts.join(" ")} ${clock(remainder)}`.trim();
}

function lifetimeAudioSharePercent(
  name: string,
  audioSeconds: number | null | undefined,
  totalAudioSeconds: number,
): number | null {
  const attribution = name.trim();
  if (!attribution || attribution.toLowerCase().includes("unknown")) return null;
  const used = Number(audioSeconds);
  const total = Number(totalAudioSeconds);
  if (!Number.isFinite(used) || used <= 0 ||
      !Number.isFinite(total) || total <= 0 || used > total) return null;
  return (used / total) * 100;
}

function renderLifetimeUsageList(
  id: string,
  usage: Record<string, LifetimeStatisticsUsage> | undefined,
  totalAudioSeconds: number,
): void {
  const list = document.getElementById(id);
  if (!list) return;
  list.replaceChildren();

  const entries = Object.entries(usage || {}).sort(
    (a, b) => (b[1]?.transcriptions || 0) - (a[1]?.transcriptions || 0),
  );
  if (!entries.length) {
    const empty = document.createElement("li");
    empty.className = "lifetime-usage-empty";
    empty.textContent = translateKey("msg.no_usage_recorded.1d21");
    list.appendChild(empty);
    return;
  }
  for (const [name, value] of entries) {
    const item = document.createElement("li");
    const share = lifetimeAudioSharePercent(name, value?.audioSeconds, totalAudioSeconds);
    const shareLabel = share === null
      ? translateKey("msg.lifetime_audio_share_unavailable.1d12")
      : translateKey("msg.lifetime_audio_share_percent.1d12", {
          share: share < 0.05 ? "<0.1" : share.toFixed(1),
        });
    const modelKey = id === "lifetime-model-list" ? modelNameLocaleKey(name) : null;
    const displayName = modelKey ? translateKey(modelKey, {}, getActiveLocale()) : formatLifetimeHardwareDeviceLabel(name);
    item.textContent = translateKey("msg.lifetime_usage_transcription_count.1d21", { name: displayName, count: String(value.transcriptions), share: shareLabel });
    list.appendChild(item);
  }
}

let lifetimeStatisticsRefreshInFlight = false;

let lifetimeMemberSinceAnimationObserver: IntersectionObserver | null = null;
let lifetimeMemberSinceAnimationStarted = false;

function armLifetimeMemberSinceAnimation(pill: HTMLElement): void {
  if (lifetimeMemberSinceAnimationStarted || lifetimeMemberSinceAnimationObserver) return;
  if (typeof IntersectionObserver === "undefined") return;
  lifetimeMemberSinceAnimationObserver = new IntersectionObserver((entries, observer) => {
    if (!entries.some(entry => entry.isIntersecting && entry.intersectionRatio >= 0.35)) return;
    lifetimeMemberSinceAnimationStarted = true;
    pill.classList.add("is-in-view");
    observer.disconnect();
    lifetimeMemberSinceAnimationObserver = null;
  }, { threshold: [0.35] });
  lifetimeMemberSinceAnimationObserver.observe(pill);
}


async function refreshLifetimeStatistics(): Promise<void> {
    if (lifetimeStatisticsRefreshInFlight) return;
    lifetimeStatisticsRefreshInFlight = true;
  const status = document.getElementById("lifetime-statistics-status");
  if (status) status.textContent = translateKey("msg.refreshing_statistics.1d21");
  try {
    await appleGpuDisplayLabelPromise;
    const stats = await invoke<LifetimeStatistics>("get_lifetime_statistics");
    const memberSince = document.getElementById("lifetime-member-since");
    if (memberSince && Number.isFinite(stats.memberSinceAtMillis)) {
      const monthYear = new Date(stats.memberSinceAtMillis as number)
        .toLocaleDateString(getActiveLocale(), { month: "long", year: "numeric" });
      memberSince.textContent = translateKey("msg.valued_member_since.1d21", { date: monthYear }, getActiveLocale());
      armLifetimeMemberSinceAnimation(memberSince);
    }
    const setText = (id: string, value: string) => {
      const element = document.getElementById(id);
      if (element) element.textContent = formatHardwareDeviceText(value);
    };
    const audioSeconds = Number(stats.totalAudioSeconds) || 0;
    const inferenceMs = Number(stats.totalInferenceMilliseconds) || 0;
    const speed = inferenceMs > 0
      ? `${(audioSeconds / (inferenceMs / 1000)).toFixed(2)}× RTF` : "—";

    setText("lifetime-total-transcriptions", String(stats.totalTranscriptions || 0));
    setText("lifetime-total-audio", formatLifetimeDuration(audioSeconds));
    setText("lifetime-total-inference", formatLifetimeDuration(inferenceMs / 1000));
    setText("lifetime-average-speed", speed);
    setText("lifetime-total-upload", formatLifetimeBytes(Number(stats.totalUploadBytes) || 0));
    renderLifetimeUsageList("lifetime-hardware-list", stats.hardwareUsage, audioSeconds);
    renderLifetimeUsageList("lifetime-model-list", stats.modelUsage, audioSeconds);
    setText("lifetime-statistics-status", stats.lastTranscriptionAt
      ? translateKey("msg.lifetime_updated_last_transcription.1d21", {
          date: new Date(stats.lastTranscriptionAt).toLocaleString(getActiveLocale()),
        })
      : translateKey("msg.no_successful_transcriptions.1d21"));
  } catch (error) {
    if (status) status.textContent = translateKey("msg.lifetime_statistics_unavailable.1d21", {
        error: formatHardwareDeviceText(String(error)),
      });
  } finally {
    lifetimeStatisticsRefreshInFlight = false;
  }
}

window.addEventListener("rdt-connect-language-changed", () => {
  if (isLifetimeStatisticsVisible()) void refreshLifetimeStatistics();
});

function isLifetimeStatisticsVisible(): boolean {
  const section = document.getElementById("settings-lifetime-section");
  return !!section && document.visibilityState !== "hidden"
    && section.getClientRects().length > 0
    && getComputedStyle(section).visibility !== "hidden";
}

let lifetimeStatisticsWasVisible = false;
const lifetimeStatisticsObserver = new MutationObserver(() => {
  const visible = isLifetimeStatisticsVisible();
  if (visible && !lifetimeStatisticsWasVisible) void refreshLifetimeStatistics();
  lifetimeStatisticsWasVisible = visible;
});
lifetimeStatisticsObserver.observe(document.body, {
  subtree: true, attributes: true,
  attributeFilter: ["class", "hidden", "style", "aria-hidden"],
});
document.addEventListener("visibilitychange", () => {
  if (isLifetimeStatisticsVisible()) void refreshLifetimeStatistics();
});
window.setInterval(() => {
  if (isLifetimeStatisticsVisible()) void refreshLifetimeStatistics();
}, 30_000);
lifetimeStatisticsWasVisible = isLifetimeStatisticsVisible();
if (lifetimeStatisticsWasVisible) void refreshLifetimeStatistics();

// Keep normal app tabs available. Developer Options and the Remote Access diagnostics
// disclosure are hidden entirely while DEV is off.
const viewTabs = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-view-target]"));
const appViews = Array.from(document.querySelectorAll<HTMLElement>(".app-view"));
const developerTab = document.querySelector<HTMLButtonElement>('[data-view-target="developer"]');
const remoteTab = document.querySelector<HTMLButtonElement>('[data-view-target="remote"]');
const offlineOnlyModeToggle = document.querySelector<HTMLInputElement>("#offline-only-mode-toggle");
const OFFLINE_ONLY_MODE_KEY = "rdt_connect_offline_only_mode";
let offlineOnlyModeEnabled = false;
try { offlineOnlyModeEnabled = localStorage.getItem(OFFLINE_ONLY_MODE_KEY) === "true"; } catch { /* optional preference */ }
if (offlineOnlyModeToggle) offlineOnlyModeToggle.checked = offlineOnlyModeEnabled;
await invoke("set_offline_only_mode", { enabled: offlineOnlyModeEnabled }).catch(() => undefined);
const remoteDiagnosticsPanel = document.querySelector<HTMLDetailsElement>("#view-remote details.remote-diagnostics-tools");
const developerOptionsToggle = document.querySelector<HTMLInputElement>("#developer-options-toggle");
const developerToggleButton = document.querySelector<HTMLButtonElement>("#developer-toggle-button");
const localServerDetails = document.querySelector<HTMLDetailsElement>(".local-server-details");
const versionDetails = document.querySelector<HTMLDetailsElement>(".version-details");
const headerCheckUpdatesButton = $("header-check-updates-button") as HTMLButtonElement;
const aboutCheckUpdatesButton = $("about-check-updates-button") as HTMLButtonElement;
const applicationUpdatePanel = $("application-update-panel") as HTMLDivElement;
const applicationUpdateMessage = $("application-update-message") as HTMLParagraphElement;
const applicationUpdateActionButton = $("application-update-action-button") as HTMLButtonElement;
const headerUpdateActionButton = document.querySelector<HTMLButtonElement>("#header-update-action-button");
const headerVersionLabel = $("header-version-label") as HTMLSpanElement;
const headerBuildId = $("header-build-id") as HTMLSpanElement;
const aboutVersionLabel = $("about-version-label") as HTMLSpanElement;
let developerOptionsEnabled = false;
try { developerOptionsEnabled = localStorage.getItem("rdt_connect_developer_options") === "true"; } catch { /* optional preference */ }
function syncDeveloperVisibility() {
  const hidden = !developerOptionsEnabled;
  if (developerTab) {
    developerTab.classList.toggle("hidden", hidden);
    developerTab.setAttribute("aria-hidden", String(hidden));
    developerTab.tabIndex = hidden ? -1 : 0;
  }
  if (remoteTab) {
    remoteTab.classList.toggle("hidden", offlineOnlyModeEnabled);
    remoteTab.setAttribute("aria-hidden", String(offlineOnlyModeEnabled));
    remoteTab.tabIndex = offlineOnlyModeEnabled ? -1 : 0;
  }
  if (offlineOnlyModeEnabled && document.getElementById("view-remote")?.classList.contains("hidden") === false) {
    setAppView("local", false);
  }
  if (remoteDiagnosticsPanel) {
    remoteDiagnosticsPanel.classList.toggle("hidden", hidden);
    remoteDiagnosticsPanel.setAttribute("aria-hidden", String(hidden));
    if (hidden) remoteDiagnosticsPanel.open = false;
  }
}
if (developerOptionsToggle) developerOptionsToggle.checked = developerOptionsEnabled;
syncDeveloperVisibility();

let offlineOnlyPreviousView: string | null = null;

offlineOnlyModeToggle?.addEventListener("change", () => {
  const enablingOfflineOnly = offlineOnlyModeToggle.checked;
  if (enablingOfflineOnly) {
    offlineOnlyPreviousView =
      document.querySelector<HTMLButtonElement>('[data-view-target].active')?.dataset.viewTarget || null;
  }
  offlineOnlyModeEnabled = enablingOfflineOnly;
  syncAutomaticUpdateToggleOfflineState();
  try { localStorage.setItem(OFFLINE_ONLY_MODE_KEY, String(offlineOnlyModeEnabled)); } catch { /* optional preference */ }
  void invoke("set_offline_only_mode", { enabled: offlineOnlyModeEnabled }).catch(() => undefined);
  syncDeveloperVisibility();
  if (offlineOnlyModeEnabled) {
    if (updateCheckInFlight && !updateCheckInFlightManual) {
      updateCheckGeneration += 1;
      updateCheckInFlight = null;
      updateCheckInFlightManual = false;
    }
    showNotice("Offline only mode is enabled. Automatic update checks are paused.", "info");
  } else {
    const savedView = (() => {
      try { return localStorage.getItem("rdt_connect_active_view") || "local"; }
      catch { return "local"; }
    })();
    setAppView(offlineOnlyPreviousView || savedView, false);
    offlineOnlyPreviousView = null;
    showNotice("Offline only mode is disabled. RDT Voice will prefer the official website and fall back offline when unavailable.", "info");
    if (canRunAutomaticUpdateCheck()) void checkForApplicationUpdates();
  }
});

function syncDeveloperToggleButton() {
  if (!developerToggleButton) return;
  developerToggleButton.textContent = developerOptionsEnabled ? translateKey("msg.dev_on.3074426d", {}, getActiveLocale()) : translateKey("msg.dev.4030fe15", {}, getActiveLocale());
  developerToggleButton.setAttribute("aria-pressed", String(developerOptionsEnabled));
  developerToggleButton.classList.toggle("enabled", developerOptionsEnabled);
  developerToggleButton.title = developerOptionsEnabled ? translateKey("msg.developer_tools_enabled.3fb8a401", {}, getActiveLocale()) : translateKey("msg.enable_developer_tools.2ec3eb03", {}, getActiveLocale());
}
syncDeveloperToggleButton();
const connectMemoryMeter = document.querySelector<HTMLButtonElement>("#connect-memory-meter");
const connectMemoryMacOS =
  /Macintosh|Mac OS X/i.test(navigator.userAgent) || navigator.platform === "MacIntel";
let connectMemoryTimer: number | null = null;
let connectMemoryInFlight = false;

function connectMemoryEligible(): boolean {
  return connectMemoryMacOS && developerOptionsEnabled &&
    document.querySelector('[data-view-target="local"].active') !== null;
}
function formatConnectMemory(mb: number): string {
  return mb >= 1024
    ? `${(mb / 1024).toFixed(1)} GB`
    : `${Math.round(mb / 5) * 5} MB`;
}

let connectMemoryLatestSample: { currentMb: number; peakMb: number } | null = null;
let connectMemoryShowingPeak = false;

function renderConnectMemoryMeter(): void {
  const meter = connectMemoryMeter;
  const sample = connectMemoryLatestSample;
  if (!meter || !sample) return;
  const value = connectMemoryShowingPeak ? sample.peakMb : sample.currentMb;
  meter.textContent = `${connectMemoryShowingPeak ? "PEAK" : "RAM"} ${formatConnectMemory(value)}`;
  meter.classList.toggle("peak", connectMemoryShowingPeak);
  meter.setAttribute("aria-pressed", String(connectMemoryShowingPeak));
  meter.setAttribute(
    "aria-label",
    `${connectMemoryShowingPeak ? "Peak" : "Current"} memory usage ${formatConnectMemory(value)}. Click to switch display.`
  );
}

connectMemoryMeter?.addEventListener("click", () => {
  connectMemoryShowingPeak = !connectMemoryShowingPeak;
  renderConnectMemoryMeter();
});
async function refreshConnectMemoryMeter(): Promise<void> {
  if (!connectMemoryMeter || connectMemoryInFlight || !connectMemoryEligible()) return;
  connectMemoryInFlight = true;
  try {
    const sample = await invoke<{ currentMb: number; peakMb: number }>("get_connect_memory");
    if (!connectMemoryEligible()) return;
    if (!Number.isFinite(sample.currentMb) || !Number.isFinite(sample.peakMb) ||
        sample.currentMb <= 0 || sample.peakMb <= 0) {
        connectMemoryMeter.classList.add("hidden");
      return;
    }
    connectMemoryLatestSample = sample;
    connectMemoryMeter.title =
      "Summed resident memory (RSS) for the RDT Connect app, its helper processes, " +
      "the managed transcription backend and its workers. The loaded model is included " +
      "when resident. Shared pages may be counted more than once. Updated every 7.5 seconds. " +
      `Peak observed while the meter was active this session: ${formatConnectMemory(sample.peakMb)}.`;
    connectMemoryMeter.classList.remove("hidden");
    renderConnectMemoryMeter();
  } catch {
    connectMemoryMeter.classList.add("hidden");
  } finally {
    connectMemoryInFlight = false;
  }
}
function syncConnectMemoryMeter(): void {
  if (!connectMemoryMeter) return;
  if (!connectMemoryEligible()) {
    if (connectMemoryTimer !== null) window.clearInterval(connectMemoryTimer);
    connectMemoryTimer = null;
    connectMemoryMeter.classList.add("hidden");
    return;
  }
  void refreshConnectMemoryMeter();
  if (connectMemoryTimer === null) {
    connectMemoryTimer = window.setInterval(() => {
      void refreshConnectMemoryMeter();
    }, 7500);
  }
}

function setAppView(target: string, persist = true) {
  const allowed = ["local", "settings"].includes(target)
    || (target === "remote" && !offlineOnlyModeEnabled)
    || (target === "developer" && developerOptionsEnabled);
  const safeTarget = allowed ? target : "local";
  for (const view of appViews) view.classList.toggle("hidden", view.id !== `view-${safeTarget}`);
  for (const tab of viewTabs) {
    const active = tab.dataset.viewTarget === safeTarget;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
  if (persist) { try { localStorage.setItem("rdt_connect_active_view", safeTarget); } catch { /* optional preference */ } }

  syncConnectMemoryMeter();
}
function setDeveloperOptions(enabled: boolean) {
  developerOptionsEnabled = enabled;
  syncDeveloperVisibility();
  if (developerOptionsToggle) developerOptionsToggle.checked = enabled;
  syncDeveloperToggleButton();
  try { localStorage.setItem("rdt_connect_developer_options", String(enabled)); } catch { /* optional preference */ }
  localPortTools.classList.toggle("hidden", !enabled);
  inferenceBenchmarkCard.classList.toggle("hidden", !enabled);
  if (!enabled && localServerDetails) localServerDetails.open = false;
  if (!enabled && document.getElementById("view-developer")?.classList.contains("hidden") === false) setAppView("local");
  // Keep the model panel in sync immediately when Developer Options changes.
  renderModelDownloadUI();

  syncConnectMemoryMeter();
}
window.setTimeout(syncConnectMemoryMeter, 0);

for (const tab of viewTabs) tab.addEventListener("click", () => {
  const target = tab.dataset.viewTarget || "local";
  if (target === "developer" && !developerOptionsEnabled) {
    showNotice(translateKey("msg.enable_dev_to_open_developer_options.9f67a54e", {}, getActiveLocale()), "info");
    return;
  }
  setAppView(target);
  if (target === "developer") void refreshLifetimeStatistics();
  if (target === "settings") { void refreshLocalHttpsStatus(); void refreshModelStorageStatus(); void refreshModelCacheBadges(); }
});
if (developerOptionsToggle) developerOptionsToggle.addEventListener("change", () => {
  setDeveloperOptions(developerOptionsToggle.checked);
  if (developerOptionsToggle.checked) showNotice(translateKey("msg.developer_tools_enabled.dc8a294c", {}, getActiveLocale()), "info");
  else showNotice(translateKey("msg.ready_to_connect.5802ac49", {}, getActiveLocale()), "info");
});
if (developerToggleButton) developerToggleButton.addEventListener("click", () => {
  setDeveloperOptions(!developerOptionsEnabled);
  showNotice(developerOptionsEnabled ? translateKey("msg.developer_tools_enabled.dc8a294c", {}, getActiveLocale()) : translateKey("msg.ready_to_connect.5802ac49", {}, getActiveLocale()), "info");
});

// Native language selection uses the same catalogs as automatic first-launch detection.
const appLanguageSelect = $("app-language") as HTMLSelectElement;
const appLanguageStatus = $("app-language-status");
const brandHomeLink = $("brand-home-link") as HTMLAnchorElement;
const RDT_VOICE_HOME_URL = "https://rolandoristech.github.io/rdt-voice/";
function updateRdtVoiceHomeLinkLocale() {
  if (!brandHomeLink) return;
  const url = new URL(RDT_VOICE_HOME_URL);
  url.searchParams.set("rdt_connect_lang", getActiveLocale());
  brandHomeLink.href = url.toString();
}
function updateAppLanguageStatus() {
  const active = getActiveLocale();

  appLanguageSelect.value = active;
  appLanguageSelect.setAttribute("aria-label", translateKey("msg.interface_language.8027f432", {}, active));
  appLanguageSelect.setAttribute("title", translateKey("msg.choose_the_language_used_throughout_rdt_conn.81f7adea", {}, active));
  appLanguageStatus.textContent = translateKey("msg.interface_language.8027f432", {}, active);
}
updateAppLanguageStatus();
updateRdtVoiceHomeLinkLocale();
// Keep native tray labels in the same language as the window; no error means the UI can still work offline.
void invoke("set_app_language", { locale: getActiveLocale() }).catch(() => undefined);
appLanguageSelect.addEventListener("change", () => {
  const requested = appLanguageSelect.value;
  void setActiveLocale(requested, true).then((next) => {
    if (requested !== next) return;
    updateAppLanguageStatus();
    refreshLocalizedModelMenu();
    updateRdtVoiceHomeLinkLocale();
    renderModelDownloadUI();
    void refreshLocalHttpsStatus();
    void refreshModelStorageStatus();
    void refreshModelCacheBadges();
    void refreshAppBuildInfo();
    void refreshBenchmarkDiagnostics();
    if (latestHardwareOptimizationResult) renderHardwareComparison(latestHardwareOptimizationResult);
    void invoke("set_app_language", { locale: next }).catch(() => {
      // The web UI preference is still saved; Rust tray labels refresh on the next launch.
    });
  }).catch((error) => {
    console.error("Could not change the RDT Connect interface language.", error);
    appLanguageSelect.value = getActiveLocale();
  });
});

const APP_INTERFACE_SCALE_KEY = "rdt_connect_interface_scale_percent";
const appInterfaceScaleSelect = $("app-interface-scale") as HTMLSelectElement;
const appShell = document.querySelector<HTMLElement>(".app-shell");

function normalizeAppInterfaceScalePercent(value: unknown): number {
  const candidate = Number(value);
  return Number.isInteger(candidate) && candidate >= 100 && candidate <= 150 && (candidate - 100) % 5 === 0
    ? candidate
    : 110;
}

function applyAppInterfaceScale(percent: number, refreshGuide = true): void {
  const scale = percent / 100;
  if (appShell) {
    appShell.style.setProperty("zoom", scale.toFixed(2));
    // Percentage widths are not magnified by CSS zoom. Keep the shell at
    // the viewport's full width and compensate only the pixel max-width.
    appShell.style.width = "100%";
    appShell.style.maxWidth = `${(1440 / scale).toFixed(6)}px`;
    // Keep the horizontal shell gutters constant in rendered pixels too.
    const horizontalPadding = window.matchMedia("(max-width: 880px)").matches ? 16 : 24;
    const scaledHorizontalPadding = `${(horizontalPadding / scale).toFixed(6)}px`;
    appShell.style.paddingLeft = scaledHorizontalPadding;
    appShell.style.paddingRight = scaledHorizontalPadding;
    appShell.style.boxSizing = "border-box";
    appShell.style.removeProperty("transform");
    appShell.style.removeProperty("transform-origin");
  }
  appInterfaceScaleSelect.value = String(percent);
  // Re-measure the actual target after zoom/layout has been applied.
  if (refreshGuide) window.requestAnimationFrame(() => scheduleVisualSetupGuideUpdate());
}

let initialAppInterfaceScale = 110;
try {
  initialAppInterfaceScale = normalizeAppInterfaceScalePercent(
    localStorage.getItem(APP_INTERFACE_SCALE_KEY) ?? 110
  );
} catch { /* use the 110% default when storage is unavailable */ }
applyAppInterfaceScale(initialAppInterfaceScale, false);

appInterfaceScaleSelect.addEventListener("change", () => {
  const nextScale = normalizeAppInterfaceScalePercent(appInterfaceScaleSelect.value);
  applyAppInterfaceScale(nextScale);
  try {
    localStorage.setItem(APP_INTERFACE_SCALE_KEY, String(nextScale));
  } catch { /* preference remains active for this session */ }
});

// Keep the shell's max-width and responsive gutters correct when the window
// crosses the compact-layout breakpoint or changes size.
window.addEventListener("resize", () => {
  applyAppInterfaceScale(
    normalizeAppInterfaceScalePercent(appInterfaceScaleSelect.value),
    false
  );
});

// Preserve the user's last active view, as the previous English-only startup code did.
try {
  const savedView = localStorage.getItem("rdt_connect_active_view") || "local";
  setAppView(savedView, false);
} catch { setAppView("local", false); }

const statusPill = $("server-status");
const statusDetail = $("server-detail");
const connectionStatus = $("connection-status");
const connectionSummary = $("connection-summary");
const testConnectionButton = $("test-connection-button") as HTMLButtonElement;
const orphanPanel = $("orphan-backend-panel");
const orphanUserMessage = $("orphan-user-message");
const orphanDetail = $("developer-orphan-detail");
const developerStopError = $("developer-stop-error");
const modelIdDetail = $("model-id-detail");
const localHttpsStatusEl = $("local-https-status");
const localHttpsAdvancedStatusEl = $("local-https-advanced-status");
const localHttpsCertPathEl = $("tls-cert-path");
const localHttpsKeyPathEl = $("tls-key-path");
const localHttpsCaPathEl = $("tls-ca-path");
const setupLocalHttpsButton = $("setup-local-https-button") as HTMLButtonElement;
const localHttpsQuickPrompt = $("local-https-quick-prompt");
const localHttpsQuickStatus = $("local-https-quick-status");
const localHttpsQuickSetupButton = $("local-https-quick-setup-button") as HTMLButtonElement;
const localHttpsDetailsButton = $("local-https-details-button") as HTMLButtonElement;

const visualSetupGuideToggle = $("visual-setup-guide-toggle") as HTMLInputElement;
const autoCheckUpdatesToggle = $("auto-check-updates-toggle") as HTMLInputElement;

/* 1D40: accessible Offline Mode update tooltip. */
function syncAutomaticUpdateToggleOfflineState(): void {
  const wrapper = autoCheckUpdatesToggle.closest<HTMLElement>(".auto-check-updates-toggle");
  autoCheckUpdatesToggle.disabled = offlineOnlyModeEnabled;
  wrapper?.classList.toggle("is-offline-disabled", offlineOnlyModeEnabled);
  if (!wrapper) return;

  const tooltipId = "auto-check-updates-offline-tooltip";
  let tooltip = wrapper.querySelector<HTMLElement>(".offline-update-tooltip");
  if (!tooltip) {
    tooltip = document.createElement("span");
    tooltip.className = "offline-update-tooltip";
    tooltip.id = tooltipId;
    tooltip.setAttribute("role", "tooltip");
    wrapper.append(tooltip);
  }

  tooltip.textContent = translateKey("msg.offline_update_tooltip.1d40");
  tooltip.setAttribute("aria-hidden", offlineOnlyModeEnabled ? "false" : "true");
  wrapper.dataset.offlineUpdateTooltip = offlineOnlyModeEnabled ? "true" : "false";

  const describedBy = new Set(
    (wrapper.getAttribute("aria-describedby") ?? "").split(/\s+/).filter(Boolean)
  );
  if (offlineOnlyModeEnabled) {
    describedBy.add(tooltipId);
    if (!wrapper.hasAttribute("tabindex")) {
      wrapper.setAttribute("tabindex", "0");
      wrapper.dataset.offlineTooltipAddedTabindex = "true";
    }
  } else {
    describedBy.delete(tooltipId);
    if (wrapper.dataset.offlineTooltipAddedTabindex === "true") {
      wrapper.removeAttribute("tabindex");
      delete wrapper.dataset.offlineTooltipAddedTabindex;
    }
  }

  if (describedBy.size) {
    wrapper.setAttribute("aria-describedby", [...describedBy].join(" "));
  } else {
    wrapper.removeAttribute("aria-describedby");
  }
}
const autoInstallUpdatesToggle = $("auto-install-updates-toggle") as HTMLInputElement;

const modelHelp = $("model-help");
const remoteActionHelp = $("remote-action-help");
const expandQrButton = $("expand-qr") as HTMLButtonElement;
const qrExpandedOverlay = $("qr-expanded-overlay");
const qrExpandedImage = $("qr-expanded-image") as HTMLImageElement;
const qrExpandedCountdown = $("qr-expanded-countdown");
const closeExpandedQrButton = $("close-expanded-qr") as HTMLButtonElement;
const chooseTlsCertButton = $("choose-tls-cert-button") as HTMLButtonElement;
const chooseTlsKeyButton = $("choose-tls-key-button") as HTMLButtonElement;
const chooseTlsCaButton = $("choose-tls-ca-button") as HTMLButtonElement;
const linkTlsButton = $("link-tls-button") as HTMLButtonElement;
const trustTlsButton = $("trust-tls-button") as HTMLButtonElement;
const removeTlsTrustButton = $("remove-tls-trust-button") as HTMLButtonElement;
const transportHttpsInput = $("transport-https") as HTMLInputElement;
const transportHttpInput = $("transport-http") as HTMLInputElement;
const keepInMenuBarInput = $("keep-in-menu-bar") as HTMLInputElement;

function syncKeepInMenuBarPlatformCopy(): void {
  if (!isWindowsRuntime()) return;
  const copy = keepInMenuBarInput.closest(".menu-bar-option-copy");
  const heading = copy?.querySelector("strong");
  const description = copy?.querySelector("small");
  if (heading) heading.textContent = "Show in notification center";
  if (description) {
    description.textContent =
      "When on, closing the window keeps RDT Connect in the notification center. When off, closing quits the app and stops its managed server.";
  }
}
syncKeepInMenuBarPlatformCopy();
const settingsLockHint = document.querySelector<HTMLElement>("#settings-lock-hint");
const localAddress = $("local-address");
const preferredPortInput = $("preferred-port") as HTMLInputElement;
const localPortTools = $("local-port-tools");
const inferenceBenchmarkCard = $("inference-benchmark-card");
const openNativeBenchmarkButton = $("open-native-benchmark-button") as HTMLButtonElement;
const trayTestPhraseStatus = document.querySelector<HTMLElement>("#tray-test-phrase-status");
localPortTools.classList.toggle("hidden", !developerOptionsEnabled);
inferenceBenchmarkCard.classList.toggle("hidden", !developerOptionsEnabled);
if (!developerOptionsEnabled && localServerDetails) localServerDetails.open = false;
const portHelp = $("port-help");
const displayPort = $("display-port");
const tokenDisplay = $("token-display") as HTMLInputElement;
const toggleTokenButton = $("toggle-token-button") as HTMLButtonElement;
const copyTokenButton = $("copy-token-button") as HTMLButtonElement;
const copyEndpointButton = $("copy-endpoint-button") as HTMLButtonElement;
const copyRemoteUrlButton = $("copy-remote-url-button") as HTMLButtonElement;
const remoteUrlValue = $("remote-url");
const browserChoiceRow = $("browser-choice-row");
const preferredPortTooltip = $("preferred-port-tooltip");
const modelSelect = $("model-select") as HTMLSelectElement;
const modelSelectTooltip = $("model-select-tooltip");
const modelSelectTrigger = $("model-select-trigger") as HTMLButtonElement;
const modelSelectMenu = $("model-select-menu");
const modelSelectCurrentName = $("model-select-current-name");
const modelSelectCurrentDetail = $("model-select-current-detail");
const modelSelectCurrentBadge = $("model-select-current-badge");
let modelSwitchTargetAfterStop: string | null = null;
const modelOptionBaseLabels = new Map<string, string>(Array.from(modelSelect.options).map((option) => [option.value, option.textContent || option.value]));
let recommendedModelTone: "tiny" | "base" = "tiny";
const modelMenuDetails: Record<string, { name: string; description: string; size: string; recommended?: boolean }> = {
  "mlx-community/whisper-tiny-mlx": { name: "Tiny", description: "Fastest · smallest download (may occasionally omit punctuation)", size: "~74 MB" },
  "mlx-community/whisper-base-mlx": { name: "Base", description: "Lightweight choice", size: "~144 MB" },
  "mlx-community/whisper-small-mlx": { name: "Small", description: "Middle-size model", size: "~481 MB" },
  "mlx-community/whisper-large-v3-turbo": { name: "Large v3 Turbo", description: "Strong balance of speed and quality", size: "~1.61 GB", recommended: true },
  "mlx-community/whisper-large-v3-mlx": { name: "Large v3", description: "Largest download in this list", size: "~3.08 GB" },
};
function modelNameLocaleKey(model: string): string | null {
  switch (modelToneFor(model)) {
    case "tiny": return "msg.whisper_tiny.bd612f06";
    case "base": return "msg.whisper_base.69e01c4a";
    case "small": return "msg.whisper_small.274ca4bf";
    case "turbo": return "msg.whisper_large_v3_turbo.ba2e4b36";
    case "large": return "msg.whisper_large_v3.332fb9ac";
    default: return null;
  }
}
function modelGuidanceLocaleKey(model: string): string | null {
  switch (modelToneFor(model)) {
    case "tiny": return "msg.model_guidance_tiny.1d20";
    case "base": return "msg.model_guidance_base.1d20";
    case "small": return "msg.model_guidance_small.1d20";
    case "turbo": return "msg.model_guidance_turbo.1d20";
    case "large": return "msg.model_guidance_large.1d20";
    default: return null;
  }
}
function modelMenuInfo(option: HTMLOptionElement) {
  const details = modelMenuDetails[option.value] || { name: option.textContent || option.value, description: "Whisper model", size: "Size varies" };
  const locale = getActiveLocale();
  const nameKey = modelNameLocaleKey(option.value);
  const guidanceKey = modelGuidanceLocaleKey(option.value);
  const tone = modelToneFor(option.value);
  return {
    ...details,
    name: nameKey ? translateKey(nameKey, {}, locale) : details.name,
    description: guidanceKey ? translateKey(guidanceKey, {}, locale) : details.description,
    recommended: tone !== "other" && tone === recommendedModelTone,
  };
}
function refreshLocalizedModelMenu(): void {
  for (const option of Array.from(modelSelect.options)) {
    const info = modelMenuInfo(option);
    modelOptionBaseLabels.set(option.value, info.name);
    option.textContent = option.dataset.cacheState === "downloaded"
      ? translateKey("msg.baselabel_downloaded.bf85439f", { baseLabel: info.name }, getActiveLocale())
      : info.name;
  }
  renderModelSelectMenu();
  syncModelSelectPresentation();
  renderModelCacheSelection();
}
function modelToneFor(model: string): "tiny" | "base" | "small" | "turbo" | "large" | "other" {
  if (model === "tiny" || model.endsWith("whisper-tiny-mlx")) return "tiny";
  if (model === "base" || model.endsWith("whisper-base-mlx")) return "base";
  if (model === "small" || model.endsWith("whisper-small-mlx")) return "small";
  if (model === "large-v3-turbo" || model.endsWith("whisper-large-v3-turbo")) return "turbo";
  if (model === "large-v3" || model.endsWith("whisper-large-v3-mlx")) return "large";
  return "other";
}
function isLinuxRuntime(): boolean {
  return /Linux/i.test(`${navigator.userAgent} ${navigator.platform}`) && !/Android/i.test(navigator.userAgent);
}
function isWindowsRuntime(): boolean {
  return /Windows/i.test(`${navigator.userAgent} ${navigator.platform}`);
}
function isWindowsOrLinuxRuntime(): boolean {
  return /Windows|Linux/i.test(`${navigator.userAgent} ${navigator.platform}`);
}
let platformModelCatalogFailed = false;

function isNativeGgmlCatalog(): boolean {
  return modelSelect.options.length > 0 && modelSelect.options[0].value === "tiny";
}
type PlatformModelOption = { id: string; name: string; description: string; size: string; recommended: boolean };
async function initializePlatformModelCatalog() {
  try {
    const catalog = await invoke<PlatformModelOption[]>("get_platform_model_catalog");
    if (!Array.isArray(catalog) || catalog.length !== 5 || catalog.some((item) => !item || typeof item.id !== "string" || typeof item.name !== "string")) {
      failPlatformModelCatalog();
      return;
    }
    const previousValue = modelSelect.value;
    for (const key of Object.keys(modelMenuDetails)) delete modelMenuDetails[key];
    modelOptionBaseLabels.clear();
    modelSelect.replaceChildren();
    for (const item of catalog) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.name;
      modelSelect.appendChild(option);
      modelOptionBaseLabels.set(item.id, item.name);
      modelMenuDetails[item.id] = { name: item.name, description: item.description, size: item.size, recommended: item.recommended };
    }
    let savedValue: string | null = null;
    try { savedValue = localStorage.getItem("rdt_voice_desktop_model"); } catch { /* optional preference */ }
    modelSelect.value = catalog.some((item) => item.id === savedValue) ? savedValue! : (catalog.some((item) => item.id === previousValue) ? previousValue : catalog[0].id);
    const label = modelSelect.closest(".local-model-label")?.querySelector("label");
    if (label) label.textContent = isNativeGgmlCatalog() ? translateKey("msg.choose_a_model_ggml_cpu_baseline.1d24", {}, getActiveLocale()) : translateKey("msg.choose_a_model_apple_mlx.248ca57c", {}, getActiveLocale());
    modelIdDetail.textContent = modelSelect.value;
    const selectedInfo = modelMenuInfo(modelSelect.options[modelSelect.selectedIndex]);
    modelSelectCurrentDetail.textContent = `${selectedInfo.size}${selectedInfo.recommended ? ` · ${translateKey("msg.recommended.d70604e8", {}, getActiveLocale())}` : ""}`;
    renderModelSelectMenu();
    syncModelSelectPresentation();
    void refreshModelCacheBadges();
  } catch {
    if (isWindowsOrLinuxRuntime()) {
      failPlatformModelCatalog();
      return;
    }
    // Preserve the bundled catalog fallback for older macOS native binaries.
  }
}

function failPlatformModelCatalog() {
  if (!isWindowsOrLinuxRuntime()) return;
  platformModelCatalogFailed = true;
  modelSelect.replaceChildren();
  modelSelect.disabled = true;
  const label = modelSelect.closest(".local-model-label")?.querySelector("label");
  if (label) label.textContent = "Model catalog unavailable:";
  modelIdDetail.textContent = "Unavailable";
  modelHelp.textContent =
    "The native model catalog could not be loaded. Restart or repair RDT Connect; incompatible models will not be offered on this platform.";
}


function syncModelSelectPresentation() {
  const selected = modelSelect.options[modelSelect.selectedIndex];
  if (selected) {
    const info = modelMenuInfo(selected);
    modelSelectCurrentName.textContent = info.name;
    modelSelectCurrentName.dataset.modelTone = modelToneFor(selected.value);
    const selectedInfo = modelMenuInfo(modelSelect.options[modelSelect.selectedIndex]);
    modelSelectCurrentDetail.textContent = `${selectedInfo.size}${selectedInfo.recommended ? ` · ${translateKey("msg.recommended.d70604e8", {}, getActiveLocale())}` : ""}`;
    modelSelectCurrentBadge.hidden = selected.dataset.cacheState !== "downloaded";
  }
  modelSelectTrigger.disabled = modelSelect.disabled;
  modelSelectTrigger.title = modelSelect.title || translateKey("msg.choose_a_transcription_model.569c22c8", {}, getActiveLocale());
  for (const item of Array.from(modelSelectMenu.querySelectorAll<HTMLButtonElement>(".model-select-option"))) {
    item.disabled = modelSelect.disabled;
    const selected = item.dataset.modelValue === modelSelect.value;
    item.setAttribute("aria-selected", String(selected));
    item.classList.toggle("selected", selected);
    const option = Array.from(modelSelect.options).find((option) => option.value === item.dataset.modelValue);
    const inUse = backend.running && !!backend.model && backend.model === option?.value;
    let inUseBadge = item.querySelector<HTMLElement>(".model-select-in-use");
    if (inUse && !inUseBadge) {
      inUseBadge = document.createElement("span");
      inUseBadge.className = "model-select-in-use";
      inUseBadge.textContent = "In use";
      item.querySelector(".model-select-option-title")?.appendChild(inUseBadge);
    } else if (!inUse && inUseBadge) {
      inUseBadge.remove();
    }
  }
}
function syncModelSwitchControl() {
  const needsSwitch = backend.running && !!backend.model && modelSelect.value !== backend.model;
  if (serverOperation === "starting") startButtonLabel.textContent = translateKey("msg.starting.bbe5fc3b", {}, getActiveLocale());
  else if (serverOperation === "stopping") startButtonLabel.textContent = translateKey("msg.stopping.bbe85741", {}, getActiveLocale());
  else startButtonLabel.textContent = backend.running ? (needsSwitch ? translateKey("msg.switch_model.7aa32f4a", {}, getActiveLocale()) : translateKey("msg.stop_server.cb7c1e8d", {}, getActiveLocale())) : translateKey("msg.start_server.f08a9813", {}, getActiveLocale());
  startButton.classList.toggle("is-model-switch", needsSwitch && serverOperation === null);
  startButton.setAttribute("aria-label", backend.running
    ? (needsSwitch
      ? translateKey("msg.switch_to_the_selected_transcription_model.29347c97", {}, getActiveLocale())
      : translateKey("msg.stop_the_local_transcription_server.4bb79a10", {}, getActiveLocale()))
    : translateKey("msg.start_the_local_transcription_server.746c013b", {}, getActiveLocale()));
  if (needsSwitch) {
    startButton.title = (modelDownloadStatus.active || modelStorageBusy)
      ? translateKey("msg.finish_model_downloads_and_storage_changes_before_switching_mode.addffeca", {}, getActiveLocale())
      : translateKey("msg.stop_the_active_server_and_start_the_selected_model_only_after_t.5b77c9a4", {}, getActiveLocale());
  } else {
    startButton.removeAttribute("title");
  }
}

function renderModelCacheSelection() {
  modelCacheSelectionList.replaceChildren();
  const options = Array.from(modelSelect.options);
  const cachedOptions = options.filter((option) => option.dataset.cacheState === "downloaded");
  const unknown = options.some((option) => !option.dataset.cacheState || option.dataset.cacheState === "unknown");
  if (cachedOptions.length === 0) {
    const empty = document.createElement("p");
    empty.className = "muted field-help";
    empty.textContent = translateKey(
      unknown ? "msg.cache_status_is_still_being_checked_or_could_not_be_verified_reo.54782a8e" : "msg.no_downloaded_rdt_connect_models_were_found_in_your_configured_s.85f21902",
      {},
      getActiveLocale(),
    );
    modelCacheSelectionList.appendChild(empty);
    renderModelCleanupButtons();
    return;
  }
  for (const option of cachedOptions) {
    const info = modelMenuInfo(option);
    const row = document.createElement("label");
    row.className = "model-cache-selection-item";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.modelValue = option.value;
    checkbox.checked = false;
    const copy = document.createElement("span");
    copy.className = "model-cache-selection-copy";
    const name = document.createElement("strong");
    name.textContent = translateKey("msg.label_message.e6a1feb4", { label: info.name, message: info.size }, getActiveLocale());
    const modelId = document.createElement("code");
    modelId.textContent = option.value;
    copy.append(name, modelId);
    row.append(checkbox, copy);
    modelCacheSelectionList.appendChild(row);
  }
  if (unknown) {
    const note = document.createElement("p");
    note.className = "muted field-help";
    note.textContent = translateKey("msg.some_cache_locations_could_not_be_verified_unknown_entries_are_n.7e20e7be", {}, getActiveLocale());
    modelCacheSelectionList.appendChild(note);
  }
  syncModelCacheSelectionLocks();
}

function getSelectedModelCacheIds(): string[] {
  return Array.from(modelCacheSelectionList.querySelectorAll<HTMLInputElement>('input[type="checkbox"][data-model-value]:checked'))
    .map((checkbox) => checkbox.dataset.modelValue || "")
    .filter(Boolean);
}

function syncModelCacheSelectionLocks() {
  for (const checkbox of Array.from(modelCacheSelectionList.querySelectorAll<HTMLInputElement>('input[type="checkbox"][data-model-value]'))) {
    const model = checkbox.dataset.modelValue || "";
    const active = backend.running && !!backend.model && model === backend.model;
    const activeUnknown = backend.running && !backend.model;
    const copy = checkbox.closest(".model-cache-selection-item")?.querySelector<HTMLElement>(".model-cache-selection-copy");
    let note = copy?.querySelector<HTMLElement>(".model-cache-active-note") || null;
    if (active && checkbox.checked) checkbox.checked = false;
    if (active && copy && !note) {
      note = document.createElement("small");
      note.className = "model-cache-active-note";
      note.textContent = translateKey("msg.in_use_protected_while_the_server_runs.f57b7714", {}, getActiveLocale());
      copy.appendChild(note);
    } else if (!active && note) {
      note.remove();
    }
    checkbox.disabled = modelDownloadStatus.active || modelStorageBusy || busy || active || activeUnknown;
    checkbox.closest(".model-cache-selection-item")?.classList.toggle("is-active-model-cache", active);
  }
  renderModelCleanupButtons();
}



function renderModelSelectMenu() {
  modelSelectMenu.replaceChildren();
  for (const option of Array.from(modelSelect.options)) {
    const info = modelMenuInfo(option);
    const item = document.createElement("button");
    item.type = "button";
    item.className = "model-select-option";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(option.value === modelSelect.value));
    item.tabIndex = -1;
    item.dataset.modelValue = option.value;
    item.dataset.modelTone = modelToneFor(option.value);
    const copy = document.createElement("span");
    copy.className = "model-select-option-copy";
    const title = document.createElement("span");
    title.className = "model-select-option-title";
    const name = document.createElement("strong");
    name.textContent = info.name;
    title.appendChild(name);
    if (backend.running && backend.model === option.value) {
      const inUse = document.createElement("span");
      inUse.className = "model-select-in-use";
      inUse.textContent = translateKey("msg.in_use.17a97caa", {}, getActiveLocale());
      title.appendChild(inUse);
    }
    if (info.recommended) {
      const recommend = document.createElement("span");
      recommend.className = "model-select-recommend";
      recommend.textContent = translateKey("msg.recommended.d70604e8", {}, getActiveLocale());
      title.appendChild(recommend);
    }
    const description = document.createElement("small");
    description.className = "model-select-option-description";
    description.textContent = info.description;
    copy.append(title, description);
    const meta = document.createElement("span");
    meta.className = "model-select-option-meta";
    const size = document.createElement("span");
    size.className = "model-select-option-size";
    size.textContent = info.size;
    meta.appendChild(size);
    if (option.dataset.cacheState === "downloaded") {
      const downloaded = document.createElement("span");
      downloaded.className = "model-select-downloaded";
      downloaded.textContent = translateKey("msg.downloaded.f0b0738f", {}, getActiveLocale());
      meta.appendChild(downloaded);
    }
    item.append(copy, meta);
    item.addEventListener("click", () => {
      if (modelSelect.disabled) return;
      modelSelect.value = option.value;
      modelSelect.dispatchEvent(new Event("change", { bubbles: true }));
      closeModelSelectMenu(false);
      modelSelectTrigger.focus();
    });
    modelSelectMenu.appendChild(item);
  }
  syncModelSelectPresentation();
}
function closeModelSelectMenu(restoreFocus: boolean) {
  modelSelectTooltip.classList.remove("open");
  modelSelectTrigger.setAttribute("aria-expanded", "false");
  modelSelectMenu.setAttribute("aria-hidden", "true");
  if (restoreFocus) modelSelectTrigger.focus();
}
function openModelSelectMenu() {
  if (modelSelectTrigger.disabled) return;
  modelSelectTooltip.classList.add("open");
  modelSelectTrigger.setAttribute("aria-expanded", "true");
  modelSelectMenu.setAttribute("aria-hidden", "false");
  window.requestAnimationFrame(() => {
    const selected = modelSelectMenu.querySelector<HTMLButtonElement>('.model-select-option[aria-selected="true"]');
    (selected || modelSelectMenu.querySelector<HTMLButtonElement>(".model-select-option"))?.focus();
  });
}
const modelStorageStatusInFlight = new Map<string, Promise<ModelStorageStatus>>();
function requestModelStorageStatus(model: string): Promise<ModelStorageStatus> {
  const existing = modelStorageStatusInFlight.get(model);
  if (existing) return existing;
  const request = invoke<ModelStorageStatus>("get_model_storage_status", { model });
  const tracked = request.finally(() => {
    if (modelStorageStatusInFlight.get(model) === tracked) {
      modelStorageStatusInFlight.delete(model);
    }
  });

  modelStorageStatusInFlight.set(model, tracked);
  return tracked;
}

function benchmarkModelSizeBytes(model: string): number {
  const label = approximateModelSizeLabel(model);
  const match = label.match(/([\d]+(?:\.[\d]+)?)\s*(B|KB|MB|GB|TB)\b/i);
  if (!match) return Number.MAX_SAFE_INTEGER;
  const units: Record<string, number> = {
    B: 1, KB: 1_000, MB: 1_000_000,
    GB: 1_000_000_000, TB: 1_000_000_000_000,
  };
  return Number(match[1]) * (units[match[2].toUpperCase()] || 1);
}

async function resolveBenchmarkModel(): Promise<{
  model: string; cache: ModelStorageStatus; usedFallback: boolean;
} | null> {
  const options = Array.from(modelSelect.options);
  const requested = modelSelect.value;
  const selectedOption = options.find(
    (option) => option.value === requested && option.value.trim().length > 0,
  ) ?? null;

  const verify = async (option: HTMLOptionElement): Promise<ModelStorageStatus> => {
    const status = await requestModelStorageStatus(option.value);
    if (status.model !== option.value) {
      throw new Error("The model-cache response did not match the requested model.");
    }
    option.dataset.cacheState = status.modelCached ? "downloaded" : "not-downloaded";
    const baseLabel = modelOptionBaseLabels.get(option.value) || option.value;
    option.textContent = status.modelCached
      ? translateKey("msg.baselabel_downloaded.bf85439f",
          { baseLabel }, getActiveLocale())
      : baseLabel;
    return status;
  };

  if (selectedOption) {
    let selectedCache: ModelStorageStatus;
    try {
      selectedCache = await verify(selectedOption);
    } catch (error) {
      showNotice(
        `Could not verify the selected model's downloaded files. ${formatHardwareDeviceText(String(error))}`,
        "error",
      );
      return null;
    }

    if (selectedCache.modelCached) {
      renderModelSelectMenu();
      renderModelCacheSelection();
      return { model: requested, cache: selectedCache, usedFallback: false };
    }
  }

  const candidates = options
    .filter((option) => option.value !== requested)
    .sort((a, b) =>
      benchmarkModelSizeBytes(a.value) - benchmarkModelSizeBytes(b.value)
      || a.value.localeCompare(b.value)
    );

  for (const option of candidates) {
    let status: ModelStorageStatus;
    try {
      status = await verify(option);
    } catch (error) {
      showNotice(
        `Could not verify whether ${friendlyModelName(option.value)} is downloaded. The benchmark cannot safely choose the smallest available model. ${formatHardwareDeviceText(String(error))}`,
        "error",
      );
      renderModelSelectMenu();
      renderModelCacheSelection();
      return null;
    }
    if (!status.modelCached) continue;

    modelSelect.value = option.value;
    modelSelect.dispatchEvent(new Event("change", { bubbles: true }));
    renderModelSelectMenu();
    renderModelCacheSelection();
    return { model: option.value, cache: status, usedFallback: true };
  }

  renderModelSelectMenu();
  renderModelCacheSelection();
  showNotice(
    "No downloaded model is available for the benchmark. Download at least one supported Whisper model, then try again.",
    "warning",
  );
  nudgeModelDownloadButton();
  return null;
}

let modelBadgeRefreshSerial = 0;
async function refreshModelCacheBadges() {
  const serial = ++modelBadgeRefreshSerial;
  const options = Array.from(modelSelect.options);
    const results: Array<{ value: string; cached: boolean | null }> = [];
  for (const option of options) {
    if (serial !== modelBadgeRefreshSerial) return;
    try {
      const status = await requestModelStorageStatus(option.value);
      results.push({ value: option.value, cached: Boolean(status.modelCached) });
    } catch {
      results.push({ value: option.value, cached: null });
    }
  }
  if (serial !== modelBadgeRefreshSerial) return;
  for (const result of results) {
    const option = options.find((candidate) => candidate.value === result.value);
    const baseLabel = modelOptionBaseLabels.get(result.value) || result.value;
    if (!option) continue;
    option.textContent = result.cached === true ? translateKey("msg.baselabel_downloaded.bf85439f", { baseLabel: baseLabel }, getActiveLocale()) : baseLabel;
    if (result.cached === true) option.dataset.cacheState = "downloaded";
    else if (result.cached === false) option.dataset.cacheState = "not-downloaded";
    else option.dataset.cacheState = "unknown";
  }
  renderModelSelectMenu();
  renderModelCacheSelection();
}
const modelStorageSummary = $("model-storage-summary");
const modelStorageLocationsEl = $("model-storage-locations");
const modelStorageAddButton = $("model-storage-add-button") as HTMLButtonElement;
const modelStorageSaveButton = $("model-storage-save-button") as HTMLButtonElement;
const modelDownloadPanel = $("model-download-panel");
const modelDownloadTitle = $("model-download-title");
const modelDownloadDetail = $("model-download-detail");
const modelDownloadButton = $("model-download-button") as HTMLButtonElement;
const modelDownloadCancelButton = $("model-download-cancel-button") as HTMLButtonElement;
const modelDownloadProgressWrap = $("model-download-progress-wrap");
const modelDownloadProgressPhase = $("model-download-progress-phase");
const modelDownloadProgressPercent = $("model-download-progress-percent");
const modelDownloadProgressTrack = $("model-download-progress-track");
const modelDownloadProgressBar = $("model-download-progress-bar");
const modelDownloadProgressMeta = $("model-download-progress-meta");
const modelDownloadStatusEl = $("model-download-status");
const modelCacheSelectionList = $("model-cache-selection-list");
const deleteRdtModelsButton = $("delete-rdt-models-button") as HTMLButtonElement;
const modelCleanupStatus = $("model-cleanup-status");
const startButton = $("start-button") as HTMLButtonElement;
const startButtonLabel = $("start-button-label");
const startProgress = $("start-progress");
const startProgressText = $("start-progress-text");
const startProgressDescription = $("start-progress-description");
const aboutCompanyLink = $("about-company-link") as HTMLAnchorElement;
const aboutYoutubeButton = $("about-youtube-button") as HTMLButtonElement;
const aboutBusinessContactLink = $("about-business-contact-link") as HTMLAnchorElement;
const openButton = $("open-button") as HTMLButtonElement;
const browserSelect = $("browser-select") as HTMLSelectElement;
const footerServerStatusDot = $("footer-server-status-dot");
const headerServerStatusDot = $("header-server-status-dot");
const setupRemoteButton = $("setup-remote") as HTMLButtonElement;
const generateQrButton = $("generate-qr") as HTMLButtonElement;
const copyLinkButton = $("copy-link") as HTMLButtonElement;
const qrImage = $("pairing-qr") as HTMLImageElement;
const qrPlaceholder = $("qr-placeholder");
const qrStatus = $("qr-status");
const qrCountdown = $("qr-countdown");
const pendingList = $("pending-list");
const deviceList = $("device-list");
const remoteState = $("remote-state");
const notice = $("notice");
const logPath = $("log-path");
const openLogButton = $("open-log-button") as HTMLButtonElement;
const copyLogButton = $("copy-log-button") as HTMLButtonElement;
const runDiagnosticsButton = $("run-diagnostics-button") as HTMLButtonElement;
const copyDiagnosticsButton = $("copy-diagnostics-button") as HTMLButtonElement;
const copyActivityButton = $("copy-activity-log-button") as HTMLButtonElement;
const diagnosticsOutput = $("diagnostics-output");
const benchmarkDiagnosticsRefreshButton = $("benchmark-diagnostics-refresh-button") as HTMLButtonElement;
const benchmarkDiagnosticsCopyButton = $("benchmark-diagnostics-copy-button") as HTMLButtonElement;
const benchmarkDiagnosticsOutput = $("benchmark-diagnostics-output");
const remoteRunDiagnosticsButton = $("remote-run-diagnostics-button") as HTMLButtonElement;
const remoteCopyDiagnosticsButton = $("remote-copy-diagnostics-button") as HTMLButtonElement;
const remoteDiagnosticsSummary = $("remote-diagnostics-summary");
const remoteDiagnosticsOutput = $("remote-diagnostics-output");

try {
  const savedModel = localStorage.getItem("rdt_voice_desktop_model");
  if (savedModel && Array.from(modelSelect.options).some((option) => option.value === savedModel)) {
    modelSelect.value = savedModel;
  }
} catch { /* Private browsing/storage policies should not block startup. */ }
renderModelSelectMenu();
void initializePlatformModelCatalog();
let preferredBrowserId = "default";
try { preferredBrowserId = localStorage.getItem("rdt_connect_preferred_browser") || "default"; } catch { /* optional preference */ }
modelSelect.addEventListener("change", () => {
  syncModelSelectPresentation();
  try { localStorage.setItem("rdt_voice_desktop_model", modelSelect.value); } catch { /* optional preference */ }
  modelIdDetail.textContent = modelSelect.value;

  void invoke("remember_recent_server_choice", { model: modelSelect.value, preferredPort: Number(preferredPortInput.value) || 8888 }).catch(() => undefined);
  void refreshModelStorageStatus();
  void refreshModelDownloadStatus();
  syncModelSwitchControl();
});

modelSelectTrigger.addEventListener("click", () => {
  if (modelSelectTrigger.getAttribute("aria-expanded") === "true") closeModelSelectMenu(false);
  else openModelSelectMenu();
});
modelSelectTrigger.addEventListener("keydown", (event: KeyboardEvent) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    openModelSelectMenu();
  }
});
modelSelectMenu.addEventListener("keydown", (event: KeyboardEvent) => {
  const items = Array.from(modelSelectMenu.querySelectorAll<HTMLButtonElement>(".model-select-option:not(:disabled)"));
  const current = event.target instanceof HTMLElement ? event.target.closest<HTMLButtonElement>(".model-select-option") : null;
  const index = current ? items.indexOf(current) : -1;
  let next = -1;
  if (event.key === "ArrowDown") next = Math.min(items.length - 1, index + 1);
  else if (event.key === "ArrowUp") next = Math.max(0, index - 1);
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = items.length - 1;
  else if (event.key === "Escape") { event.preventDefault(); closeModelSelectMenu(true); return; }
  else if (event.key === "Tab") { closeModelSelectMenu(false); return; }
  if (next >= 0 && items[next]) { event.preventDefault(); items[next].focus(); }
});
document.addEventListener("pointerdown", (event: PointerEvent) => {
  if (!modelSelectTooltip.contains(event.target as Node)) closeModelSelectMenu(false);
});
modelIdDetail.textContent = modelSelect.value;
try {
  const savedPortRaw = localStorage.getItem("rdt_connect_preferred_port");
  const savedPort = Number(savedPortRaw || "8888");
  // 8889 was a temporary development default. Migrate that legacy default to 8888.
  if (savedPort === 8889 || !savedPortRaw) {
    preferredPortInput.value = "8888";
    localStorage.setItem("rdt_connect_preferred_port", "8888");
  } else if (Number.isInteger(savedPort) && savedPort >= 1024 && savedPort <= 65535) {
    preferredPortInput.value = String(savedPort);
  }
} catch { /* optional preference */ }
void invoke("remember_recent_server_choice", { model: modelSelect.value, preferredPort: Number(preferredPortInput.value) || 8888 }).catch(() => undefined);
preferredPortInput.addEventListener("change", () => {
  const port = Number(preferredPortInput.value);
  if (Number.isInteger(port) && port >= 1024 && port <= 65535) {
    preferredPortInput.value = String(port);
    try { localStorage.setItem("rdt_connect_preferred_port", String(port)); } catch { /* optional preference */ }
    void invoke("remember_recent_server_choice", { model: modelSelect.value, preferredPort: port }).catch(() => undefined);
    displayPort.textContent = String(backend.port || port);
  } else {
    preferredPortInput.value = "8888";
    showNotice(translateKey("msg.choose_a_port_from_1024_through_65535.807c4a34", {}, getActiveLocale()), "warning");
  }
});

copyLogButton.addEventListener("click", async () => {
  copyLogButton.disabled = true;
  try {
    const text = await invoke<string>("get_recent_backend_log");
    if (!text.trim()) { showNotice(translateKey("msg.the_backend_log_is_empty_so_far.9915274c", {}, getActiveLocale()), "warning"); return; }
    await navigator.clipboard.writeText(text);
    showButtonFeedback(copyLogButton);
  } catch (error) {
    showNotice(translateKey("msg.could_not_copy_the_backend_log_error.78131054", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
  } finally { copyLogButton.disabled = false; }
});

function sanitizedActivityText(): string {
  const now = Date.now();
  const last100 = activityEntries.slice(-100);
  const lastTenMinutes = activityEntries.filter((entry) => entry.at >= now - 10 * 60 * 1000);
  const selected = lastTenMinutes.length > last100.length ? lastTenMinutes : last100;
  const safeLines = selected.map((entry) => {
    let message = entry.message;
    if (/\.ts\.net|\.tailscale\.net/i.test(message)) message = "[private remote-access endpoint redacted]";
    message = message.replace(/(authorization:\s*bearer\s+)[^\s,;]+/ig, "$1[REDACTED]");
    message = message.replace(/(\bbearer\s+)[^\s,;]+/ig, "$1[REDACTED]");
    message = message.replace(/((?:access_?token|token|api_?key|secret|password)\s*[=:]\s*)[^\s&,;]+/ig, "$1[REDACTED]");
    message = message.replace(/\/(?:Users|home)\/[^/\s]+/g, "~");
    if (/(?:transcript\s*[=:]|audio\s*[=:]|payload\s*[=:])/i.test(message)) message = "[possible audio/transcript payload redacted]";
    return `${new Date(entry.at).toLocaleTimeString()} [${entry.kind.toUpperCase()}] ${message}`;
  });
  return ["RDT Connect in-app activity (bounded recent window)", ...safeLines].join("\n");
}
type TailscaleProbeDiagnostic = {
  command?: string; state?: string; exit_code?: number | null; elapsed_ms?: number;
  stdout_bytes?: number; stderr_bytes?: number; json_parsed?: boolean;
  root_keys?: string[]; timeout_seconds?: number; version?: string;
};
type RemoteAccessDiagnostic = {
  schema_version?: number; read_only?: boolean; configuration_changed?: boolean;
  cli?: { found?: boolean; source?: string; binary_name?: string; version?: string | null };
  cli_candidates?: Array<{ source?: string; state?: string; exit_code?: number | null; elapsed_ms?: number; stdout_bytes?: number; stderr_bytes?: number; json_parsed?: boolean }>;
  probes?: { version?: TailscaleProbeDiagnostic; status_json?: TailscaleProbeDiagnostic; serve_status_json?: TailscaleProbeDiagnostic };
  summary?: { backend_port?: number; backend_transport?: string; tailscale_online?: boolean | null; dns_name_present?: boolean | null; dns_suffix_valid?: boolean | null; web_listener_count?: number | null; tcp_listener_count?: number | null; matching_route_for_this_backend?: boolean | null; route_state?: string; route_https_port?: number | null };
};
function probeLine(label: string, probe?: TailscaleProbeDiagnostic): string {
  if (!probe) return `${label}: NOT AVAILABLE`;
  const state = String(probe.state || "unknown").toUpperCase();
  const exit = probe.exit_code === null || probe.exit_code === undefined ? "n/a" : String(probe.exit_code);
  const timing = Number.isFinite(probe.elapsed_ms) ? `; ${probe.elapsed_ms} ms` : "";
  const sizes = `; stdout ${probe.stdout_bytes ?? "?"} B; stderr ${probe.stderr_bytes ?? "?"} B`;
  const json = probe.json_parsed === undefined ? "" : `; JSON ${probe.json_parsed ? "parsed" : "not parsed"}`;
  const keys = probe.root_keys?.length ? `; keys: ${probe.root_keys.join(", ")}` : "";
  const version = probe.version ? `; version ${probe.version}` : "";
  return `${label}: ${state} (exit ${exit}${timing}${sizes}${json}${version}${keys})`;
}
function formatRemoteAccessDiagnostic(value: RemoteAccessDiagnostic): { summary: string; report: string } {
  const cli = value.cli || {};
  const probes = value.probes || {};
  const info = value.summary || {};
  const route = info.route_state || "not evaluated";
  const candidates = Array.isArray(value.cli_candidates) ? value.cli_candidates : [];
  const candidateLine = candidates.length
    ? `CLI candidate checks: ${candidates.map((candidate) => `${candidate.source || "candidate"}=${String(candidate.state || "unknown")} (exit ${candidate.exit_code ?? "n/a"}; ${candidate.elapsed_ms ?? "?"} ms; stdout ${candidate.stdout_bytes ?? "?"} B; stderr ${candidate.stderr_bytes ?? "?"} B; JSON ${candidate.json_parsed ? "yes" : "no"})`).join(" | ")}`
    : "CLI candidate checks: unavailable";
  const summary = [
    `Tailscale CLI: ${cli.found ? `FOUND (${cli.source || "source unknown"})${cli.version ? ` · ${cli.version}` : ""}` : "NO WORKING CLI CANDIDATE"}`,
    candidateLine,
    probeLine("tailscale status --json", probes.status_json),
    probeLine("tailscale serve status --json", probes.serve_status_json),
    `Tailscale online: ${info.tailscale_online === true ? "YES" : info.tailscale_online === false ? "NO" : "UNKNOWN"}; DNS name present: ${info.dns_name_present === true ? "YES" : info.dns_name_present === false ? "NO" : "UNKNOWN"}; expected suffix valid: ${info.dns_suffix_valid === true ? "YES" : info.dns_suffix_valid === false ? "NO" : "UNKNOWN"}`,
    `Active backend upstream: ${info.backend_transport || "unknown"} on port ${info.backend_port ?? "unknown"}; Serve route: ${route}${info.route_https_port ? ` (HTTPS port ${info.route_https_port})` : ""}`,
    `Read-only: ${value.read_only === true ? "YES" : "UNKNOWN"}; Tailscale configuration changed: ${value.configuration_changed === false ? "NO" : "UNKNOWN"}`
  ].join("\n");
  const report = [
    summary,
    "",
    "Structured Tailscale diagnostic payload (private hostnames, IPs, tokens, and raw CLI output are intentionally omitted):",
    JSON.stringify(value, null, 2)
  ].join("\n");
  return { summary, report };
}
async function loadDiagnostics(copyReport = false, source: "developer" | "remote" = "developer") {
  if (!copyReport) showNotice(translateKey("msg.running_read_only_diagnostics.03caa55d", {}, getActiveLocale()), "info");
  const diagnosticButtons = [runDiagnosticsButton, copyDiagnosticsButton, remoteRunDiagnosticsButton, remoteCopyDiagnosticsButton];
  const sourceCopyButton = source === "remote" ? remoteCopyDiagnosticsButton : copyDiagnosticsButton;
  diagnosticButtons.forEach((button) => { button.disabled = true; });
  try {
    const report = await invoke<string>("get_app_diagnostics", { preferredPort: Number(preferredPortInput.value) || 8888 });
    let remoteReport = "NOT RUN — the local backend is stopped, so its Tailscale CLI cannot be probed through the authenticated local API.";
    let remoteSummary = "Tailscale diagnostics not run: start the local server first.";
    if (backend.running) {
      try {
        const remote = await invoke<RemoteAccessDiagnostic>("get_remote_access_diagnostics");
        const formatted = formatRemoteAccessDiagnostic(remote);
        remoteReport = formatted.report;
        remoteSummary = formatted.summary;
      } catch (error) {
        const reason = formatHardwareDeviceText(String(error)).replace(/(authorization:\s*bearer\s+|\bbearer\s+)[^\s,;]+/ig, "$1[REDACTED]").replace(/\/(?:Users|home)\/[^/\s]+/g, "~");
        if (/\b404\b|not found/i.test(reason)) {
          remoteSummary = "Tailscale diagnostic endpoint is missing (HTTP 404). This backend bundle may predate the diagnostic update; prepare resources from the current source and rebuild.";
        } else {
          remoteSummary = `Tailscale diagnostics request failed: ${reason}`;
        }
        remoteReport = `${remoteSummary}\nNo raw CLI output, hostnames, or tokens are included in this panel.`;
      }
    }
    const completeReport = `${report}\n\n===== TAILSCALE CLI / SERVE DIAGNOSTICS (read-only) =====\n${remoteReport}`;
    diagnosticsOutput.textContent = completeReport;
    remoteDiagnosticsSummary.textContent = remoteSummary;
    remoteDiagnosticsOutput.textContent = remoteReport;
    if (copyReport) {
      const [backendLogResult] = await Promise.allSettled([invoke<string>("get_recent_backend_log")]);
      const backendLog = backendLogResult.status === "fulfilled" ? backendLogResult.value : `[Backend log unavailable: ${String(backendLogResult.reason).replace(/\bBearer\s+[^\s,;]+/ig, "Bearer [REDACTED]")}]`;
      const bundle = [
        "RDT CONNECT — AI TROUBLESHOOTING REPORT",
        `Generated: ${new Date().toISOString()}`,
        "Review before sharing. This report intentionally omits token values, private Tailscale hostnames/IPs, raw CLI output, private keys, audio, and transcripts.",
        "\n===== 1. APP / BACKEND DIAGNOSTICS =====\n", completeReport,
        "\n===== 2. RECENT APP ACTIVITY (sanitized) =====\n", sanitizedActivityText(),
        "\n===== 3. RECENT BACKEND LOG (sanitized/bounded) =====\n", backendLog
      ].join("\n");
      await navigator.clipboard.writeText(bundle);
      showButtonFeedback(sourceCopyButton, "Report copied");
      showNotice(translateKey("msg.troubleshooting_report_copied_review_it_for.5694f2b3", {}, getActiveLocale()), "good");
    } else {
      showNotice(translateKey("msg.diagnostics_complete_no_server_was_started_o.c96707fb", {}, getActiveLocale()), "good");
    }
  } catch (error) {
    showNotice(translateKey("msg.diagnostics_could_not_complete_redacted.14a83a85", { error: formatHardwareDeviceText(String(error)).replace(/\bBearer\s+[^\s,;]+/ig, "Bearer [REDACTED]") }, getActiveLocale()), "error");
  } finally {
    diagnosticButtons.forEach((button) => { button.disabled = false; });
  }
}

async function refreshBenchmarkDiagnostics(copyReport = false): Promise<void> {
  benchmarkDiagnosticsRefreshButton.disabled = true;
  benchmarkDiagnosticsCopyButton.disabled = true;
  try {
    const report = await invoke<{ schema?: string; attempts?: unknown[]; privacy?: Record<string, boolean> }>(
      "get_native_benchmark_diagnostics"
    );
    const attempts = Array.isArray(report?.attempts) ? report.attempts : [];
    const serialized = JSON.stringify(report, null, 2);
    benchmarkDiagnosticsOutput.textContent = attempts.length
      ? serialized
      : translateKey("msg.no_benchmark_attempts_recorded.1d30", {}, getActiveLocale());
    if (copyReport) {
      await navigator.clipboard.writeText(serialized);
      showNotice(translateKey("msg.copied.8d525e5f", {}, getActiveLocale()), "good");
    }
  } catch {
    benchmarkDiagnosticsOutput.textContent = translateKey(
      "msg.diagnostics_could_not_complete_redacted.14a83a85",
      { error: "Benchmark diagnostics are unavailable." },
      getActiveLocale()
    );
    if (copyReport) showNotice(benchmarkDiagnosticsOutput.textContent, "error");
  } finally {
    benchmarkDiagnosticsRefreshButton.disabled = false;
    benchmarkDiagnosticsCopyButton.disabled = false;
  }
}
benchmarkDiagnosticsRefreshButton.addEventListener("click", () => void refreshBenchmarkDiagnostics());
benchmarkDiagnosticsCopyButton.addEventListener("click", () => void refreshBenchmarkDiagnostics(true));
void refreshBenchmarkDiagnostics();

runDiagnosticsButton.addEventListener("click", () => void loadDiagnostics(false));
copyDiagnosticsButton.addEventListener("click", () => void loadDiagnostics(true));
remoteRunDiagnosticsButton.addEventListener("click", () => void loadDiagnostics(false, "remote"));
remoteCopyDiagnosticsButton.addEventListener("click", () => void loadDiagnostics(true, "remote"));
copyActivityButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(sanitizedActivityText());
    showButtonFeedback(copyActivityButton);
  } catch (error) { showNotice(translateKey("msg.could_not_copy_activity_error.9bc99bf3", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
});

copyEndpointButton.addEventListener("click", async () => {
  if (!backend.localUrl) return;
  try {
    await navigator.clipboard.writeText(backend.localUrl);
    showButtonFeedback(copyEndpointButton);
  } catch (error) { showNotice(translateKey("msg.could_not_copy_address_error.5ef57c80", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
});
copyRemoteUrlButton.addEventListener("click", async () => {
  const url = remoteUrlValue.textContent?.trim() || "";
  if (!url || !/^https:\/\//i.test(url)) return;
  try {
    await navigator.clipboard.writeText(url);
    showButtonFeedback(copyRemoteUrlButton, translateKey("msg.copied.8d525e5f", {}, getActiveLocale()));
    showNotice(translateKey("msg.private_https_url_copied_it_is_only_reachabl.56e52882", {}, getActiveLocale()), "good");
  } catch (error) { showNotice(translateKey("msg.could_not_copy_the_private_https_url_error.7b31d0ea", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
});

async function readServerToken(): Promise<string> {
  const token = await invoke<string>("get_server_token");
  if (!token || token.trim().length < 32) throw new Error("RDT Connect did not return a valid access token. Start the server once, then try again.");
  return token.trim();
}
toggleTokenButton.addEventListener("click", async () => {
  if (tokenDisplay.type === "text") {
    tokenDisplay.value = backend.running ? "••••••••••••••" : "";
    tokenDisplay.type = "password";
    toggleTokenButton.textContent = translateKey("msg.show.0df6f1ca", {}, getActiveLocale());
    toggleTokenButton.setAttribute("aria-label", translateKey("msg.reveal_the_access_token.c51590dd", {}, getActiveLocale()));
    return;
  }
  toggleTokenButton.disabled = true;
  try {
    tokenDisplay.value = await readServerToken();
    tokenDisplay.type = "text";
    toggleTokenButton.textContent = translateKey("msg.hide.ac20a57b", {}, getActiveLocale());
    toggleTokenButton.setAttribute("aria-label", translateKey("msg.hide_the_access_token.9d4c7fa3", {}, getActiveLocale()));
  } catch (error) {
    showNotice(translateKey("msg.could_not_reveal_the_access_token_error.12941737", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
  } finally { toggleTokenButton.disabled = false; }
});
copyTokenButton.addEventListener("click", async () => {
  copyTokenButton.disabled = true;
  try {
    const token = await readServerToken();
    await navigator.clipboard.writeText(token);
    showButtonFeedback(copyTokenButton, translateKey("msg.copied.8d525e5f", {}, getActiveLocale()));
    showNotice(translateKey("msg.access_token_copied_paste_it_into_rdt_voice.3df18578", {}, getActiveLocale()), "good");
  } catch (error) {
    showNotice(translateKey("msg.could_not_copy_the_access_token_error.9674c325", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
  } finally { copyTokenButton.disabled = false; }
});

openLogButton.addEventListener("click", async () => {
  openLogButton.disabled = true;
  try {
    await invoke("open_backend_log");
    showButtonFeedback(openLogButton, "Opened");
  } catch (error) {
    showNotice(translateKey("msg.couldn_t_open_the_backend_log_error.52d916e5", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
  } finally {
    openLogButton.disabled = false;
  }
});

let lastConnectionCheck: BackendConnectionCheck | null = null;

/* RDT_INFERENCE_DEVICE_UI_BEGIN */
type InferenceDevicePreference = "auto" | "gpu" | "cpu";

const inferenceDeviceOptions = $("inference-device-options") as HTMLFieldSetElement;
const inferenceDeviceRadios = Array.from(
  inferenceDeviceOptions.querySelectorAll<HTMLInputElement>('input[name="inference-device"]'),
);
const inferenceDeviceStatus = $("inference-device-status");
const inferenceDeviceNote = $("inference-device-note");
const inferenceDeviceAutoButton = $("inference-device-auto-button") as HTMLButtonElement;

let inferenceDevicePreference: InferenceDevicePreference = "auto";
let appliedInferenceDevicePreference: InferenceDevicePreference | null = null;

function syncModelRecommendation(): void {
  const provider = lastConnectionCheck?.inferenceProvider?.provider?.toLowerCase() || "";
  const gpuConfirmed = /gpu|metal|cuda|vulkan|opencl|mps/.test(provider);
  const cpuConfirmed = /cpu/.test(provider);
  const useGpu = !cpuConfirmed &&
    (gpuConfirmed || inferenceDevicePreference === "gpu");
  recommendedModelTone = useGpu ? "base" : "tiny";
  renderModelSelectMenu();
}

let inferenceDevicePreferenceReadError: string | null = null;



inferenceDeviceOptions.disabled = true;

let inferenceDevicePreferenceSaving = false;

function validInferenceDevice(value: string): value is InferenceDevicePreference {
  return value === "auto" || value === "gpu" || value === "cpu";
}

function syncInferenceDeviceRadios(): void {
  for (const radio of inferenceDeviceRadios) {
    radio.checked = radio.value === inferenceDevicePreference;
  }
  inferenceDeviceAutoButton.hidden = inferenceDevicePreference === "auto";
}

function renderInferenceHardwareStatus(): void {
  inferenceDeviceOptions.disabled = serverOperation !== null || inferenceDevicePreferenceSaving;

  const appliedPreferenceUnknown =
    backend.running &&
    appliedInferenceDevicePreference === null &&
    !inferenceDevicePreferenceReadError;
  const pendingRestart =
    backend.running &&
    appliedInferenceDevicePreference !== null &&
    appliedInferenceDevicePreference !== inferenceDevicePreference;

  if (inferenceDevicePreferenceReadError) {
    inferenceDeviceStatus.hidden = false;
    inferenceDeviceStatus.textContent =
      translateKey("msg.hardware_preference_read_failed.1d20", { error: inferenceDevicePreferenceReadError }, getActiveLocale());
  } else if (pendingRestart) {
    inferenceDeviceStatus.hidden = false;
    inferenceDeviceStatus.removeAttribute("title");
    inferenceDeviceStatus.textContent =
      translateKey("msg.hardware_preferences_next_start_tooltip.1d20", {}, getActiveLocale());
  } else {
    inferenceDeviceStatus.hidden = true;
    inferenceDeviceStatus.textContent = "";
    inferenceDeviceStatus.removeAttribute("title");
  }

  inferenceDeviceNote.textContent = backend.running
    ? (appliedPreferenceUnknown
      ? translateKey("msg.hardware_preferences_next_start_tooltip.1d20", {}, getActiveLocale())
      : "")
    : translateKey("msg.hardware_choices_next_start_tooltip.1d20", {}, getActiveLocale());

  const hardwareCard = inferenceDeviceOptions.closest<HTMLElement>(".inference-device-card");
  if (hardwareCard) {
    hardwareCard.title = backend.running
      ? benchmarkMessage("msg.hardware_preferences_next_start_tooltip.1d20")
      : benchmarkMessage("msg.hardware_choices_next_start_tooltip.1d20");
    hardwareCard.setAttribute("data-tooltip", hardwareCard.title);
  }

  syncInferenceDeviceRadios();
  inferenceDeviceStatus.classList.toggle("is-next-start", pendingRestart && !inferenceDevicePreferenceReadError);
  inferenceDeviceStatus.classList.toggle("hardware-selection-next-start-warning", pendingRestart && !inferenceDevicePreferenceReadError);
  const showHardwareStatus = Boolean(inferenceDevicePreferenceReadError) || pendingRestart;
  inferenceDeviceStatus.hidden = !showHardwareStatus;
  inferenceDeviceStatus.classList.toggle("hidden", !showHardwareStatus);
}

async function saveInferenceDevicePreference(next: InferenceDevicePreference): Promise<void> {
  if (next === inferenceDevicePreference) return;

  if (next === "cpu" && !window.confirm(
    benchmarkMessage("msg.hardware_cpu_warning.1d20"),
  )) {
    syncInferenceDeviceRadios();
    return;
  }

  inferenceDevicePreferenceSaving = true;
  inferenceDeviceOptions.disabled = true;
  inferenceDeviceStatus.textContent = translateKey("msg.hardware_preference_saving.1d20", {}, getActiveLocale());

  try {
    const saved = await invoke<string>("set_inference_device_preference", { device: next });
    if (!validInferenceDevice(saved)) throw new Error(benchmarkMessage("msg.hardware_preference_invalid.1d20"));
    inferenceDevicePreference = saved;
    inferenceDevicePreferenceReadError = null;
    renderInferenceHardwareStatus();
    syncModelRecommendation();
  } catch (error) {
    inferenceDeviceStatus.textContent = `Could not save the setting; the previous choice is preserved. ${formatHardwareDeviceText(String(error))}`;
    syncInferenceDeviceRadios();
  } finally {
    inferenceDevicePreferenceSaving = false;
    inferenceDeviceOptions.disabled = serverOperation !== null || inferenceDevicePreferenceSaving;
    renderInferenceHardwareStatus();
  }
}

for (const radio of inferenceDeviceRadios) {
  radio.addEventListener("change", () => {
    if (radio.checked && validInferenceDevice(radio.value)) {
      void saveInferenceDevicePreference(radio.value);
    }
  });
}

inferenceDeviceAutoButton.addEventListener("click", () => {
  void saveInferenceDevicePreference("auto");
});

const inferenceDevicePreferenceReady = invoke<string>("get_inference_device_preference")
  .then((saved) => {
    if (!validInferenceDevice(saved)) throw new Error(benchmarkMessage("msg.hardware_preference_invalid.1d20"));
    inferenceDevicePreference = saved;
  })
  .catch((error) => {
    inferenceDevicePreferenceReadError = String(error);
  })
  .finally(() => {
    syncInferenceDeviceRadios();
    inferenceDeviceOptions.disabled = serverOperation !== null || inferenceDevicePreferenceSaving;
    renderInferenceHardwareStatus();
    syncModelRecommendation();
  });
/* RDT_INFERENCE_DEVICE_UI_END */
type ActivityEntry = { at: number; kind: string; message: string };
const activityEntries: ActivityEntry[] = [];
let tailscale: TailscaleStatus = {};
let pairingQr: PairingQr | null = null;
let pairingQrConsumed = false;
let knownPendingRequestIds = new Set<string>();
let remotePoll: number | null = null;
let busy = false;
function pairingQrUsable() {
  return Boolean(pairingQr && !pairingQrConsumed && Date.now() / 1000 < pairingQr.expiresAt);
}

// Browser choices are discovered by the native layer and persisted as a preference.
// If discovery fails, the pre-existing system-default route remains available.
browserSelect.addEventListener("change", () => {
  preferredBrowserId = browserSelect.value || "default";
  try {
    localStorage.setItem("rdt_connect_preferred_browser", preferredBrowserId);
  } catch {
    /* optional preference */
  }
  void invoke("set_preferred_browser", {
    browserId: preferredBrowserId
  }).catch(() => undefined);
});
async function loadAvailableBrowsers() {
  try {
    const options = await invoke<BrowserOption[]>("list_available_browsers");
    if (!Array.isArray(options) || !options.some((option) => option.id === "default")) return;
    browserSelect.replaceChildren();
    for (const browser of options) {
      const option = document.createElement("option");
      option.value = browser.id;
      option.textContent = browser.name;
      browserSelect.appendChild(option);
    }
    const nativePreference =
      await invoke<string | null>("get_preferred_browser").catch(() => null);

    const requestedBrowser = nativePreference || preferredBrowserId;

    browserSelect.value = options.some(
      (option) => option.id === requestedBrowser
    ) ? requestedBrowser : "default";

    preferredBrowserId = browserSelect.value;

    try {
      localStorage.setItem(
        "rdt_connect_preferred_browser",
        preferredBrowserId
      );
    } catch {
      /* optional preference */
    }

    await invoke("set_preferred_browser", {
      browserId: preferredBrowserId
    }).catch(() => undefined);
  } catch {
    // Keep the static System Default Browser option if native discovery is unavailable.
    browserSelect.title = translateKey("msg.browser_discovery_is_unavailable_the_system.ca8d09f1", {}, getActiveLocale());
  }
}
void loadAvailableBrowsers();

function ensureSharedFooterPlacement(): void {
  const appShell = document.querySelector<HTMLElement>("main.app-shell");
  const footer = document.querySelector<HTMLElement>("footer.foot");
  if (!appShell || !footer || footer.parentElement === appShell) return;

  const qrOverlay = document.getElementById("qr-expanded-overlay");
  if (qrOverlay?.parentElement === appShell) {
    appShell.insertBefore(footer, qrOverlay);
  } else {
    appShell.appendChild(footer);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", ensureSharedFooterPlacement, { once: true });
} else {
  ensureSharedFooterPlacement();
}

function renderFooterServerStatusDot() {
  const state = serverOperation === "starting" || serverOperation === "stopping"
    ? "processing"
    : backend.running ? "running" : "stopped";
  footerServerStatusDot.className = `foot-dot ${state}`;
  const label = state === "running" ? translateKey("msg.footer_server_running.1d20", {}, getActiveLocale()) : state === "processing" ? translateKey("msg.footer_server_processing.1d20", {}, getActiveLocale()) : translateKey("msg.footer_server_stopped.1d20", {}, getActiveLocale());
  footerServerStatusDot.setAttribute("title", label);
  footerServerStatusDot.setAttribute("aria-label", label);
  headerServerStatusDot.className = `header-server-status-dot ${state}`;
  headerServerStatusDot.setAttribute("title", label);
  headerServerStatusDot.setAttribute("aria-label", label);
}

// A27 authenticated browser-open preflight: Open stays disabled until the current
// managed backend passes both native /health and authenticated API checks.
function browserPreflightPassed(): boolean {
  return Boolean(
    backend.running &&
    lastConnectionCheck &&
    lastConnectionCheck.running &&
    lastConnectionCheck.pid === backend.pid &&
    lastConnectionCheck.port === backend.port &&
    lastConnectionCheck.healthOk === true &&
    lastConnectionCheck.authOk === true
  );
}
function browserPreflightFailure(): string {
  if (!backend.running) return translateKey("msg.browser_server_stopped.1d20", {}, getActiveLocale());
  const check = lastConnectionCheck;
  if (!check || !check.running || check.pid !== backend.pid || check.port !== backend.port)
    return translateKey("msg.browser_server_not_verified.1d20", {}, getActiveLocale());
  if (!check.healthOk)
    return translateKey("msg.browser_health_check_failed.1d20", { message: check.message || translateKey("msg.browser_health_check_retry_hint.1d20", {}, getActiveLocale()) }, getActiveLocale());
  if (check.authOk !== true) {
    if (check.authHttpStatus === 401)
      return translateKey("msg.browser_auth_401.1d20", {}, getActiveLocale());
    if (check.authHttpStatus === 403)
      return translateKey("msg.browser_auth_403.1d20", {}, getActiveLocale());
    return translateKey("msg.browser_auth_check_failed.1d20", { message: check.message || translateKey("msg.browser_auth_retry_hint.1d20", {}, getActiveLocale()) }, getActiveLocale());
  }
  return "The current server has not passed its health and authentication check yet.";
}
let serverOperation: "starting" | "stopping" | null = null;
let startupReady = false;
let conflictingBackendPresent = false;
const autoCleanupAttemptedPids = new Set<number>();
let autoCleanupFailureAnnounced = false;

function conciseNotice(message: string, kind: "info" | "good" | "error" | "warning"): string {
  if (developerOptionsEnabled) return message;
  if (kind === "error") return translateKey("msg.server_attention_needed.1d20", {}, getActiveLocale());
  if (/started on|backend is ready|server started/i.test(message)) return translateKey("msg.server_started_summary.1d20", {}, getActiveLocale());
  if (/stopped and verified|server stopped|previous server stopped/i.test(message)) return translateKey("msg.server_stopped_summary.1d20", {}, getActiveLocale());
  if (/connection.*(pass|ready|healthy)|authenticated API/i.test(message)) return translateKey("msg.connection_checked_summary.1d20", {}, getActiveLocale());
  if (/could not inspect|diagnostics/i.test(message)) return translateKey("msg.server_check_complete_summary.1d20", {}, getActiveLocale());
  if (message.length > 115) return message.slice(0, 112).trimEnd() + "…";
  return message;
}
function showNotice(message: string, kind: "info" | "good" | "error" | "warning" = "info") {
  const displayMessage = formatHardwareDeviceText(message);
  notice.textContent = conciseNotice(displayMessage, kind);
  notice.className = `notice ${kind}`;
  notice.setAttribute("data-state", kind);
  const detail = document.getElementById("developer-latest-detail");
  if (detail) detail.textContent = displayMessage;
  activityEntries.push({ at: Date.now(), kind, message: displayMessage });
  if (activityEntries.length > 500) activityEntries.splice(0, activityEntries.length - 500);
}

void listenNative<string>("rdt-backend-state-changed", (event) => {
  void (async () => {
    await refreshBackendStatus();
    await refreshOrphanedBackendStatus();
    if (event.payload === "stopped" && !backend.running) {
      showNotice(translateKey("msg.server_stopped_from_the_menu_bar.058995fe", {}, getActiveLocale()), "good");
    }
    renderBackendStatus();
  })();
});

// Menu-bar Start reuses the same native backend checks; bring the window forward if user action is needed.
void listenNative<string>("rdt-tray-start-failed", (event) => {
  setAppView("local");
  showNotice(event.payload, "error");
});
void listenNative<string>("rdt-tray-open-voice-failed", (event) => {
  setAppView("local");
  showNotice(event.payload, "error");
});
void listenNative<string>("rdt-tray-model-needs-download", (event) => {
  const requestedModel = event.payload;
  if (Array.from(modelSelect.options).some((option) => option.value === requestedModel)) {
    modelSelect.value = requestedModel;
    syncModelSelectPresentation();
    void refreshModelStorageStatus();
  }
  setAppView("local");
  showNotice(translateKey("msg.this_model_isn_t_downloaded_yet_select_downl.396f9fd2", {}, getActiveLocale()), "warning");
});

// Quiet success feedback for routine actions: change only the clicked button briefly.
// Serious operations/errors continue to use the notice area and persistent status panel.
const buttonFeedbackTimers = new WeakMap<HTMLButtonElement, number>();
function showButtonFeedback(button: HTMLButtonElement, label = translateKey("msg.copied.8d525e5f", {}, getActiveLocale())) {
  const oldTimer = buttonFeedbackTimers.get(button);
  if (oldTimer !== undefined) window.clearTimeout(oldTimer);
  const originalHtml = button.dataset.feedbackOriginalHtml ?? button.innerHTML;
  const originalAria = button.dataset.feedbackOriginalAria;
  button.dataset.feedbackOriginalHtml = originalHtml;
  if (originalAria === undefined) button.dataset.feedbackOriginalAria = button.getAttribute("aria-label") || "";
  button.innerHTML = `<span class="rdt-button-inline-feedback">✓ ${label}</span>`;
  button.classList.add("rdt-inline-success");
  button.setAttribute("aria-label", label);
  const timer = window.setTimeout(() => {
    button.innerHTML = button.dataset.feedbackOriginalHtml || originalHtml;
    button.classList.remove("rdt-inline-success");
    const restoreAria = button.dataset.feedbackOriginalAria || "";
    if (restoreAria) button.setAttribute("aria-label", restoreAria); else button.removeAttribute("aria-label");
    delete button.dataset.feedbackOriginalHtml;
    delete button.dataset.feedbackOriginalAria;
    buttonFeedbackTimers.delete(button);
  }, 950);
  buttonFeedbackTimers.set(button, timer);
}


// Close the version-number popover when clicking elsewhere or pressing Escape.
document.addEventListener("click", (event) => {
  const target = event.target;
  if (versionDetails?.open && target instanceof Node && !versionDetails.contains(target)) {
    versionDetails.open = false;
  }
});
document.addEventListener("keydown", (event: KeyboardEvent) => {
  if (event.key === "Escape" && versionDetails?.open) versionDetails.open = false;
});


// RDT Voice Easter Egg — faithful canvas port of its six-burst fireworks.
(() => {
  type FireworkBurst = { x: number; y: number; delay: number };
  type Ember = {
    cx: number; cy: number; x: number; y: number;
    vx: number; vy: number; hue: number; birth: number;
    life: number; trail: Array<{ x: number; y: number }>;
  };
  type Ignition = {
    cx: number; cy: number; birth: number; hue: number; life: number;
  };

  let active = false;
  let activationPending = false;
  let keyBuffer = "";
  let lastKeyAt = 0;
  let finishTimer = 0;
  let rafId = 0;
  let resizeHandler: (() => void) | null = null;

  const greetings: Record<string, string> = {
    rdt: "Congratulations! You found this RDT Easter Egg!",
    firework: "Congratulations! You found this Fireworks Easter Egg!",
    easteregg: "Congratulations! You found this Easter Egg!"
  };

  async function activateFireworks(alias: string): Promise<void> {
    if (
      active ||
      activationPending ||
      !Object.prototype.hasOwnProperty.call(greetings, alias)
    ) {
      return;
    }

    activationPending = true;

    try {
      const result = await invoke<{
        firstThisSession: boolean;
        lifetimeCount: number;
      }>("record_fireworks_activation");

      const first = result.firstThisSession === true;
      const count = Number(result.lifetimeCount);

      if (!Number.isSafeInteger(count) || count < 1) {
        throw new Error("Invalid fireworks lifetime count");
      }

      launchFireworks(
        first ? greetings[alias] : `#${count}`,
        !first
      );
    } catch (error) {
      console.error(
        "[RDT Connect] Could not record fireworks activation:",
        error
      );
    } finally {
      activationPending = false;
    }
  }

  function launchFireworks(
    greeting: string,
    randomPosition = false
  ): void {
    if (active) return;
    active = true;

    const canvas = document.createElement("canvas");
    canvas.id = "rdt-connect-developer-fireworks";
    canvas.className = "rdt-connect-easter-egg-canvas";
    canvas.setAttribute("aria-hidden", "true");

    const layer = document.createElement("div");
    layer.className = "rdt-connect-easter-egg-layer";
    layer.setAttribute("aria-live", "polite");
    layer.setAttribute("aria-atomic", "true");

    const message = document.createElement("div");
    message.className = "rdt-connect-easter-egg-message";
    message.textContent = greeting;
    layer.appendChild(message);

    document.body.append(canvas, layer);
    if (randomPosition) {
      const rect = message.getBoundingClientRect();
      const edge = 24;

      const maxLeft = Math.max(
        edge,
        window.innerWidth - rect.width - edge
      );

      const maxTop = Math.max(
        edge,
        window.innerHeight - rect.height - edge
      );

      message.style.position = "absolute";
      message.style.left =
        `${edge + Math.random() * Math.max(0, maxLeft - edge)}px`;
      message.style.top =
        `${edge + Math.random() * Math.max(0, maxTop - edge)}px`;
    }

    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const ctx = canvas.getContext("2d");

    const cleanup = () => {
      if (rafId) cancelAnimationFrame(rafId);
      if (finishTimer) window.clearTimeout(finishTimer);
      if (resizeHandler) window.removeEventListener("resize", resizeHandler);
      canvas.remove();
      layer.remove();
      active = false;
    };

    if (reducedMotion || !ctx) {
      finishTimer = window.setTimeout(cleanup, 2200);
      return;
    }

    let width = window.innerWidth;
    let height = window.innerHeight;
    let dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));

    const resize = () => {
      width = window.innerWidth;
      height = window.innerHeight;
      dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    resizeHandler = resize;
    window.addEventListener("resize", resize);

    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    const burstCount = 6;
    const cols = 4;
    const rows = 4;
    const cells: Array<{ c: number; r: number }> = [];

    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) cells.push({ c, r });
    }
    for (let i = cells.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [cells[i], cells[j]] = [cells[j], cells[i]];
    }

    const picked = cells.slice(0, burstCount);
    const cellW = 100 / cols;
    const cellH = 100 / rows;
    const palette = [195, 265, 285, 175, 40, 320];

    const bursts: FireworkBurst[] = picked.map(({ c, r }, idx) => {
      const padX = cellW * 0.18;
      const padY = cellH * 0.18;
      return {
        x: c * cellW + padX + Math.random() * (cellW - padX * 2),
        y: r * cellH + padY + Math.random() * (cellH - padY * 2),
        delay: idx * 0.04
      };
    });

    const t0 = performance.now();
    const GRAVITY = 220;
    const DRAG = 0.985;
    const particles: Ember[] = [];
    const flashes: Ignition[] = [];

    for (const burst of bursts) {
      const cx = (burst.x / 100) * width;
      const cy = (burst.y / 100) * height;
      const delayMs = burst.delay * 1000;

      flashes.push({
        cx, cy,
        birth: t0 + delayMs,
        hue: Math.random() * 360,
        life: 0.45
      });

      for (let i = 0; i < 60; i++) {
        const angle = Math.random() * Math.PI * 2;
        const speed = 160 + Math.random() * 300;
        particles.push({
          cx, cy,
          x: cx, y: cy,
          vx: Math.cos(angle) * speed,
          vy: Math.sin(angle) * speed,
          hue: Math.random() * 360,
          birth: t0 + delayMs,
          life: 1.45 + Math.random() * 0.9,
          trail: [{ x: cx, y: cy }]
        });
      }
    }

    let last = performance.now();

    function frame(now: number): void {
      let dt = (now - last) / 1000;
      last = now;
      if (dt > 0.05) dt = 0.05;
      if (dt <= 0) dt = 1 / 60;
      const dragFactor = Math.pow(DRAG, dt * 60);

      ctx!.clearRect(0, 0, width, height);
      ctx!.globalCompositeOperation = "lighter";
      let alive = false;

      // Original soft ignition flashes, drawn behind the embers.
      for (const f of flashes) {
        const age = (now - f.birth) / 1000;
        if (age < 0) { alive = true; continue; }
        if (age > f.life) continue;
        alive = true;
        const k = 1 - age / f.life;
        const radius = 14 + (1 - k) * 18;
        const glow = ctx!.createRadialGradient(f.cx, f.cy, 0, f.cx, f.cy, radius);
        glow.addColorStop(0, `hsla(${f.hue}, 100%, 88%, ${0.45 * k})`);
        glow.addColorStop(0.55, `hsla(${f.hue}, 100%, 65%, ${0.22 * k})`);
        glow.addColorStop(1, `hsla(${f.hue}, 100%, 55%, 0)`);
        ctx!.fillStyle = glow;
        ctx!.beginPath();
        ctx!.arc(f.cx, f.cy, radius, 0, Math.PI * 2);
        ctx!.fill();
      }

      // Original gravity-bent ember trails and two-pass glow/core rendering.
      for (const p of particles) {
        const age = (now - p.birth) / 1000;
        if (age < 0) { alive = true; continue; }
        if (age > p.life) continue;
        alive = true;

        p.x += p.vx * dt;
        p.y += p.vy * dt;
        p.vy += GRAVITY * dt;
        p.vx *= dragFactor;
        p.vy *= dragFactor;
        p.trail.push({ x: p.x, y: p.y });

        const lifeT = age / p.life;
        let alpha = 1;
        if (lifeT >= 0.38) {
          const fade = (lifeT - 0.26) / 0.78;
          alpha = Math.pow(1 - fade, 1.5);
        }

        ctx!.strokeStyle = `hsla(${p.hue}, 100%, 62%, ${alpha * 0.32})`;
        ctx!.lineWidth = 5.5;
        ctx!.beginPath();
        ctx!.moveTo(p.trail[0].x, p.trail[0].y);
        for (let i = 1; i < p.trail.length; i++) {
          ctx!.lineTo(p.trail[i].x, p.trail[i].y);
        }
        ctx!.stroke();

        ctx!.strokeStyle = `hsla(${p.hue}, 100%, 78%, ${alpha})`;
        ctx!.lineWidth = 1.7;
        ctx!.stroke();

        ctx!.fillStyle = `hsla(${p.hue}, 100%, 94%, ${alpha})`;
        ctx!.beginPath();
        ctx!.arc(p.x, p.y, 1.6, 0, Math.PI * 2);
        ctx!.fill();
      }

      ctx!.globalCompositeOperation = "source-over";
      if (alive) rafId = requestAnimationFrame(frame);
      else rafId = 0;
    }

    rafId = requestAnimationFrame(frame);
    finishTimer = window.setTimeout(cleanup, 2700);
  }

  window.addEventListener("rdt-connect-fireworks", (event: Event) => {
    const alias = (event as CustomEvent<string>).detail;
    if (typeof alias !== "string" ||
        !Object.prototype.hasOwnProperty.call(greetings, alias)) return;
    void activateFireworks(alias);
  });

  document.addEventListener("keydown", (event: KeyboardEvent) => {
    if (event.isComposing || event.repeat ||
        event.ctrlKey || event.metaKey || event.altKey) return;

    const target = event.target;
    if (target instanceof Element &&
        target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"], [role="textbox"]')) {
      keyBuffer = "";
      return;
    }

    if (event.key.length !== 1) {
      if (event.key !== "Shift") keyBuffer = "";
      return;
    }

    const now = Date.now();
    if (now - lastKeyAt > 1800) keyBuffer = "";
    lastKeyAt = now;
    keyBuffer = (keyBuffer + event.key.toLowerCase()).slice(-16);

    for (const alias of ["easteregg", "firework", "rdt"]) {
      if (keyBuffer.endsWith(alias)) {
        keyBuffer = "";
        void activateFireworks(alias);
        return;
      }
    }
  }, true);
})();

// Every enabled button acknowledges the click visually, including buttons whose
// async operation takes several seconds. Persistent server/connection status lives
// in separate elements and is never replaced by clipboard/action feedback.
document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const button = target.closest("button");
  if (!(button instanceof HTMLButtonElement) || button.disabled) return;
  button.classList.remove("rdt-click-confirm");
  void button.offsetWidth;
  button.classList.add("rdt-click-confirm");
  window.setTimeout(() => button.classList.remove("rdt-click-confirm"), 520);
}, true);

let modelReadyFeedbackModel: string | null = null;
let modelReadyFeedbackUntil = 0;
let modelReadyFeedbackTimer: number | null = null;
let lastModelCompletionSignature: string | null = null;
void listenNative<ModelDownloadStatus>("rdt-model-download-status", (event) => {
  const incoming = event.payload;
  const wasActive = modelDownloadStatus.active;
  // Native side already enforces monotonic progress. Keep that state intact on each event;
  // filenames are never carried into the UI.
  acceptModelDownloadStatus(incoming);
  if (incoming.active || incoming.phase === "failed" || incoming.phase === "cancelled") {
    lastModelCompletionSignature = null;
  }
  if (wasActive && !incoming.active && incoming.phase === "complete" && incoming.model) {
    const signature = `${incoming.model}:${incoming.downloadedBytes}:${incoming.totalBytes ?? ""}:${incoming.message}`;
    if (signature !== lastModelCompletionSignature) {
      lastModelCompletionSignature = signature;
      modelReadyFeedbackModel = incoming.model;
      modelReadyFeedbackUntil = Date.now() + 2600;
      if (modelReadyFeedbackTimer !== null) window.clearTimeout(modelReadyFeedbackTimer);
      modelReadyFeedbackTimer = window.setTimeout(() => {
        modelReadyFeedbackTimer = null;
        if (modelReadyFeedbackModel === incoming.model && Date.now() >= modelReadyFeedbackUntil) {
          modelReadyFeedbackModel = null;
          renderModelDownloadUI();
        }
      }, 2650);
    }
  }
  renderModelDownloadUI();
  scheduleVisualSetupGuideUpdate();
  if (!incoming.active && incoming.phase === "complete") {
    showNotice(incoming.message, "good");
    void refreshModelStorageStatus();
    void refreshModelCacheBadges();
  } else if (!incoming.active && incoming.phase === "cancelled") {
    showNotice(incoming.message, "warning");
    void refreshModelStorageStatus();
    void refreshModelCacheBadges();
  } else if (incoming.phase === "failed") {
    showNotice(incoming.message, "error");
    // A failed or recovered download can leave partial HF snapshot entries behind.
    // Re-read native cache verification even if cleanup remains active so a stale
    // "Downloaded" badge is never preserved from the previous selection.
    void refreshModelStorageStatus();
    void refreshModelCacheBadges();
  }
});

void listenNative<string>("rdt-connect-stop-failed", (event) => {
  developerStopError.textContent = event.payload;
  developerStopError.classList.remove("hidden");
  showNotice(translateKey("msg.rdt_connect_stayed_open_because_the_server_c.9626dcfc", {}, getActiveLocale()), "error");
  void refreshBackendStatus();
  void refreshOrphanedBackendStatus(true);
});
function renderBrowserChoiceRow() {
  // Match the Open RDT Voice action: show this preference only while the managed server runs.
  // CSS animates both entry and exit; aria-hidden and disabled state follow the same condition.
  const visible = backend.running;
  const revealing = visible && serverOperation === "starting";
  browserChoiceRow.classList.toggle("is-visible", visible);
  browserChoiceRow.classList.toggle("is-revealing", revealing);
  browserChoiceRow.setAttribute("aria-hidden", visible ? "false" : "true");
  browserSelect.disabled = busy || !visible;
}
function setBusy(value: boolean) {
  busy = value;
  // A single primary control starts or stops the one managed local server.
  startButton.disabled = !startupReady || value || serverOperation !== null || (!backend.running && conflictingBackendPresent) || (!backend.running && modelDownloadStatus.active) || (backend.running && !!backend.model && modelSelect.value !== backend.model && (modelDownloadStatus.active || modelStorageBusy));
  startButtonLabel.textContent = serverOperation === "starting" ? "Starting…" : serverOperation === "stopping" ? translateKey("msg.stopping.bbe85741", {}, getActiveLocale()) : backend.running ? (backend.model && modelSelect.value !== backend.model ? "Switch Model" : "Stop Server") : "Start Server";
  startButton.classList.toggle("is-starting", serverOperation === "starting");
  startButton.classList.toggle("is-stopping", serverOperation === "stopping");
  startButton.classList.toggle("server-running", backend.running);
  preferredPortInput.disabled = value || backend.running;
  preferredPortInput.title = backend.running
    ? "Stop server to use this feature. Change the port for the next server."
    : "Choose a port from 1024–65535 for the next server.";
  portHelp.textContent = backend.running
    ? "Stop the server before changing this. Other apps are never stopped automatically."
    : translateKey("msg.takes_effect_the_next_time_the_server_starts.10233561", {}, getActiveLocale());
  copyEndpointButton.disabled = value || !backend.running;
  copyRemoteUrlButton.disabled = value || !tailscale.ready || !String(tailscale.public_url || "").startsWith("https://");
  toggleTokenButton.disabled = value;
  copyTokenButton.disabled = value;
  openButton.disabled = value || !browserPreflightPassed();
  testConnectionButton.disabled = value || !backend.running;
  openNativeBenchmarkButton.disabled = benchmarkRunInProgress;
  modelSelect.disabled = value || modelDownloadStatus.active || platformModelCatalogFailed;
  modelSelect.title = backend.running
    ? "Browse models while running; select another model and choose Switch Model to restart safely."
    : (isNativeGgmlCatalog() ? "GGML models use the bundled whisper.cpp CPU worker. GPU use is not implied." : "All choices use the inference backend supported by this device.");
  modelSelectTooltip.setAttribute("title", modelSelect.title);
  modelSelectTooltip.setAttribute("data-tooltip", modelSelect.title);
  syncModelSelectPresentation();
  modelHelp.textContent = isNativeGgmlCatalog() && isLinuxRuntime()
    ? "GGML model files are shared across compatible Windows/Linux CPU architectures. GPU acceleration is not implied; the qualified baseline is CPU inference."
    : translateKey("msg.rdt_connect_uses_the_fastest_transcription_h.32d1234d", {}, getActiveLocale());
  renderBrowserChoiceRow();
  renderModelDownloadUI();
  preferredPortTooltip.setAttribute("data-tooltip", backend.running
    ? "Stop server to use this feature. Change the port for the next server."
    : "Choose a port from 1024–65535 for the next server.");
  startButton.classList.toggle("is-idle-start", startupReady && !value && !backend.running && serverOperation === null && !conflictingBackendPresent);
  renderFooterServerStatusDot();
  syncModelSwitchControl();
  updateSettingsLockHelp();
  localHttpsQuickSetupButton.disabled = value || backend.running;
  setupRemoteButton.disabled = value;
  generateQrButton.disabled = value || !backend.running || !tailscale.ready;
  copyLinkButton.disabled = value || !pairingQr?.pairingUrl || !pairingQrUsable();
  expandQrButton.disabled = value || !pairingQr?.qrDataUrl || !pairingQrUsable();
}
function fmtTime(seconds: number | undefined) {
  if (!seconds || !Number.isFinite(seconds)) return "Not yet used";
  return new Date(seconds * 1000).toLocaleString();
}
function shortAgent(agent: string) {
  // Display the complete browser user-agent; CSS wraps it inside the scrollable list.
  return agent;
}
function clearChildren(el: HTMLElement) { while (el.firstChild) el.removeChild(el.firstChild); }
function emptyMessage(text: string) {
  const p = document.createElement("p"); p.className = "empty-row"; p.textContent = text; return p;
}

function clearRecoveredStartupErrorIfRunning(): void {
  if (!backend.running || notice.dataset.state !== "error") return;
  const detail = document.getElementById("developer-latest-detail")?.textContent || "";
  const isStartupFailure = /backend exited during startup|backend did not become healthy within|could not launch the packaged whisper backend|could not monitor the backend process|could not check the selected model before starting/i.test(detail);
  if (isStartupFailure) showNotice(translateKey("msg.server_started_successfully.2c4a56fc", {}, getActiveLocale()), "good");
}

async function refreshBackendStatus(silent = true) {
  try {
    const previous = backend;
    const next = await invoke<BackendStatus>("get_backend_status");
    const identityChanged = previous.running !== next.running || previous.pid !== next.pid || previous.port !== next.port;
    const changed = (["running", "pid", "port", "model", "localUrl", "logPath"] as const)
      .some((key) => previous[key] !== next[key]);
    backend = next;
    if (!next.running || identityChanged) appliedInferenceDevicePreference = null;
    renderInferenceHardwareStatus();
    if (!backend.running || (lastConnectionCheck && (lastConnectionCheck.port !== backend.port || lastConnectionCheck.pid !== backend.pid || lastConnectionCheck.running !== backend.running))) lastConnectionCheck = null;
    if (changed) renderBackendStatus();
    if (backend.running && identityChanged) void refreshConnectionStatus();
    if (backend.running) clearRecoveredStartupErrorIfRunning();
    else renderConnectionStatus();
    if (backend.running && !silent && changed) showNotice(translateKey("msg.the_local_whisper_backend_is_ready.da81efea", {}, getActiveLocale()), "good");
  } catch (e) {
    if (!silent) showNotice(String(e), "error");
  }
}
function renderConnectionStatus() {
  openButton.disabled = busy || !browserPreflightPassed();
  if (!backend.running) {
    connectionStatus.textContent = translateKey("msg.no_server_is_running_start_it_on_the_local_s.e3d5652a", {}, getActiveLocale());
    connectionStatus.className = "connection-status disconnected";
    connectionSummary.textContent = translateKey("msg.start_the_server_to_check_its_connection.6691825b", {}, getActiveLocale());
    testConnectionButton.disabled = true;
    return;
  }
  if (!lastConnectionCheck || lastConnectionCheck.port !== backend.port || !lastConnectionCheck.running) {
    connectionStatus.textContent = `Server process is active at ${backend.localUrl || translateKey("msg.the_local_endpoint.2e5694ca", {}, getActiveLocale())}, but connection/authentication has not been checked in this session.`;
    connectionStatus.className = "connection-status pending";
    connectionSummary.textContent = translateKey("msg.open_rdt_voice_is_disabled_until_the_server.ed8f7664", {}, getActiveLocale());
  } else {
    const serverCheckStatus = lastConnectionCheck.healthOk ? "PASS" : "FAIL";
    const accessCheckStatus = lastConnectionCheck.authOk === true
      ? "PASS"
      : lastConnectionCheck.authOk === false
        ? `FAIL${lastConnectionCheck.authHttpStatus ? ` (HTTP ${lastConnectionCheck.authHttpStatus})` : ""}`
        : "NOT CHECKED";
    connectionStatus.textContent = translateKey("msg.fail_checked_message.c15aeec8", { serverCheckStatus: serverCheckStatus, accessCheckStatus: accessCheckStatus, message: lastConnectionCheck.message }, getActiveLocale());
    connectionStatus.className = `connection-status ${lastConnectionCheck.healthOk && lastConnectionCheck.authOk === true ? "connected" : "disconnected"}`;
    connectionSummary.textContent = browserPreflightPassed()
      ? "Server and authentication verified. RDT Voice is ready to open."
      : browserPreflightFailure();
  }
  testConnectionButton.disabled = busy || !backend.running;
}

async function refreshConnectionStatus() {
  if (!backend.running) {
    lastConnectionCheck = null;
    renderConnectionStatus();
    syncModelRecommendation();
    return;
  }
  lastConnectionCheck = null;
  openButton.disabled = true;
  testConnectionButton.disabled = true;
  connectionStatus.textContent = translateKey("msg.checking_local_health_and_rdt_connect_s_auth.f8e14e85", {}, getActiveLocale());
  connectionStatus.className = "connection-status pending";
  connectionSummary.textContent = translateKey("msg.checking_connection.de9fc337", {}, getActiveLocale());
  try {
    lastConnectionCheck = await invoke<BackendConnectionCheck>("get_backend_connection_status");
    renderConnectionStatus();
    syncModelRecommendation();
  } catch (error) {
    lastConnectionCheck = null;
    syncModelRecommendation();
    connectionStatus.textContent = translateKey("msg.connection_check_failed_error_this_result_re.43825799", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale());
    connectionStatus.className = "connection-status disconnected";
    connectionSummary.textContent = translateKey("msg.connection_needs_attention_enable_developer.1dd15333", {}, getActiveLocale());
  } finally {
     testConnectionButton.disabled = busy || !backend.running; openButton.disabled = busy || !browserPreflightPassed(); }
}
testConnectionButton.addEventListener("click", () => void refreshConnectionStatus());

let currentLocalHttps: LocalHttpsStatus | null = null;
let localHttpsSetupInProgress = false;
let localHttpsStatusRequestId = 0;
let selectedTlsCertificate = "";
let selectedTlsKey = "";
let selectedTlsCa = "";

function setTlsPath(el: HTMLElement, path: string | null | undefined) {
  el.textContent = path || translateKey("msg.not_selected.df12aeba", {}, getActiveLocale());
  el.setAttribute("title", path || translateKey("msg.not_selected.df12aeba", {}, getActiveLocale()));
}

function renderLocalHttpsStatus(status: LocalHttpsStatus) {
  currentLocalHttps = status;
  const secureReady = status.mode === "https" && status.configured && status.trusted;

  for (const button of [setupLocalHttpsButton, localHttpsQuickSetupButton]) {
    button.classList.toggle("https-needs-attention", !secureReady);
  }

  localHttpsQuickPrompt.classList.toggle("hidden", secureReady);
  localHttpsAdvancedStatusEl.textContent = secureReady
    ? translateKey("msg.trusted_https_is_ready_on_this_mac.a56d8792", {}, getActiveLocale())
    : "";
  localHttpsAdvancedStatusEl.className = secureReady
    ? "local-https-advanced-status tls-good"
    : "local-https-advanced-status hidden";

  const label = status.mode.toUpperCase();
  localHttpsStatusEl.textContent = translateKey("msg.label_message.e6a1feb4", { label: label, message: status.message }, getActiveLocale());
  localHttpsStatusEl.className = `tls-status ${
    secureReady ? "tls-good" : status.mode === "http" ? "tls-warning" : "tls-needs-setup"
  }`;

  if (secureReady) {
    localHttpsQuickStatus.textContent = "";
    localHttpsQuickStatus.dataset.userFeedback = "ready";
    localHttpsQuickStatus.className = "local-https-quick-status hidden";
  } else {
    localHttpsQuickStatus.textContent =
      translateKey("msg.trusted_https_setup_is_required_arg1.dfacc79f", { arg1: status.message || "The certificate and trust status are not ready." }, getActiveLocale());
    localHttpsQuickStatus.dataset.userFeedback = "status-warning";
    localHttpsQuickStatus.className = "local-https-quick-status tls-warning";
  }

  setTlsPath(localHttpsCertPathEl, selectedTlsCertificate || status.certificatePath);
  setTlsPath(localHttpsKeyPathEl, selectedTlsKey || status.privateKeyPath);
  setTlsPath(localHttpsCaPathEl, selectedTlsCa || status.caCertificatePath);
  transportHttpsInput.checked = status.mode !== "http";
  transportHttpInput.checked = status.mode === "http";
  trustTlsButton.disabled = backend.running || !(selectedTlsCa || status.caCertificatePath) || !status.certificatePath || !status.privateKeyPath;
  removeTlsTrustButton.disabled = backend.running || status.rdtAddedTrustCount < 1;
  setupLocalHttpsButton.disabled = backend.running || localHttpsSetupInProgress;
  localHttpsQuickSetupButton.disabled = backend.running || localHttpsSetupInProgress;
  chooseTlsCertButton.disabled = backend.running;
  chooseTlsKeyButton.disabled = backend.running;
  chooseTlsCaButton.disabled = backend.running;
  linkTlsButton.disabled = backend.running || !(selectedTlsCertificate || status.certificatePath) || !(selectedTlsKey || status.privateKeyPath);
  updateSettingsLockHelp();

  scheduleVisualSetupGuideUpdate();

}

async function refreshLocalHttpsStatus(
  showErrors = false,
  allowDuringSetup = false,
  showChecking = false
): Promise<LocalHttpsStatus | null> {
  if (localHttpsSetupInProgress && !allowDuringSetup) return currentLocalHttps;

  const requestId = ++localHttpsStatusRequestId;
  if (showChecking && !currentLocalHttps) {
    localHttpsStatusEl.textContent = translateKey("msg.checking_local_https_configuration.49441538", {}, getActiveLocale());
    localHttpsStatusEl.className = "tls-status tls-needs-setup";
  }

  try {
    const status = await invoke<LocalHttpsStatus>("get_local_https_status");

    // Ignore replies from older requests, or requests made before setup began.
    if (requestId !== localHttpsStatusRequestId) return currentLocalHttps;
    if (localHttpsSetupInProgress && !allowDuringSetup) return currentLocalHttps;

    if (!selectedTlsCertificate) selectedTlsCertificate = status.certificatePath || "";
    if (!selectedTlsKey) selectedTlsKey = status.privateKeyPath || "";
    if (!selectedTlsCa) selectedTlsCa = status.caCertificatePath || "";
    renderLocalHttpsStatus(status);
    return status;
  } catch (error) {
    if (requestId !== localHttpsStatusRequestId) return currentLocalHttps;
    if (localHttpsSetupInProgress && !allowDuringSetup) return currentLocalHttps;

    // A failed lookup means unknown, not "setup required" or "trusted".
    currentLocalHttps = null;
    localHttpsStatusEl.textContent = translateKey("msg.could_not_verify_local_https_status_error.a81f2529", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale());
    localHttpsStatusEl.className = "tls-status tls-error";
    localHttpsAdvancedStatusEl.textContent = "";
    localHttpsAdvancedStatusEl.className = "local-https-advanced-status hidden";
    localHttpsQuickPrompt.classList.remove("hidden");
    setupLocalHttpsButton.classList.add("https-needs-attention");
    localHttpsQuickSetupButton.classList.add("https-needs-attention");
    localHttpsQuickStatus.textContent =
      "Could not verify HTTPS status. Check your connection and retry; the current trust state is unknown.";
    localHttpsQuickStatus.dataset.userFeedback = "status-error";
    localHttpsQuickStatus.className = "local-https-quick-status tls-error";
    scheduleVisualSetupGuideUpdate();


    if (showErrors) showNotice(translateKey("msg.could_not_read_https_settings_error.8e18eb97", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
    return null;
  }
}

let storageLocationDraft: string[] = [];
let selectedStorageRootDraft = "";
let savedSelectedStorageRoot = "";
let savedStorageLocations: string[] = [];
let modelStorageStatus: ModelStorageStatus | null = null;
let modelDownloadStatus: ModelDownloadStatus = { active: false, model: null, destinationRoot: null, phase: "idle", message: translateKey("msg.choose_a_whisper_model_to_check_its_local_av.fff38d44", {}, getActiveLocale()), progressPercent: 0, downloadedBytes: 0, totalBytes: null, plannedFiles: null, sizeIsEstimate: false };
let modelStorageBusy = false;

const VISUAL_GUIDE_ENABLED_KEY = "rdt_connect_visual_setup_guide_enabled";
let visualSetupGuideDismissedUntilIdle = false;
let visualSetupGuideIdleTimer: number | null = null;
let visualSetupGuideUpdateQueued = false;

// Place each curtain directly under body rather than inside a wrapper
// stacking context. Inline important styles provide a visible fallback even
// if stylesheet stacking or WebKit backdrop-filter rules behave unexpectedly.
const visualSetupGuideOverlay = document.createElement("div");
visualSetupGuideOverlay.id = "visual-setup-guide-overlay";
visualSetupGuideOverlay.setAttribute("aria-hidden", "true");
document.body.appendChild(visualSetupGuideOverlay);

try {
  visualSetupGuideToggle.checked =
    localStorage.getItem(VISUAL_GUIDE_ENABLED_KEY) !== "false";
} catch {
  visualSetupGuideToggle.checked = true;
}

function clearVisualSetupGuideSpotlight() {
  visualSetupGuideOverlay.classList.remove("active");
  document.body.classList.remove(
    "visual-setup-guide-model",
    "visual-setup-guide-https"
  );
}

function positionVisualSetupGuideSpotlight(target: HTMLElement) {
  const rect = target.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return;

  // The spotlight is a direct body child, outside the zoomed app-shell.
  // Rebase its aperture to that fixed viewport so it tracks the real button.
  const overlayRect = visualSetupGuideOverlay.getBoundingClientRect();
  const viewportWidth = Math.max(overlayRect.width || window.innerWidth || 0, 1);
  const viewportHeight = Math.max(overlayRect.height || window.innerHeight || 0, 1);
  const centerX = Math.max(
    0,
    Math.min(viewportWidth, rect.left + rect.width / 2 - overlayRect.left)
  );
  const centerY = Math.max(
    0,
    Math.min(viewportHeight, rect.top + rect.height / 2 - overlayRect.top)
  );

  // Size the clear aperture from the button's actual rendered dimensions.
  const radiusX = Math.max(110, (rect.width / 2 + 22) / 0.70);
  const radiusY = Math.max(48, (rect.height / 2 + 18) / 0.70);
  visualSetupGuideOverlay.style.setProperty("--spot-x", `${centerX}px`);
  visualSetupGuideOverlay.style.setProperty("--spot-y", `${centerY}px`);
  visualSetupGuideOverlay.style.setProperty("--spot-rx", `${radiusX}px`);
  visualSetupGuideOverlay.style.setProperty("--spot-ry", `${radiusY}px`);
}
function updateVisualSetupGuide() {
  const localView = document.getElementById("view-local");
  const localViewVisible = Boolean(
    localView &&
    !localView.classList.contains("hidden") &&
    localView.getClientRects().length > 0
  );

  // The model stage is independent of HTTPS; readiness is checked for step two.
  if (
    !visualSetupGuideToggle.checked ||
    visualSetupGuideDismissedUntilIdle ||
    !localViewVisible
  ) {
    clearVisualSetupGuideSpotlight();
    return;
  }

  const selectedModel = modelSelect.value;
  const modelReady = Boolean(
    modelStorageStatus?.model === selectedModel &&
    modelStorageStatus.modelCached
  );
  const downloadMatches = modelDownloadStatus.model === selectedModel;
  const selectedDownloadActive = Boolean(
    downloadMatches && modelDownloadStatus.active
  );

  let target: HTMLButtonElement | null = null;
  let stage: "model" | "https" | null = null;

  if (!modelReady && !selectedDownloadActive) {
    if (!modelDownloadButton.hidden && !modelDownloadButton.disabled) {
      target = modelDownloadButton;
      stage = "model";
    }
  } else if (
    modelReady &&
    currentLocalHttps !== null &&
    !(
      currentLocalHttps.mode === "https" &&
      currentLocalHttps.configured &&
      currentLocalHttps.trusted
    ) &&
    !localHttpsQuickPrompt.classList.contains("hidden") &&
    !localHttpsQuickSetupButton.disabled
  ) {
    target = localHttpsQuickSetupButton;
    stage = "https";
  }

  if (!target || !stage) {
    clearVisualSetupGuideSpotlight();
    return;
  }

  document.body.classList.toggle("visual-setup-guide-model", stage === "model");
  document.body.classList.toggle("visual-setup-guide-https", stage === "https");

  // The single masked scrim leaves a clear aperture around the actual button.
  // This avoids relying on z-index to lift a nested button above the overlay.
  positionVisualSetupGuideSpotlight(target);
  visualSetupGuideOverlay.classList.add("active");
}

function scheduleVisualSetupGuideUpdate() {
  if (visualSetupGuideUpdateQueued) return;
  visualSetupGuideUpdateQueued = true;
  window.requestAnimationFrame(() => {
    visualSetupGuideUpdateQueued = false;
    updateVisualSetupGuide();
  });
}

function suspendVisualSetupGuide() {
  visualSetupGuideDismissedUntilIdle = true;
  if (visualSetupGuideIdleTimer !== null) {
    window.clearTimeout(visualSetupGuideIdleTimer);
    visualSetupGuideIdleTimer = null;
  }
  clearVisualSetupGuideSpotlight();
}

function restartVisualSetupGuideIdleTimer() {
  visualSetupGuideDismissedUntilIdle = true;
  if (visualSetupGuideIdleTimer !== null) {
    window.clearTimeout(visualSetupGuideIdleTimer);
  }
  clearVisualSetupGuideSpotlight();
  visualSetupGuideIdleTimer = window.setTimeout(() => {
    visualSetupGuideIdleTimer = null;
    visualSetupGuideDismissedUntilIdle = false;
    scheduleVisualSetupGuideUpdate();
  }, 4000);
}

visualSetupGuideToggle.addEventListener("change", () => {
  try {
    localStorage.setItem(
      VISUAL_GUIDE_ENABLED_KEY,
      String(visualSetupGuideToggle.checked)
    );
  } catch { /* optional preference */ }
  scheduleVisualSetupGuideUpdate();
});

// Any click while using the Local Server view dismisses the spotlight for
// four seconds. It then returns only if the current state still calls for it.
document.addEventListener("click", (event) => {
  const node = event.target;
  if (!(node instanceof Element)) return;

  const localView = document.getElementById("view-local");
  const clickedLocalTab = node.closest('[data-view-target="local"]') !== null;

  if (localView?.contains(node) || clickedLocalTab) {
    restartVisualSetupGuideIdleTimer();
  } else {
    suspendVisualSetupGuide();
  }
}, true);

// Covers keyboard-based model changes and viewport movement as well as clicks.
modelSelect.addEventListener("change", restartVisualSetupGuideIdleTimer);
window.addEventListener("resize", scheduleVisualSetupGuideUpdate, { passive: true });
document.addEventListener("scroll", scheduleVisualSetupGuideUpdate, true);

updateVisualSetupGuide();


function stopServerSettingTitle(baseTitle: string): string {
  return backend.running ? `Stop server to use this feature. ${baseTitle}` : baseTitle;
}
function updateSettingsLockHelp() {
  const locked = backend.running;
  settingsLockHint?.classList.toggle("hidden", !locked);
  const titledControls: Array<[HTMLElement, string]> = [
    [modelStorageAddButton, "Add a model-storage folder."],
    [modelStorageSaveButton, "Save the storage-folder priority for future downloads and model lookup."],
    [setupLocalHttpsButton, "Set up a trusted local HTTPS certificate."],
    [chooseTlsCertButton, "Choose the server certificate file."],
    [chooseTlsKeyButton, "Choose the matching private key file."],
    [chooseTlsCaButton, "Choose the root CA used to trust this certificate."],
    [linkTlsButton, "Link and validate the selected certificate and matching key."],
    [trustTlsButton, "Add the selected root CA to this Mac's login Keychain."],
    [removeTlsTrustButton, "Remove only HTTPS trust entries previously added by RDT Connect."],
    [deleteRdtModelsButton, "Delete only supported RDT Connect MLX Whisper cache entries; other repositories remain untouched."],
  ];
  for (const [element, title] of titledControls) element.title = stopServerSettingTitle(title);
  for (const element of Array.from(modelStorageLocationsEl.querySelectorAll<HTMLButtonElement>("button"))) {
    const baseTitle = element.dataset.baseTitle || element.title.replace(/^Stop server to use this feature\.\s*/i, "");
    element.dataset.baseTitle = baseTitle;
    element.title = stopServerSettingTitle(baseTitle);
  }
  const modelDownloadBusy = modelDownloadStatus.active;
  deleteRdtModelsButton.title = locked
    ? stopServerSettingTitle("Finish or cancel active downloads before deleting cached models.")
    : modelDownloadBusy
      ? "Finish or cancel the active model download to use this feature. Other model repositories are not touched."
      : "Delete only the supported RDT Connect MLX Whisper cache entries; other repositories are not touched.";
  transportHttpsInput.disabled = locked;
  transportHttpInput.disabled = locked;
  transportHttpsInput.title = stopServerSettingTitle(translateKey("msg.use_https_with_a_valid_trusted_certificate.d876b6f6", {}, getActiveLocale()));
  transportHttpInput.title = stopServerSettingTitle(translateKey("msg.http_is_unencrypted_and_intended_for_local_d.38719d9d", {}, getActiveLocale()));
}

function modelStorageDraftIsDirty(): boolean {
  return JSON.stringify(storageLocationDraft) !== JSON.stringify(savedStorageLocations) || selectedStorageRootDraft !== savedSelectedStorageRoot;
}

function renderModelStorageSettings() {
  modelStorageLocationsEl.replaceChildren();
  storageLocationDraft.forEach((path, index) => {
    const row = document.createElement("div"); row.className = "model-storage-row";
    const destination = document.createElement("input");
    destination.type = "radio"; destination.name = "model-storage-download-destination";
    destination.value = path; destination.checked = path === selectedStorageRootDraft;
    destination.className = "model-storage-destination-radio";
    destination.disabled = backend.running || modelStorageBusy;
    destination.setAttribute("aria-label", translateKey("msg.use_path_for_the_next_model_download.1d20", { path }, getActiveLocale()));
    destination.addEventListener("change", () => {
      selectedStorageRootDraft = path;
      renderModelStorageSettings();
    });
    const pathWrap = document.createElement("div"); pathWrap.className = "model-storage-path-wrap";
    const pathText = document.createElement("code"); pathText.className = "model-storage-path"; pathText.textContent = path; pathText.title = path;
    const role = document.createElement("small"); role.className = "model-storage-role";
    role.textContent = path === selectedStorageRootDraft ? translateKey("msg.next_model_download_destination.1d20", {}, getActiveLocale()) : translateKey("msg.available_for_finding_existing_models.1d20", {}, getActiveLocale());
    const availability = document.createElement("small"); availability.className = "model-storage-availability";
    const savedIndex = modelStorageStatus?.locations.indexOf(path) ?? -1;
    if (savedIndex < 0 && modelStorageDraftIsDirty()) {
      availability.textContent = translateKey("msg.pending_save.3bdd6c5f", {}, getActiveLocale()); availability.classList.add("unsaved");
    } else if (savedIndex >= 0 && modelStorageStatus?.locationsAvailability[savedIndex] === "available") {
      availability.textContent = modelStorageStatus?.modelCached && modelStorageStatus.activeRoot === path ? translateKey("msg.available_selected_model_is_cached_here.a1ce60d7", {}, getActiveLocale()) : translateKey("msg.available.e6744473", {}, getActiveLocale());
    } else if (savedIndex >= 0 && modelStorageStatus?.locationsAvailability[savedIndex] === "not-checked") {
      availability.textContent = translateKey("msg.not_checked_model_is_already_available_earli.4fb9d856", {}, getActiveLocale());
      availability.classList.add("unsaved");
    } else {
      availability.textContent = translateKey("msg.unavailable_reconnect_the_drive_or_remove_th.0f55c774", {}, getActiveLocale());
      availability.classList.add("unavailable");
    }
    pathWrap.append(pathText, role, availability);
    const actions = document.createElement("div"); actions.className = "model-storage-row-actions";
    const remove = document.createElement("button"); remove.className = "mini-button deny"; remove.type = "button";
    remove.textContent = translateKey("msg.remove.c3812fc4", {}, getActiveLocale());
    remove.dataset.baseTitle = translateKey("msg.remove_this_model_storage_folder_from_the_li.c967032d", {}, getActiveLocale());
    remove.title = stopServerSettingTitle(remove.dataset.baseTitle);
    remove.disabled = storageLocationDraft.length <= 1 || backend.running || modelStorageBusy;
    remove.addEventListener("click", () => { const removed = storageLocationDraft.splice(index, 1)[0]; if (selectedStorageRootDraft === removed) selectedStorageRootDraft = storageLocationDraft[0] || ""; renderModelStorageSettings(); });
    actions.append(remove);
    row.append(destination, pathWrap, actions);
    modelStorageLocationsEl.appendChild(row);
  });
  modelStorageAddButton.title = translateKey("msg.add_a_model_storage_folder.d01586c6", {}, getActiveLocale());
  modelStorageSaveButton.title = stopServerSettingTitle(translateKey("msg.save_selected_download_destination_and_all_model_access_folders.1d20", {}, getActiveLocale()));
  modelStorageAddButton.disabled = modelStorageBusy || storageLocationDraft.length >= 20;
  modelStorageSaveButton.disabled = backend.running || modelStorageBusy || !modelStorageDraftIsDirty() || storageLocationDraft.length < 1 || storageLocationDraft.length > 20;
  modelStorageSaveButton.textContent = modelStorageBusy ? translateKey("msg.saving.23e39291", {}, getActiveLocale()) : translateKey("msg.save_model_storage_locations.1d20", {}, getActiveLocale());
}

function renderModelStorageSummary(status: ModelStorageStatus) {
  if (status.modelCached) {
    modelStorageSummary.textContent = translateKey("msg.verified_local_model_copy_found_rdt_connect.af333d61", { activeRoot: status.activeRoot, model: status.model }, getActiveLocale());
    modelStorageSummary.className = "tls-status tls-good";
  } else {
    modelStorageSummary.textContent = translateKey("msg.no_verified_local_snapshot_found_for_model_a.ecf84dd0", { model: status.model, activeRoot: status.activeRoot }, getActiveLocale());
    modelStorageSummary.className = "tls-status tls-warning";
  }
  if (backend.running) modelStorageSummary.textContent += ` ${translateKey("msg.stop_the_server_before_editing_storage_locat.1f64929b", {}, getActiveLocale())}`;
}

function withModelStorageDeadline<T>(request: Promise<T>, timeoutMs = 15000): Promise<T> {
  let timer: number | undefined;
  const deadline = new Promise<T>((_, reject) => {
    timer = window.setTimeout(() => reject(new Error(
      translateKey("msg.model_information_check_timed_out_after_15_seconds_check_the_sele.1d20", {}, getActiveLocale())
    )), timeoutMs);
  });
  return Promise.race([request, deadline]).finally(() => {
    if (timer !== undefined) window.clearTimeout(timer);
  });
}

async function refreshModelStorageStatus(showErrors = false) {
  delete modelDownloadStatusEl.dataset.storageError;
  renderModelDownloadUI();
  try {
    const status = await withModelStorageDeadline(requestModelStorageStatus(modelSelect.value));
    modelStorageStatus = status;
    delete modelDownloadStatusEl.dataset.storageError;
    if (!modelStorageDraftIsDirty()) {
      storageLocationDraft = [...status.locations];
      savedStorageLocations = [...status.locations];
      selectedStorageRootDraft = status.selectedRoot;
      savedSelectedStorageRoot = status.selectedRoot;
    }
    renderModelStorageSummary(status);
    renderModelStorageSettings();
    renderModelDownloadUI();
    scheduleVisualSetupGuideUpdate();
    renderModelCleanupButtons();
  } catch (error) {
    modelDownloadStatusEl.dataset.storageError = String(error);
    if (String(error).includes("timed out")) modelStorageStatusInFlight.delete(modelSelect.value);
    modelStorageSummary.textContent = translateKey("msg.could_not_read_model_storage_settings_error.c837f48b", { error: String(error) }, getActiveLocale());
    modelStorageSummary.className = "tls-status tls-error";
    if (showErrors) showNotice(translateKey("msg.could_not_read_model_storage_settings_error.c837f48b", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
    renderModelStorageSettings();
    renderModelDownloadUI();
    scheduleVisualSetupGuideUpdate();
    renderModelCleanupButtons();
  }
}

function friendlyModelName(model: string): string {
  const localeKey = modelNameLocaleKey(model);
  if (localeKey) return translateKey(localeKey, {}, getActiveLocale());
  return model.split("/").pop() || translateKey("msg.whisper_model.e656f4a1", {}, getActiveLocale());
}
function approximateModelSizeLabel(model: string): string {
  const catalogSize = modelMenuDetails[model]?.size;
  if (catalogSize) return catalogSize;
  const sizes: Record<string, string> = {
    "mlx-community/whisper-large-v3-turbo": "~1.61 GB",
    "mlx-community/whisper-large-v3-mlx": "~3.08 GB",
    "mlx-community/whisper-small-mlx": "~481 MB",
    "mlx-community/whisper-base-mlx": "~144 MB",
    "mlx-community/whisper-tiny-mlx": "~74 MB",
    "tiny": "~43.5 MB",
    "base": "~81.8 MB",
    "small": "~264 MB",
    "large-v3-turbo": "~574 MB",
    "large-v3": "~1.08 GB",
  };
  return sizes[model] || "Size unavailable";
}
function formatModelBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 MB";
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(2)} GB`;
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(0)} MB`;
  if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(0)} KB`;
  return `${Math.round(bytes)} bytes`;
}
function modelDownloadPhaseRank(phase: string): number {
  return ({ idle: 0, checking: 1, downloading: 2, canceling: 3, verifying: 4, complete: 5, failed: 5, cancelled: 5 } as Record<string, number>)[phase] ?? 0;
}
function acceptModelDownloadStatus(incoming: ModelDownloadStatus) {
  const previous = modelDownloadStatus;
  // A user cancellation is an explicit override, even if a queued progress event
  // already advanced the phase to verifying. Do not let an older event undo it.
  if (previous.model && previous.model === incoming.model && previous.active && incoming.active && incoming.phase === "canceling" && !["complete", "cancelled"].includes(previous.phase)) {
    modelDownloadStatus = {
      ...incoming,
      progressPercent: Math.max(previous.progressPercent, incoming.progressPercent),
      downloadedBytes: Math.max(previous.downloadedBytes, incoming.downloadedBytes),
      totalBytes: incoming.totalBytes ?? previous.totalBytes,
      plannedFiles: incoming.plannedFiles ?? previous.plannedFiles,
      sizeIsEstimate: incoming.totalBytes == null ? previous.sizeIsEstimate : incoming.sizeIsEstimate,
      destinationRoot: incoming.destinationRoot ?? previous.destinationRoot,
    };
    return;
  }
  // Once cancellation is requested, later in-flight progress cannot move the UI
  // back to checking/downloading/verifying. A failed cancellation remains retryable.
  if (previous.model && previous.model === incoming.model && previous.active && previous.phase === "canceling" && incoming.active && !["canceling", "failed"].includes(incoming.phase)) {
    modelDownloadStatus = {
      ...incoming,
      active: true,
      phase: "canceling",
      message: previous.message,
      progressPercent: Math.max(previous.progressPercent, incoming.progressPercent),
      downloadedBytes: Math.max(previous.downloadedBytes, incoming.downloadedBytes),
      totalBytes: incoming.totalBytes ?? previous.totalBytes,
      plannedFiles: incoming.plannedFiles ?? previous.plannedFiles,
      sizeIsEstimate: incoming.totalBytes == null ? previous.sizeIsEstimate : incoming.sizeIsEstimate,
      destinationRoot: incoming.destinationRoot ?? previous.destinationRoot,
    };
    return;
  }
  // Native command responses can race with progress events. Ignore an older
  // response from the same attempt so progress and phase never jump backwards.
  if (incoming.phase !== "canceling" && previous.model && previous.model === incoming.model && modelDownloadPhaseRank(previous.phase) > modelDownloadPhaseRank(incoming.phase)) {
    modelDownloadStatus = {
      ...incoming,
      active: previous.active,
      phase: previous.phase,
      message: previous.message,
      progressPercent: Math.max(previous.progressPercent, incoming.progressPercent),
      downloadedBytes: Math.max(previous.downloadedBytes, incoming.downloadedBytes),
      totalBytes: incoming.totalBytes ?? previous.totalBytes,
      plannedFiles: incoming.plannedFiles ?? previous.plannedFiles,
      sizeIsEstimate: incoming.totalBytes == null ? previous.sizeIsEstimate : incoming.sizeIsEstimate,
      destinationRoot: incoming.destinationRoot ?? previous.destinationRoot,
    };
    return;
  }
  if (previous.model && previous.model === incoming.model && previous.active && incoming.active) {
    modelDownloadStatus = {
      ...incoming,
      progressPercent: Math.max(previous.progressPercent, incoming.progressPercent),
      downloadedBytes: Math.max(previous.downloadedBytes, incoming.downloadedBytes),
      totalBytes: incoming.totalBytes ?? previous.totalBytes,
      plannedFiles: incoming.plannedFiles ?? previous.plannedFiles,
      sizeIsEstimate: incoming.totalBytes == null ? previous.sizeIsEstimate : incoming.sizeIsEstimate,
      destinationRoot: incoming.destinationRoot ?? previous.destinationRoot,
    };
    return;
  }
  modelDownloadStatus = incoming;
}

function renderModelDownloadUI() {

  const selectedModel = modelSelect.value;
  const statusMatches = modelDownloadStatus.model === selectedModel;
  const download = statusMatches ? modelDownloadStatus : null;
  const cached = modelStorageStatus?.model === selectedModel && modelStorageStatus.modelCached;
  const active = Boolean(download?.active);
  const runningModelCacheProtected = backend.running && (!backend.model || backend.model === selectedModel);
  const phase = download?.phase || "idle";
  const failed = phase === "failed";
  const cancelled = phase === "cancelled";
  const complete = phase === "complete";
  const developerDetails = developerOptionsEnabled;
  const readyFeedback = complete && Boolean(download) && !active && Boolean(cached) && modelReadyFeedbackModel === selectedModel && Date.now() < modelReadyFeedbackUntil;
  const successIdle = complete && Boolean(download) && !active && !readyFeedback && Boolean(cached);
  const destination = download?.destinationRoot || modelStorageStatus?.activeRoot || "configured model storage";
  const settingsHint = translateKey("msg.change_this_path_in_settings_model_storage.638f85bd", {}, getActiveLocale());
  // The ordinary view stays quiet for cached models. A just-finished download gets a brief success animation.
  modelDownloadPanel.classList.toggle("developer-details", developerDetails);
  modelDownloadPanel.classList.toggle("is-active", active);
  modelDownloadPanel.classList.toggle("has-attention", failed || cancelled);
  modelDownloadPanel.classList.toggle("is-cached", !developerDetails && ((Boolean(cached) && !readyFeedback) || successIdle));
  modelDownloadPanel.classList.toggle("is-ready-feedback", !developerDetails && readyFeedback);
  modelDownloadPanel.dataset.downloadPhase = phase;

  if (cached && !readyFeedback) {
    modelDownloadTitle.textContent = translateKey("msg.model_ready_to_use.6839ae20", {}, getActiveLocale());
    modelDownloadDetail.textContent = developerDetails
      ? translateKey("msg.selectedmodel_is_available_in_destination_se.71540306", { selectedModel: friendlyModelName(selectedModel), destination: destination, settingsHint: settingsHint }, getActiveLocale())
      : translateKey("msg.selectedmodel_verified_and_available.c595e479", { selectedModel: friendlyModelName(selectedModel) }, getActiveLocale());
    modelDownloadStatusEl.textContent = translateKey("msg.no_download_needed_the_server_can_use_this_l.ebbde0dc", {}, getActiveLocale());
  } else if (failed) {
    modelDownloadTitle.textContent = active ? translateKey("msg.download_needs_attention.7b00d3be", {}, getActiveLocale()) : translateKey("msg.download_needs_attention.7b00d3be", {}, getActiveLocale());
    modelDownloadDetail.textContent = developerDetails
      ? translateKey("msg.selectedmodel_selectedmodel2_destination_des.56fb6dd4", { selectedModel: friendlyModelName(selectedModel), selectedModel2: approximateModelSizeLabel(selectedModel), destination: destination, settingsHint: settingsHint }, getActiveLocale())
      : translateKey("msg.selectedmodel_selectedmodel2_retry_available.ea36cc20", { selectedModel: friendlyModelName(selectedModel), selectedModel2: approximateModelSizeLabel(selectedModel) }, getActiveLocale());
    modelDownloadStatusEl.textContent = `${download?.message || translateKey("msg.the_download_did_not_finish.2450f84a", {}, getActiveLocale())}${active ? ` ${translateKey("msg.the_downloader_may_still_be_stopping_retry_c.e0354707", {}, getActiveLocale())}` : ` ${translateKey("msg.completed_files_remain_available_for_retry.f778dcbb", {}, getActiveLocale())}`}`;
  } else if (cancelled) {
    modelDownloadTitle.textContent = translateKey("msg.download_canceled.1f39d555", {}, getActiveLocale());
    modelDownloadDetail.textContent = developerDetails
      ? translateKey("msg.selectedmodel_selectedmodel2_destination_des.56fb6dd4", { selectedModel: friendlyModelName(selectedModel), selectedModel2: approximateModelSizeLabel(selectedModel), destination: destination, settingsHint: settingsHint }, getActiveLocale())
      : translateKey("msg.selectedmodel_partial_files_are_kept_for_ret.49c52236", { selectedModel: friendlyModelName(selectedModel) }, getActiveLocale());
    modelDownloadStatusEl.textContent = translateKey("msg.canceled_completed_files_remain_cached_and_c.cdc8c409", {}, getActiveLocale());
  } else if (active) {
    modelDownloadTitle.textContent = phase === "canceling"
      ? translateKey("msg.stopping_model_download.98830090", {}, getActiveLocale())
      : phase === "verifying" ? translateKey("msg.verifying_model_files.992d6123", {}, getActiveLocale())
      : phase === "checking" ? translateKey("msg.checking_model_availability.684cf2cd", {}, getActiveLocale())
      : `Downloading ${friendlyModelName(selectedModel)}`;
    modelDownloadDetail.textContent = developerDetails
      ? translateKey("msg.selectedmodel_selectedmodel2_destination_des.56fb6dd4", { selectedModel: friendlyModelName(selectedModel), selectedModel2: approximateModelSizeLabel(selectedModel), destination: destination, settingsHint: settingsHint }, getActiveLocale())
      : translateKey("msg.selectedmodel_selectedmodel2_download_in_pro.c2768505", { selectedModel: friendlyModelName(selectedModel), selectedModel2: approximateModelSizeLabel(selectedModel) }, getActiveLocale());
    modelDownloadStatusEl.textContent = download?.message || translateKey("msg.preparing_the_download.bb238b3c", {}, getActiveLocale());
  } else if (complete && download && cached) {
    modelDownloadTitle.textContent = translateKey("msg.model_ready_to_use.6839ae20", {}, getActiveLocale());
    modelDownloadDetail.textContent = developerDetails
      ? translateKey("msg.selectedmodel_selectedmodel2_saved_to_destin.3ee2e3e7", { selectedModel: friendlyModelName(selectedModel), selectedModel2: approximateModelSizeLabel(selectedModel), destination: destination, settingsHint: settingsHint }, getActiveLocale())
      : translateKey("msg.selectedmodel_verified_and_ready.cb9993c0", { selectedModel: friendlyModelName(selectedModel) }, getActiveLocale());
    modelDownloadStatusEl.textContent = download.message;
  } else {
    modelDownloadTitle.textContent = translateKey("msg.get_this_model_ready.d07c0483", {}, getActiveLocale());
    modelDownloadDetail.textContent = developerDetails
      ? translateKey("msg.selectedmodel_selectedmodel2_destination_des.56fb6dd4", { selectedModel: friendlyModelName(selectedModel), selectedModel2: approximateModelSizeLabel(selectedModel), destination: destination, settingsHint: settingsHint }, getActiveLocale())
      : translateKey("msg.selectedmodel_approximately_selectedmodel2.4c8f1705", { selectedModel: friendlyModelName(selectedModel), selectedModel2: approximateModelSizeLabel(selectedModel) }, getActiveLocale());
    modelDownloadStatusEl.textContent = modelStorageStatus?.model === selectedModel
      ? (modelStorageStatus.modelCached ? translateKey("msg.a_verified_local_copy_is_ready.f88e0e79", {}, getActiveLocale()) : translateKey("msg.no_verified_local_copy_found_download_it_her.e5290ad8", {}, getActiveLocale()))
      : (modelDownloadStatusEl.dataset.storageError || translateKey("msg.checking_whether_a_local_copy_is_available.9f041523", {}, getActiveLocale()));
  }
  if (backend.running && !backend.model) {
    modelDownloadStatusEl.textContent = translateKey("msg.the_running_server_s_active_model_could_not_be_verified_download.cd60bf94", {}, getActiveLocale());
  } else if (backend.running && backend.model === selectedModel) {
    modelDownloadStatusEl.textContent = translateKey("msg.this_model_is_running_select_a_different_model_to_download_anoth.812491f9", {}, getActiveLocale());
  }
  modelDownloadStatusEl.classList.toggle("error-text", failed || Boolean(modelDownloadStatusEl.dataset.storageError));
  modelDownloadButton.hidden = Boolean(cached || active);
  modelDownloadButton.disabled = runningModelCacheProtected || active || modelDownloadStatus.phase === "canceling" || (!modelStorageStatus && !modelDownloadStatusEl.dataset.storageError);
  modelDownloadButton.textContent = modelDownloadStatusEl.dataset.storageError ? translateKey("msg.retry_model_check.1d20", {}, getActiveLocale()) : (failed || cancelled ? translateKey("msg.retry_download.9e90b868", {}, getActiveLocale()) : translateKey("msg.download_model_selectedmodel.7b14a22c", { selectedModel: approximateModelSizeLabel(selectedModel) }, getActiveLocale()));
  // Keep Cancel visible during cancellation so the user can see that work is in progress.
  // If cancellation cannot be verified, the button becomes a retry action rather than disappearing.
  modelDownloadCancelButton.classList.toggle("hidden", !active);
  modelDownloadCancelButton.disabled = !active || phase === "canceling";
  modelDownloadCancelButton.textContent = phase === "canceling" ? translateKey("msg.stopping.bbe85741", {}, getActiveLocale()) : (failed && active ? translateKey("msg.try_cancel_again.d8afe3a9", {}, getActiveLocale()) : translateKey("msg.cancel.19766ed6", {}, getActiveLocale()));
  const hasProgress = Boolean(download && ((active && ["checking", "canceling", "verifying"].includes(phase)) || (download.totalBytes && download.totalBytes > 0 && ["downloading", "verifying", "failed", "cancelled"].includes(phase))));
  modelDownloadProgressWrap.classList.toggle("hidden", !hasProgress);
  if (hasProgress && download) {
    // 100% is reserved for a terminal, inactive success event after the native
    // downloader group is cleaned up and the cache independently validates complete.
    const verifiedComplete = complete && !active;
    const percent = verifiedComplete ? 100 : Math.max(0, Math.min(99, download.progressPercent || 0));
    modelDownloadProgressBar.style.width = `${percent}%`;
    modelDownloadProgressTrack.setAttribute("aria-valuenow", String(percent));
    modelDownloadProgressPercent.textContent = `${percent}%`;
    modelDownloadProgressPhase.textContent = phase === "checking" ? translateKey("msg.checking_model_size.12c2670d", {}, getActiveLocale()) : phase === "canceling" ? translateKey("msg.stopping_download_workers.8a6ba637", {}, getActiveLocale()) : phase === "verifying" ? translateKey("msg.verifying_model_files.992d6123", {}, getActiveLocale()) : verifiedComplete ? translateKey("msg.model_ready.996f3eb2", {}, getActiveLocale()) : failed ? translateKey("msg.download_stopped.215cff40", {}, getActiveLocale()) : cancelled ? translateKey("msg.download_canceled.1f39d555", {}, getActiveLocale()) : translateKey("msg.downloading_model_files.5a7cb690", {}, getActiveLocale());
    if (phase === "checking") {
      modelDownloadProgressMeta.textContent = translateKey("msg.checking_cached_files_and_the_total_download.607927d1", {}, getActiveLocale());
    } else if (phase === "verifying") {
      modelDownloadProgressMeta.textContent = translateKey("msg.files_received_waiting_for_the_downloader_to.e5ef3efc", {}, getActiveLocale());
    } else if (phase === "canceling") {
      modelDownloadProgressMeta.textContent = translateKey("msg.stopping_the_app_owned_download_process_and.6550e5d7", {}, getActiveLocale());
    } else if (download.totalBytes && download.totalBytes > 0) {
      const estimateLead = download.sizeIsEstimate ? "about " : "";
      const fileCount = developerDetails && download.plannedFiles && download.plannedFiles > 0 ? ` · ${download.plannedFiles} files to download` : "";
      modelDownloadProgressMeta.textContent = translateKey("msg.downloadedbytes_of_estimatelead_totalbytes_f.66da12f6", { downloadedBytes: formatModelBytes(download.downloadedBytes), estimateLead: estimateLead, totalBytes: formatModelBytes(download.totalBytes), fileCount: fileCount }, getActiveLocale());
    } else {
      modelDownloadProgressMeta.textContent = translateKey("msg.checking_the_total_download_size.008bc2c8", {}, getActiveLocale());
    }
  }
  renderModelCleanupButtons();
}

modelCacheSelectionList.addEventListener("change", (event: Event) => {
  const target = event.target;
  if (target instanceof HTMLInputElement &&
      target.matches('input[type="checkbox"][data-model-value]')) {
    syncModelCacheSelectionLocks();
  }
  scheduleVisualSetupGuideUpdate();

});

async function refreshModelDownloadStatus() {
  try {
    const current = await invoke<ModelDownloadStatus>("get_model_download_status");
    modelDownloadStatus = current;
    renderModelDownloadUI();
  } catch (error) {
    modelDownloadStatusEl.textContent = translateKey("msg.could_not_read_download_status_error.1ab50fe8", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale());
    modelDownloadStatusEl.classList.add("error-text");
  }
}
function renderModelCleanupButtons() {
  const downloadActive = modelDownloadStatus.active;
  const selected = getSelectedModelCacheIds();
  const activeUnknown = backend.running && !backend.model;
  const activeSelected = backend.running && !!backend.model && selected.includes(backend.model);
  const blocked = downloadActive || modelStorageBusy || busy || activeUnknown || activeSelected;
  deleteRdtModelsButton.disabled = blocked || selected.length === 0;
  for (const checkbox of Array.from(modelCacheSelectionList.querySelectorAll<HTMLInputElement>('input[type="checkbox"][data-model-value]'))) {
    const model = checkbox.dataset.modelValue || "";
    checkbox.disabled = busy || downloadActive || modelStorageBusy || (backend.running && (!backend.model || model === backend.model));
  }
  modelCleanupStatus.textContent = activeUnknown
    ? translateKey("msg.the_running_server_s_active_model_could_not_be_verified_so_cache.7b813da8", {}, getActiveLocale())
    : downloadActive ? translateKey("msg.finish_or_cancel_the_active_model_download_before_deleting_model.abde8468", {}, getActiveLocale())
    : modelStorageBusy ? translateKey("msg.finish_saving_model_storage_changes_before_deleting_model_caches.bbc62d5e", {}, getActiveLocale())
    : selected.length > 0 ? translateKey(selected.length === 1 ? (backend.running ? "msg.count_inactive_model_cache_is_selected_for_confirmation_the_runn.7b8a0666" : "msg.count_inactive_model_cache_is_selected_for_confirmation.aec8dadf") : (backend.running ? "msg.count_inactive_model_caches_are_selected_for_confirmation_the_ru.04f0ec68" : "msg.count_inactive_model_caches_are_selected_for_confirmation.86df3b05"), { count: selected.length }, getActiveLocale())
    : backend.running ? translateKey("msg.select_inactive_downloaded_model_caches_the_running_model_is_pro.7ec562b7", {}, getActiveLocale())
    : translateKey("msg.select_one_or_more_downloaded_model_caches_above_nothing_is_pres.604c39f1", {}, getActiveLocale());
  updateSettingsLockHelp();
}

const clearRequiredModelGuidance = () => {
  modelDownloadButton.closest<HTMLElement>(".model-download-panel")
    ?.classList.remove("needs-model-download");
  modelDownloadButton.classList.remove("needs-model-download-target");
};
modelSelect.addEventListener("change", clearRequiredModelGuidance);
modelDownloadButton.addEventListener(
  "click", clearRequiredModelGuidance, { capture: true },
);

modelDownloadButton.addEventListener("click", async () => {
  if (modelDownloadStatusEl.dataset.storageError) {
    await refreshModelStorageStatus(true);
    return;
  }
  const model = modelSelect.value;
  if (backend.running && (!backend.model || model === backend.model)) {
    showNotice(backend.model
      ? "The selected model is currently running. Choose a different model before downloading."
      : "Cannot verify the running server's active model; download is paused for safety.", "warning");
    return;
  }
  if (modelDownloadStatus.active) { showNotice(translateKey("msg.a_model_download_is_already_running.22223145", {}, getActiveLocale()), "warning"); return; }
  modelDownloadButton.disabled = true;
  modelDownloadStatus = { active: true, model, destinationRoot: modelStorageStatus?.activeRoot || null, phase: "checking", message: translateKey("msg.checking_model_availability_and_total_size.1aed63d9", {}, getActiveLocale()), progressPercent: 0, downloadedBytes: 0, totalBytes: null, plannedFiles: null, sizeIsEstimate: false };
  renderModelDownloadUI();
  try {
    const status = await invoke<ModelDownloadStatus>("start_model_download", { model });
    acceptModelDownloadStatus(status);
    renderModelDownloadUI();
    if (status.phase === "complete" && !status.active) {
      showNotice(status.message, "good");
      await refreshModelStorageStatus();
      void refreshModelCacheBadges();
    } else {
      showNotice(translateKey("msg.downloading_model_you_can_cancel_safely_comp.5dc4fc38", { model: friendlyModelName(model) }, getActiveLocale()), "info");
    }
  } catch (error) {
    acceptModelDownloadStatus({ ...modelDownloadStatus, active: false, phase: "failed", message: formatHardwareDeviceText(String(error)) });
    renderModelDownloadUI();
    showNotice(translateKey("msg.could_not_download_the_model_error.a226b9d2", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
  }
  finally { renderModelDownloadUI(); }
});
modelDownloadCancelButton.addEventListener("click", async () => {
  if (!modelDownloadStatus.active || modelDownloadStatus.phase === "canceling") return;
  modelDownloadCancelButton.disabled = true;
  try {
    const status = await invoke<ModelDownloadStatus>("cancel_model_download");
    acceptModelDownloadStatus(status);
    renderModelDownloadUI();
    if (status.phase === "failed") showNotice(status.message, "error");
    else showNotice(status.message || translateKey("msg.stopping_the_model_download_and_its_helper_w.8e3a8aaf", {}, getActiveLocale()), "warning");
  } catch (error) {
    // Keep the action retryable instead of leaving the interface permanently in
    // a disabled canceling state when the native command reports a signal error.
    modelDownloadStatus = {
      ...modelDownloadStatus,
      active: true,
      phase: "failed",
      message: translateKey("msg.cancel_could_not_be_confirmed_error_select_t.e38eb04c", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()),
    };
    renderModelDownloadUI();
    showNotice(modelDownloadStatus.message, "error");
  } finally { renderModelDownloadUI(); }
});
deleteRdtModelsButton.addEventListener("click", async () => {
  const selectedModels = getSelectedModelCacheIds();
  if (modelDownloadStatus.active || modelStorageBusy) {
    showNotice(translateKey("msg.finish_or_cancel_model_downloads_and_storage_changes_before_dele.3ef332b7", {}, getActiveLocale()), "warning");
    return;
  }
  if (backend.running && (!backend.model || selectedModels.includes(backend.model))) {
    showNotice(translateKey("msg.the_active_model_cache_is_protected_while_the_server_runs_select.8dde9398", {}, getActiveLocale()), "warning");
    return;
  }
  if (selectedModels.length === 0) {
    showNotice(translateKey("msg.select_one_or_more_downloaded_models_first_nothing_is_preselecte.005d9cde", {}, getActiveLocale()), "warning");
    return;
  }
  const selectedNames = selectedModels.map((model) => {
    const option = Array.from(modelSelect.options).find((candidate) => candidate.value === model);
    return option ? modelMenuInfo(option).name : model;
  });
  const approved = window.confirm(translateKey("msg.permanently_delete_only_these_selected_rdt_connect_mlx_model_caches_models_unselected_models_other_hugging_face_repositories_and_easywhisper_browser_data_will_remain_untouched.4235eebe", { models: selectedNames.map((name) => `• ${name}`).join("\n") }, getActiveLocale()));
  if (!approved) return;
  deleteRdtModelsButton.disabled = true;
  try {
    const result = await invoke<ModelCacheDeleteResult>("delete_rdt_model_caches", { models: selectedModels });
    modelCleanupStatus.textContent = result.message;
    showNotice(result.message, "good");
    await refreshModelStorageStatus();
    await refreshModelDownloadStatus();
    await refreshModelCacheBadges();
  } catch (error) {
    modelCleanupStatus.textContent = translateKey("msg.could_not_delete_selected_model_caches_arg1.7dfa9acc", { arg1: formatHardwareDeviceText(String(error)) }, getActiveLocale());
    showNotice(translateKey("msg.could_not_delete_selected_model_caches_arg1.7dfa9acc", { arg1: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
  } finally {
    renderModelCleanupButtons();
  }
});

modelStorageAddButton.addEventListener("click", async () => {
  if (backend.running) { showNotice(translateKey("msg.stop_the_local_server_before_changing_model.1a36c7e8", {}, getActiveLocale()), "warning"); return; }
  if (storageLocationDraft.length >= 20) { showNotice(translateKey("msg.up_to_20_model_storage_locations_are_support.20545645", {}, getActiveLocale()), "warning"); return; }
  modelStorageBusy = true; renderModelStorageSettings();
  try {
    const selected = await invoke<string | null>("pick_model_storage_directory");
    if (!selected) return;
    if (storageLocationDraft.includes(selected)) { showNotice(translateKey("msg.that_folder_is_already_listed.294a80f8", {}, getActiveLocale()), "info"); return; }
    storageLocationDraft.push(selected);
    if (!selectedStorageRootDraft) selectedStorageRootDraft = selected;
    renderModelStorageSettings();
    showNotice(translateKey("msg.folder_added_to_the_draft_list_save_storage.c0819cab", {}, getActiveLocale()), "info");
  } catch (error) { showNotice(translateKey("msg.could_not_choose_a_model_storage_folder_erro.0f408147", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
  finally { modelStorageBusy = false; renderModelStorageSettings(); }
});

modelStorageSaveButton.addEventListener("click", async () => {
  if (backend.running) { showNotice(translateKey("msg.stop_the_local_server_before_saving_model_st.1949e9c3", {}, getActiveLocale()), "warning"); return; }
  modelStorageBusy = true; renderModelStorageSettings();
  try {
    const status = await invoke<ModelStorageStatus>("set_model_storage_locations", { model: modelSelect.value, locations: storageLocationDraft, activeRoot: selectedStorageRootDraft });
    modelStorageStatus = status;
    storageLocationDraft = [...status.locations];
    savedStorageLocations = [...status.locations];
    selectedStorageRootDraft = status.selectedRoot;
    savedSelectedStorageRoot = status.selectedRoot;
    renderModelStorageSummary(status); renderModelStorageSettings();
    void refreshModelCacheBadges();
    showNotice(translateKey("msg.model_storage_locations_saved_the_next_serve.abe7ee05", {}, getActiveLocale()), "good");
  } catch (error) {
    showNotice(translateKey("msg.could_not_save_model_storage_settings_error.3a4cc317", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
  } finally { modelStorageBusy = false; renderModelStorageSettings(); }
});

async function chooseTlsFile(kind: "certificate" | "private-key" | "ca") {
  try {
    const path = await invoke<string | null>("pick_tls_file", { kind });
    if (!path) return;
    if (kind === "certificate") selectedTlsCertificate = path;
    else if (kind === "private-key") selectedTlsKey = path;
    else selectedTlsCa = path;
    setTlsPath(kind === "certificate" ? localHttpsCertPathEl : kind === "private-key" ? localHttpsKeyPathEl : localHttpsCaPathEl, path);
    linkTlsButton.disabled = !(selectedTlsCertificate || currentLocalHttps?.certificatePath) || !(selectedTlsKey || currentLocalHttps?.privateKeyPath) || backend.running;
    trustTlsButton.disabled = !(selectedTlsCa || currentLocalHttps?.caCertificatePath) || !currentLocalHttps?.certificatePath || !currentLocalHttps?.privateKeyPath || backend.running;
  } catch (error) { showNotice(translateKey("msg.could_not_choose_that_file_error.b4bb7e62", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
}

chooseTlsCertButton.addEventListener("click", () => void chooseTlsFile("certificate"));
chooseTlsKeyButton.addEventListener("click", () => void chooseTlsFile("private-key"));
chooseTlsCaButton.addEventListener("click", () => void chooseTlsFile("ca"));


let httpsSetupSuccessHideTimer: number | null = null;
let httpsSetupSuccessFadeTimer: number | null = null;
let httpsSetupSuccessHideStartedAt = 0;
let httpsSetupSuccessRemainingMs = 4200;
let httpsSetupSuccessPointerInside = false;

function getHttpsSetupSuccessLayer(): HTMLDivElement | null {
  return document.getElementById("https-setup-success") as HTMLDivElement | null;
}

function getHttpsSetupSuccessPanel(): HTMLElement | null {
  return getHttpsSetupSuccessLayer()?.querySelector<HTMLElement>(".https-setup-success-panel") ?? null;
}

function finishHttpsSetupSuccessHide() {
  const overlay = getHttpsSetupSuccessLayer();
  if (!overlay) return;

  if (httpsSetupSuccessFadeTimer !== null) {
    window.clearTimeout(httpsSetupSuccessFadeTimer);
    httpsSetupSuccessFadeTimer = null;
  }
  if (httpsSetupSuccessHideTimer !== null) {
    window.clearTimeout(httpsSetupSuccessHideTimer);
    httpsSetupSuccessHideTimer = null;
  }

  overlay.classList.remove("is-visible");
  overlay.classList.add("is-hiding");
  httpsSetupSuccessFadeTimer = window.setTimeout(() => {
    const current = getHttpsSetupSuccessLayer();
    if (current) {
      current.hidden = true;
      current.classList.remove("is-visible", "is-hiding");
      current.setAttribute("aria-hidden", "true");
    }
    httpsSetupSuccessFadeTimer = null;
  }, 300);
}

function scheduleHttpsSetupSuccessHide() {
  const overlay = getHttpsSetupSuccessLayer();
  if (
    !overlay ||
    overlay.hidden ||
    httpsSetupSuccessPointerInside ||
    !overlay.classList.contains("is-visible") ||
    httpsSetupSuccessHideTimer !== null
  ) return;

  httpsSetupSuccessHideStartedAt = Date.now();
  httpsSetupSuccessHideTimer = window.setTimeout(() => {
    httpsSetupSuccessHideTimer = null;
    finishHttpsSetupSuccessHide();
  }, Math.max(0, httpsSetupSuccessRemainingMs));
}

function pauseHttpsSetupSuccessHide() {
  const overlay = getHttpsSetupSuccessLayer();
  if (!overlay || overlay.hidden || !overlay.classList.contains("is-visible")) return;

  httpsSetupSuccessPointerInside = true;
  if (httpsSetupSuccessHideTimer !== null) {
    window.clearTimeout(httpsSetupSuccessHideTimer);
    httpsSetupSuccessHideTimer = null;
    const elapsed = Date.now() - httpsSetupSuccessHideStartedAt;
    httpsSetupSuccessRemainingMs = Math.max(0, httpsSetupSuccessRemainingMs - elapsed);
  }
}

function resumeHttpsSetupSuccessHide() {
  httpsSetupSuccessPointerInside = false;
  scheduleHttpsSetupSuccessHide();
}

function dismissHttpsSetupSuccess() {
  httpsSetupSuccessRemainingMs = 4200;
  httpsSetupSuccessPointerInside = false;
  finishHttpsSetupSuccessHide();
}

const httpsSetupSuccessPanel = getHttpsSetupSuccessPanel();
httpsSetupSuccessPanel?.addEventListener("pointerenter", pauseHttpsSetupSuccessHide);
httpsSetupSuccessPanel?.addEventListener("pointerleave", resumeHttpsSetupSuccessHide);

// Clicks inside the popup leave it visible. A click anywhere outside closes it
// while still allowing the underlying control to receive its click.
document.addEventListener("click", (event) => {
  const overlay = getHttpsSetupSuccessLayer();
  const panel = getHttpsSetupSuccessPanel();
  if (!overlay || overlay.hidden || !overlay.classList.contains("is-visible")) return;
  if (event.target instanceof Node && panel?.contains(event.target)) return;
  dismissHttpsSetupSuccess();
}, true);

function showHttpsSetupSuccess() {
  const overlay = getHttpsSetupSuccessLayer();
  if (!overlay) return;

  if (httpsSetupSuccessHideTimer !== null) {
    window.clearTimeout(httpsSetupSuccessHideTimer);
    httpsSetupSuccessHideTimer = null;
  }
  if (httpsSetupSuccessFadeTimer !== null) {
    window.clearTimeout(httpsSetupSuccessFadeTimer);
    httpsSetupSuccessFadeTimer = null;
  }

  overlay.hidden = false;
  overlay.setAttribute("aria-hidden", "false");
  overlay.classList.remove("is-visible", "is-hiding");

  httpsSetupSuccessRemainingMs = 4200;
  httpsSetupSuccessPointerInside = false;

  // Restart the entrance animation when setup is repeated.
  void overlay.offsetWidth;
  overlay.classList.add("is-visible");

  // If the pointer is already over the popup, start the timer only after exit.
  const panel = getHttpsSetupSuccessPanel();
  if (panel?.matches(":hover")) {
    httpsSetupSuccessPointerInside = true;
  } else {
    scheduleHttpsSetupSuccessHide();
  }
}
async function performLocalHttpsSetup(triggerButton: HTMLButtonElement) {
  if (localHttpsSetupInProgress) return;

  localHttpsSetupInProgress = true;
  // Invalidate any status lookup already in flight before setup starts.
  ++localHttpsStatusRequestId;

  setupLocalHttpsButton.disabled = true;
  localHttpsQuickSetupButton.disabled = true;
  triggerButton.setAttribute("aria-busy", "true");
  localHttpsQuickPrompt.classList.remove("hidden");
  localHttpsQuickStatus.textContent = translateKey("msg.setting_up_trusted_https.7faf4288", {}, getActiveLocale());
  localHttpsQuickStatus.dataset.userFeedback = "pending";
  localHttpsQuickStatus.className = "local-https-quick-status tls-needs-setup";
  localHttpsStatusEl.textContent = translateKey("msg.setting_up_trusted_https.7faf4288", {}, getActiveLocale());
  localHttpsStatusEl.className = "tls-status tls-needs-setup";
  showNotice(isLinuxRuntime() ? "Setting up trusted HTTPS… your system will ask for administrator authorization to trust the local CA." : translateKey("msg.setting_up_trusted_https.7faf4288", {}, getActiveLocale()), "info");

  let setupError: unknown = null;

  try {
    const status = await invoke<LocalHttpsStatus>("setup_local_https_certificate");
    selectedTlsCertificate = status.certificatePath || "";
    selectedTlsKey = status.privateKeyPath || "";
    selectedTlsCa = status.caCertificatePath || "";
  } catch (error) {
    setupError = error;
  } finally {
    triggerButton.removeAttribute("aria-busy");
    localHttpsSetupInProgress = false;
    setupLocalHttpsButton.disabled = backend.running;
    localHttpsQuickSetupButton.disabled = backend.running;

    // One fresh lookup determines the final visible state.
    const verifiedStatus = await refreshLocalHttpsStatus(false, true);

    if (
      verifiedStatus &&
      verifiedStatus.mode === "https" &&
      verifiedStatus.configured &&
      verifiedStatus.trusted
    ) {
      showNotice(
        "Trusted HTTPS is configured. Its status is available under Advanced.",
        "good"
      );
      // The celebration confirms a successful Local Server setup action.
      // It is separate from the optional spotlight guide preference.
      if (triggerButton === localHttpsQuickSetupButton) {
        showHttpsSetupSuccess();
      }
    } else if (!verifiedStatus) {
      const detail = setupError ? ` Setup operation reported: ${String(setupError)}` : "";
      showNotice(
        translateKey("msg.https_setup_finished_but_its_current_status_could_not_be_verifie.91d45be7", { arg1: detail }, getActiveLocale()),
        "warning"
      );
    } else if (setupError) {
      showNotice(translateKey("msg.https_setup_failed_arg1.25103fd5", { arg1: String(setupError) }, getActiveLocale()), "error");
    } else {
      showNotice(
        translateKey("msg.https_setup_needs_attention_verified.37a476ec", { verified: verifiedStatus.message || translateKey("msg.the_certificate_and_trust_status_are_not_ready", {}, getActiveLocale()) }, getActiveLocale()),
        "warning"
      );
    }
  }
}
setupLocalHttpsButton.addEventListener("click", () => void performLocalHttpsSetup(setupLocalHttpsButton));
localHttpsQuickSetupButton.addEventListener("click", () => void performLocalHttpsSetup(localHttpsQuickSetupButton));
localHttpsDetailsButton.addEventListener("click", () => {
  setAppView("settings");
  const details = document.querySelector<HTMLDetailsElement>(".https-details");
  if (details) {
    details.open = true;
    details.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }
});

linkTlsButton.addEventListener("click", async () => {
  const certificatePath = selectedTlsCertificate || currentLocalHttps?.certificatePath || "";
  const privateKeyPath = selectedTlsKey || currentLocalHttps?.privateKeyPath || "";
  const caCertificatePath = selectedTlsCa || currentLocalHttps?.caCertificatePath || null;
  if (!certificatePath || !privateKeyPath) { showNotice(translateKey("msg.choose_both_a_server_certificate_and_its_mat.5e8014ff", {}, getActiveLocale()), "warning"); return; }
  linkTlsButton.disabled = true;
  try {
    const status = await invoke<LocalHttpsStatus>("link_local_https_certificate", { certificatePath, privateKeyPath, caCertificatePath });
    selectedTlsCertificate = status.certificatePath || ""; selectedTlsKey = status.privateKeyPath || ""; selectedTlsCa = status.caCertificatePath || "";
    renderLocalHttpsStatus(status);
    showNotice(status.trusted ? "Certificate and key linked and validated; operating-system trust verification passed." : "Certificate and key linked and validated, but trust is not confirmed yet. Check the operating-system certificate trust status.", status.trusted ? "good" : "warning");
  } catch (error) { showNotice(translateKey("msg.could_not_link_that_certificate_error.f99f0898", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
  finally { await refreshLocalHttpsStatus(); }
});

trustTlsButton.addEventListener("click", async () => {
  const ok = isLinuxRuntime()
    ? window.confirm(translateKey("msg.add_the_rdt_connect_local_root_ca_to_this_computer_s_system_trust_store_your_system_will_ask_for_administrator_authorization_the_ca_is_restricted_to_localhost_127_0_0_1_and_1_and_rdt_connect_records_its_exact_fingerprint_so_it_can_remove_only_this_entry_later_firefox_and_chrome_chromium_may_keep_separate_certificate_stores_and_are_not_changed.50cb7d5d", {}, getActiveLocale()))
    : isWindowsRuntime()
    ? window.confirm(translateKey("msg.add_the_rdt_connect_local_root_ca_to_the_windows_current_user_certificate_trust_store_rdt_connect_records_its_exact_fingerprint_so_it_can_remove_only_this_entry_later.15d3811e", {}, getActiveLocale()))
    : window.confirm("Add the selected issuer/root CA certificate to this Mac's login Keychain? This changes macOS trust decisions for certificates issued by that CA. Only continue if you control or explicitly trust this self-signed root CA. Intermediate CAs are not promoted to roots. RDT Connect will record the exact fingerprint so it can offer to remove this entry later.");
  if (!ok) return;
  trustTlsButton.disabled = true;
  try {
    const status = await invoke<LocalHttpsStatus>("trust_local_https_ca", { caCertificatePath: selectedTlsCa || currentLocalHttps?.caCertificatePath || null });
    renderLocalHttpsStatus(status);
    showNotice(isLinuxRuntime() ? "The RDT local CA was added to the system trust store and recorded for targeted removal. Browsers with their own certificate store may still need the CA imported." : isWindowsRuntime() ? "The RDT local CA was added to the Windows current-user certificate trust store and recorded for targeted removal." : translateKey("msg.the_selected_ca_was_added_to_the_login_keych.ea8b28ac", {}, getActiveLocale()), "good");
  } catch (error) { showNotice(translateKey("msg.could_not_trust_that_ca_error.ea84346e", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
  finally { await refreshLocalHttpsStatus(); }
});

removeTlsTrustButton.addEventListener("click", async () => {
  const count = currentLocalHttps?.rdtAddedTrustCount || 0;
  if (!count) { showNotice(translateKey("msg.rdt_connect_has_no_tracked_ca_trust_entries.d66fa045", {}, getActiveLocale()), "info"); return; }
  const confirmation = isLinuxRuntime()
    ? translateKey(count === 1 ? "msg.remove_count_exact_ca_trust_entry_that_rdt_connect_previously_added_to_the_linux_system_trust_store_your_system_will_ask_for_administrator_authorization_certificate_and_private_key_files_will_not_be_deleted_independently_installed_certificates_will_not_be_touched.ed4f1479" : "msg.remove_count_exact_ca_trust_entries_that_rdt_connect_previously_added_to_the_linux_system_trust_store_your_system_will_ask_for_administrator_authorization_certificate_and_private_key_files_will_not_be_deleted_independently_installed_certificates_will_not_be_touched.48ac1519", { count }, getActiveLocale())
    : isWindowsRuntime()
    ? translateKey(count === 1 ? "msg.remove_count_exact_ca_trust_entry_that_rdt_connect_previously_added_to_the_windows_current_user_certificate_trust_store_certificate_and_private_key_files_will_not_be_deleted_independently_installed_certificates_will_not_be_touched.52dd3e5b" : "msg.remove_count_exact_ca_trust_entries_that_rdt_connect_previously_added_to_the_windows_current_user_certificate_trust_store_certificate_and_private_key_files_will_not_be_deleted_independently_installed_certificates_will_not_be_touched.6e6bda5a", { count }, getActiveLocale())
    : count === 1
    ? translateKey("msg.remove_count_exact_ca_trust_entry_that_rdt_c.0040e5de", { count }, getActiveLocale())
    : translateKey("msg.remove_count_exact_ca_trust_entries_that_rdt.f808b428", { count }, getActiveLocale());
  const ok = window.confirm(confirmation);
  if (!ok) return;
  removeTlsTrustButton.disabled = true;
  try {
    const status = await invoke<LocalHttpsStatus>("remove_rdt_https_trust");
    renderLocalHttpsStatus(status);
    showNotice(translateKey("msg.rdt_connect_added_https_trust_entries_were_r.0c572dad", {}, getActiveLocale()), "good");
  } catch (error) { showNotice(translateKey("msg.could_not_remove_every_tracked_trust_entry_e.d78b5b48", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
  finally { await refreshLocalHttpsStatus(); }
});

async function changeLocalTransport(mode: "https" | "http") {
  if (backend.running) { showNotice(translateKey("msg.stop_the_server_before_changing_its_transpor.4ffb5262", {}, getActiveLocale()), "warning"); await refreshLocalHttpsStatus(); return; }
  if (mode === "http" && !window.confirm("Use unencrypted HTTP for local development only? Audio and API traffic on the loopback connection will not be protected by TLS. HTTPS remains the recommended default. This does not enable public access.")) {
    await refreshLocalHttpsStatus(); return;
  }
  try {
    const status = await invoke<LocalHttpsStatus>("set_local_transport", { mode });
    renderLocalHttpsStatus(status);
    showNotice(mode === "https" ? translateKey("msg.https_selected_rdt_connect_will_require_a_va.c3c4e566", {}, getActiveLocale()) : translateKey("msg.http_development_mode_selected_explicitly.ed7ea39e", {}, getActiveLocale()), mode === "https" ? "good" : "warning");
  } catch (error) { showNotice(translateKey("msg.could_not_change_local_transport_error.6c187215", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); await refreshLocalHttpsStatus(); }
}
transportHttpsInput.addEventListener("change", () => { if (transportHttpsInput.checked) void changeLocalTransport("https"); });
transportHttpInput.addEventListener("change", () => { if (transportHttpInput.checked) void changeLocalTransport("http"); });

function renderBackendStatus() {
  openNativeBenchmarkButton.disabled = benchmarkRunInProgress;
  const operationLabel = serverOperation === "starting" ? "STARTING…" : serverOperation === "stopping" ? "STOPPING…" : null;
  statusPill.textContent = operationLabel || (backend.running ? translateKey("msg.running_value.5f2c3539", { value: backend.port || Number(preferredPortInput.value) || 8888 }, getActiveLocale()) : translateKey("msg.stopped.133c4945", {}, getActiveLocale()));
  statusPill.className = `status-pill ${serverOperation ? "processing" : backend.running ? "running" : "stopped"}`;
  statusDetail.textContent = serverOperation === "starting" ? translateKey("msg.starting_the_local_transcription_server_and.f4aff6a0", {}, getActiveLocale()) : serverOperation === "stopping" ? translateKey("msg.stopping_the_local_transcription_server_and.041ff878", {}, getActiveLocale()) : backend.running ? translateKey("msg.local_transcription_server_is_running.559685bc", {}, getActiveLocale()) : translateKey("msg.local_transcription_server_is_stopped.7ef01073", {}, getActiveLocale());
  startButtonLabel.textContent = serverOperation === "starting" ? "Starting…" : serverOperation === "stopping" ? translateKey("msg.stopping.bbe85741", {}, getActiveLocale()) : backend.running ? "Stop Server" : "Start Server";
  startButton.classList.toggle("server-running", backend.running);
  startButton.classList.toggle("is-idle-start", startupReady && !busy && !backend.running && serverOperation === null && !conflictingBackendPresent);
  startButton.parentElement?.classList.toggle("is-running", backend.running);
  syncModelSwitchControl();
  syncModelSelectPresentation();
  renderBrowserChoiceRow();
// Browser selection and Open RDT Voice appear together only after the server is running.
  renderFooterServerStatusDot();
  openButton.setAttribute("aria-hidden", backend.running ? "false" : "true");
  openButton.tabIndex = backend.running ? 0 : -1;
  startButton.setAttribute("aria-label", backend.running ? translateKey("msg.stop_the_local_transcription_server.4bb79a10", {}, getActiveLocale()) : translateKey("msg.start_the_local_transcription_server.746c013b", {}, getActiveLocale()));
  displayPort.textContent = String(backend.port || Number(preferredPortInput.value) || 8888);
  if (!backend.running) {
    tokenDisplay.value = "";
    tokenDisplay.type = "password";
    toggleTokenButton.textContent = translateKey("msg.show.0df6f1ca", {}, getActiveLocale());
    toggleTokenButton.setAttribute("aria-label", translateKey("msg.reveal_the_access_token.c51590dd", {}, getActiveLocale()));
  } else if (tokenDisplay.type === "password" && !tokenDisplay.value) {
    // Show that a token is present without putting the secret in the hidden field.
    tokenDisplay.value = "••••••••••••••";
  }
  renderConnectionStatus();
  localAddress.textContent = backend.localUrl || translateKey("msg.not_running.9e3856ee", {}, getActiveLocale());
  logPath.textContent = backend.logPath || translateKey("msg.log_file_is_created_when_the_server_starts.af4dd0a6", {}, getActiveLocale());

  setBusy(busy);
  syncModelCacheSelectionLocks();

  scheduleVisualSetupGuideUpdate();

}

window.addEventListener("rdt-apple-gpu-display-label-ready", () => {
  renderBackendStatus();
});

async function refreshOrphanedBackendStatus(showErrors = false) {
  try {
    const scan = await invoke<OrphanScan>("get_orphaned_backends");
    if (scan.managerLockedElsewhere) {
      // Another live manager owns the lock; never kill its backend from this window.
      conflictingBackendPresent = true;
      orphanPanel.classList.add("hidden");
      orphanDetail.textContent = translateKey("msg.a_live_rdt_connect_manager_owns_this_session.0a86c9d3", {}, getActiveLocale());
      statusPill.textContent = translateKey("msg.in_use.17a97caa", {}, getActiveLocale());
      statusPill.className = "status-pill manager-elsewhere";
      statusDetail.textContent = translateKey("msg.rdt_connect_is_already_running.2376999c", {}, getActiveLocale());
      connectionSummary.textContent = translateKey("msg.use_the_existing_rdt_connect_window.8c0b38b2", {}, getActiveLocale());
      connectionStatus.textContent = translateKey("msg.another_live_rdt_connect_manager_owns_the_se.629294bf", {}, getActiveLocale());
      connectionStatus.className = "connection-status disconnected";
      setBusy(busy);
      return;
    }

    const instances = scan.instances || [];
    if (instances.length) {
      // These entries were identified by the native process scanner as this app's
      // packaged backend. The native stop command repeats executable/tree checks.
      orphanPanel.classList.add("hidden");
      orphanDetail.textContent = translateKey("msg.checking_and_stopping_a_leftover_rdt_connect.17e22da5", {}, getActiveLocale());
      let attemptedNow = false;
      for (const instance of instances) {
        if (autoCleanupAttemptedPids.has(instance.pid)) continue;
        autoCleanupAttemptedPids.add(instance.pid);
        attemptedNow = true;
        try {
          await invoke("stop_orphaned_backend", { pid: instance.pid });
        } catch (error) {
          orphanDetail.textContent = translateKey("msg.could_not_automatically_stop_verified_proces.4acc1f2f", { pid: instance.pid, error: formatHardwareDeviceText(String(error)) }, getActiveLocale());
        }
      }
      const after = await invoke<OrphanScan>("get_orphaned_backends");
      const remaining = after.instances || [];
      if (!remaining.length) {
        conflictingBackendPresent = false;
        autoCleanupFailureAnnounced = false;
        orphanDetail.textContent = attemptedNow ? translateKey("msg.an_earlier_rdt_connect_backend_was_stopped_a.b6f0fe7d", {}, getActiveLocale()) : translateKey("msg.no_previous_backend_detected.06b160ff", {}, getActiveLocale());
        if (!backend.running) {
          statusPill.textContent = translateKey("msg.stopped.133c4945", {}, getActiveLocale());
          statusPill.className = "status-pill stopped";
          statusDetail.textContent = translateKey("msg.your_server_is_stopped.927176a5", {}, getActiveLocale());
          connectionSummary.textContent = translateKey("msg.start_the_server_to_check_the_connection.35c28f4d", {}, getActiveLocale());
        }
        setBusy(busy);
        return;
      }
      conflictingBackendPresent = true;
      orphanPanel.classList.remove("hidden");
      orphanUserMessage.textContent = translateKey("msg.automatic_cleanup_did_not_complete_open_deve.4c41b73d", {}, getActiveLocale());
      orphanDetail.textContent = remaining.map((item) => `PID ${item.pid} · port ${item.port ?? translateKey("msg.unknown.b23a6a84", {}, getActiveLocale())} · ${item.description}`).join("\n");
      statusPill.textContent = translateKey("msg.recovery_needed.85e5d2f1", {}, getActiveLocale());
      statusPill.className = "status-pill attention";
      statusDetail.textContent = translateKey("msg.rdt_connect_could_not_clean_up_an_earlier_ba.31f54524", {}, getActiveLocale());
      connectionSummary.textContent = translateKey("msg.server_recovery_needs_attention.6f185817", {}, getActiveLocale());
      connectionStatus.textContent = translateKey("msg.automatic_cleanup_did_not_complete_open_deve.4c41b73d", {}, getActiveLocale());
      connectionStatus.className = "connection-status disconnected";
      if (!autoCleanupFailureAnnounced || showErrors) {
        showNotice(translateKey("msg.rdt_connect_couldn_t_stop_an_earlier_server.b98c9cd5", {}, getActiveLocale()), "error");
        autoCleanupFailureAnnounced = true;
      }
      setBusy(busy);
      return;
    }

    conflictingBackendPresent = false;
    orphanPanel.classList.add("hidden");
    orphanDetail.textContent = translateKey("msg.no_previous_backend_detected.06b160ff", {}, getActiveLocale());
    if (!backend.running) {
      statusPill.textContent = translateKey("msg.stopped.133c4945", {}, getActiveLocale());
      statusPill.className = "status-pill stopped";
      statusDetail.textContent = translateKey("msg.your_server_is_stopped.927176a5", {}, getActiveLocale());
      connectionSummary.textContent = translateKey("msg.start_the_server_to_check_the_connection.35c28f4d", {}, getActiveLocale());
    }
    setBusy(busy);
  } catch (error) {
    // Do not claim cleanup succeeded when process inspection itself failed.
    // Keep the primary control locked and give the user one actionable explanation.
    if (!autoCleanupFailureAnnounced || showErrors) {
      showNotice(translateKey("msg.rdt_connect_couldn_t_verify_that_an_earlier.f98562c3", {}, getActiveLocale()), "warning");
      autoCleanupFailureAnnounced = true;
    }
    orphanDetail.textContent = translateKey("msg.could_not_verify_local_server_state_error.19b913a5", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale());
    conflictingBackendPresent = true;
    orphanPanel.classList.remove("hidden");
    orphanUserMessage.textContent = translateKey("msg.rdt_connect_couldn_t_verify_that_an_earlier.f98562c3", {}, getActiveLocale());
    startButton.disabled = true;
  }
}

async function refreshRemoteStatus(showErrors = false) {
  if (!backend.running) {
    tailscale = {};
    renderRemoteStatus();
    return;
  }
  try {
    tailscale = await invoke<TailscaleStatus>("get_tailscale_status");
    renderRemoteStatus();
  } catch (e) {
    tailscale = { available: false, ready: false, message: String(e) };
    renderRemoteStatus();
    if (showErrors) showNotice(String(e), "error");
  }
}
function renderRemoteStatus() {
  const url = String(tailscale.public_url || "");
  const privateUrlReady = Boolean(tailscale.ready && /^https:\/\//i.test(url));
  remoteState.textContent = privateUrlReady
    ? ""
    : (tailscale.message || (backend.running ? translateKey("msg.check_tailscale_to_configure_private_https_a.2c942b1f", {}, getActiveLocale()) : translateKey("msg.start_the_local_server_first.2f7c0df0", {}, getActiveLocale())));
  remoteState.className = `remote-state ${privateUrlReady ? "hidden" : !backend.running ? "warning" : ""}`;
  setupRemoteButton.textContent = tailscale.ready ? translateKey("msg.refresh_private_access.978c0605", {}, getActiveLocale()) : translateKey("msg.set_up_remote_access.aab18c87", {}, getActiveLocale());
  remoteActionHelp.textContent = !backend.running
    ? translateKey("msg.start_the_local_server_first_this_does_not_e.9ed0d79f", {}, getActiveLocale())
    : !tailscale.ready
      ? translateKey("msg.make_sure_tailscale_is_installed_and_connect.8400059a", {}, getActiveLocale())
      : !pairingQr
        ? translateKey("msg.copy_the_private_url_for_manual_setup_or_gen.9060ab93", {}, getActiveLocale())
        : translateKey("msg.the_pairing_qr_is_single_use_expires_automat.f9e19abb", {}, getActiveLocale());
  generateQrButton.disabled = busy || !backend.running || !tailscale.ready;
  if (tailscale.ready && url && /^https:\/\//i.test(url)) {
    remoteUrlValue.textContent = url;
    $("remote-url-row").classList.remove("hidden");
    copyRemoteUrlButton.disabled = busy;
  } else {
    remoteUrlValue.textContent = "";
    $("remote-url-row").classList.add("hidden");
    copyRemoteUrlButton.disabled = true;
  }
  setBusy(busy);
}

async function loadPairRequests() {
  try {
    const result = await invoke<{ requests: PairRequest[] }>("list_pair_requests");
    const incoming = Array.isArray(result.requests) ? result.requests : [];
    const hasNewRequest = incoming.some((request) => !knownPendingRequestIds.has(request.id));
    const qrWasUsed = Boolean(hasNewRequest && pairingQrUsable());
    if (qrWasUsed) {
      // A phone has submitted a request with the single-use QR. Hide the overlay
      // immediately so Approve/Deny become available, and visually retire that QR.
      qrExpandedOverlay.classList.add("hidden");
      markPairingQrUsed();
    }
    knownPendingRequestIds = new Set(incoming.map((request) => request.id));
    clearChildren(pendingList);
    if (!incoming.length) {
      pendingList.appendChild(emptyMessage("No pending requests. When a phone scans your QR, approve it here."));
      return;
    }
    for (const request of incoming) {
      const item = document.createElement("div"); item.className = "list-item";
      const copy = document.createElement("div"); copy.className = "list-copy";
      const title = document.createElement("div"); title.className = "list-title"; title.textContent = request.device_label || translateKey("msg.unknown_browser.7bcdf756", {}, getActiveLocale());
      const details = document.createElement("div"); details.className = "list-details"; details.textContent = translateKey("msg.browser_expires_expires_at.60f297eb", { browser: shortAgent(request.user_agent || translateKey("msg.browser.d31de1a5", {}, getActiveLocale())), expires_at: fmtTime(request.expires_at) }, getActiveLocale());
      copy.append(title, details);
      const actions = document.createElement("div"); actions.className = "list-actions";
      const approve = document.createElement("button"); approve.className = "mini-button approve"; approve.textContent = translateKey("msg.approve.6007acbe", {}, getActiveLocale());
      approve.addEventListener("click", async () => runAction(async () => {
        await invoke("approve_pair_request", { requestId: request.id });
        markPairingQrUsed();
        showNotice(translateKey("msg.browser_approved.18f698ae", {}, getActiveLocale()), "good");
        await refreshDeviceLists();
      }));
      const deny = document.createElement("button"); deny.className = "mini-button deny"; deny.textContent = translateKey("msg.deny.05a2d733", {}, getActiveLocale());
      deny.addEventListener("click", async () => runAction(async () => {
        await invoke("deny_pair_request", { requestId: request.id });
        markPairingQrUsed();
        showNotice(translateKey("msg.pairing_request_denied.4ba7cb73", {}, getActiveLocale()), "info");
        await refreshDeviceLists();
      }));
      actions.append(approve, deny); item.append(copy, actions); pendingList.appendChild(item);
    }
    if (qrWasUsed) {
      // Put keyboard focus on the first actionable approval control after the
      // expanded QR has closed and the pending-request controls are rendered.
      pendingList.querySelector<HTMLButtonElement>(".mini-button.approve")?.focus();
    }
  } catch (e) { pendingList.replaceChildren(emptyMessage(`Could not load requests: ${String(e)}`)); }
}
async function loadDevices() {
  try {
    const result = await invoke<{ devices: PairedDevice[] }>("list_paired_devices");
    clearChildren(deviceList);
    if (!result.devices.length) {
      deviceList.appendChild(emptyMessage("No paired browsers yet."));
      return;
    }
    for (const device of result.devices) {
      const item = document.createElement("div"); item.className = "list-item";
      const copy = document.createElement("div"); copy.className = "list-copy";
      const title = document.createElement("div"); title.className = "list-title"; title.textContent = device.device_label || translateKey("msg.browser.d31de1a5", {}, getActiveLocale());
      const details = document.createElement("div"); details.className = "list-details"; details.textContent = translateKey("msg.last_used_last_seen_browser.f1996490", { last_seen: fmtTime(device.last_seen), browser: shortAgent(device.user_agent || translateKey("msg.browser.d31de1a5", {}, getActiveLocale())) }, getActiveLocale());
      copy.append(title, details);
      const revoke = document.createElement("button"); revoke.className = "mini-button deny"; revoke.textContent = translateKey("msg.revoke.87e6d00b", {}, getActiveLocale());
      revoke.addEventListener("click", async () => runAction(async () => {
        await invoke("revoke_paired_device", { sessionId: device.id });
        showNotice(translateKey("msg.browser_access_revoked.bbf3f10e", {}, getActiveLocale()), "good"); await loadDevices();
      }));
      item.append(copy, revoke); deviceList.appendChild(item);
    }
  } catch (e) { deviceList.replaceChildren(emptyMessage(`Could not load devices: ${String(e)}`)); }
}
async function refreshDeviceLists() { await Promise.all([loadPairRequests(), loadDevices()]); }

async function runAction(action: () => Promise<unknown>, successMessage?: string) {
  setBusy(true);
  try { await action(); if (successMessage) showNotice(successMessage, "good"); }
  catch (e) {
    const detail = String(e);
    if (/address already in use|eaddrinuse|os error 48|address in use|bind[^\n]*failed/i.test(detail)) {
      const port = Number(preferredPortInput.value) || 8888;
      showNotice(translateKey("msg.port_port_is_busy_enable_developer_options_c.ceeaeb77", { port: port }, getActiveLocale()), "warning");
    } else {
      showNotice(detail, "error");
    }
  }
  finally {
    await refreshBackendStatus();
    await refreshOrphanedBackendStatus();
    setBusy(false);
    renderBackendStatus();
  }
}


let modelAvailabilityCheckInFlight = false;
function nudgeModelDownloadButton(): void {
  if (modelDownloadButton.hidden || modelDownloadButton.disabled) return;
  modelDownloadButton.classList.remove("needs-download-attention");
  void modelDownloadButton.offsetWidth;
  modelDownloadButton.classList.add("needs-download-attention");
  window.setTimeout(() => modelDownloadButton.classList.remove("needs-download-attention"), 4200);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  modelDownloadButton.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
  modelDownloadButton.focus({ preventScroll: true });
}

async function startSelectedBackendModel(preverifiedCache?: ModelStorageStatus) {
  const selectedCache = preverifiedCache ?? await requestModelStorageStatus(modelSelect.value);
  modelStorageStatus = selectedCache;
  renderModelStorageSummary(selectedCache);
  renderModelDownloadUI();
  if (!selectedCache.modelCached) {
    nudgeModelDownloadButton();
    return;
  }
  await runAction(async () => {
    await inferenceDevicePreferenceReady;
    const current = await invoke<BackendStatus>("get_backend_status");
    let startedBackendHere = false;
    if (current.running) {
      if (current.model !== modelSelect.value || !current.pid) {
        throw new Error("A different or unverified backend is already running. RDT Connect left it untouched; use Stop Server before starting another process.");
      }
      backend = current;
    } else {
      backend = await invoke<BackendStatus>("start_backend", {
        model: modelSelect.value,
        preferredPort: Number(preferredPortInput.value) || 8888,
      });
      startedBackendHere = true;
    }
    renderBackendStatus();
    await refreshConnectionStatus();
    if (!backend.running || !backend.pid || backend.model !== modelSelect.value
        || lastConnectionCheck?.healthOk !== true || lastConnectionCheck.authOk !== true) {
      throw new Error("The backend did not pass authenticated health verification. Any running process was left untouched.");
    }
    if (startedBackendHere) {
      appliedInferenceDevicePreference = inferenceDevicePreferenceReadError
        ? null
        : inferenceDevicePreference;
    }
    renderInferenceHardwareStatus();
    if (browserPreflightPassed()) {
      showNotice(translateKey("msg.rdt_connect_started_on_localurl_server_and_a.e4c94cb9", { localUrl: backend.localUrl ?? "" }, getActiveLocale()), "good");
    } else {
      showNotice(translateKey("msg.the_server_started_but_open_rdt_voice_is_dis.086dfa50", { browserPreflightFailure: browserPreflightFailure() }, getActiveLocale()), "warning");
    }
    await refreshRemoteStatus();
  });
}

startButton.addEventListener("click", async () => {
  if (busy || benchmarkRunInProgress) return;
  setBusy(true);
  try {
    backend = await invoke<BackendStatus>("get_backend_status");
  } catch (error) {
    setBusy(false);
    showNotice(formatHardwareDeviceText(String(error)), "error");
    return;
  }
  renderBackendStatus();
  renderBrowserChoiceRow();
  const wasRunning = backend.running;
  const requestedModel = modelSelect.value;
  let precheckedStartupCache: ModelStorageStatus | null = null;
  if (!wasRunning) {
    if (modelAvailabilityCheckInFlight) { setBusy(false); return; }
    modelAvailabilityCheckInFlight = true;
    try {
      const cache = await requestModelStorageStatus(requestedModel);
      modelStorageStatus = cache;
      renderModelStorageSummary(cache);
      renderModelDownloadUI();
      if (!cache.modelCached) {
        showNotice(`Download ${requestedModel} before starting the server.`, "warning");
        nudgeModelDownloadButton();
        setBusy(false);
        return;
      }
      precheckedStartupCache = cache;
    } catch (error) {
      showNotice(`Could not verify the selected model cache; the server was not started. ${formatHardwareDeviceText(String(error))}`, "error");
      setBusy(false);
      return;
    } finally {
      modelAvailabilityCheckInFlight = false;
    }
  }

  // Missing model: guide attention through motion and focus, not text.
  if (!wasRunning && !modelDownloadButton.hidden
      && !modelDownloadButton.disabled && !modelDownloadStatus.active) {
    const downloadPanel =
      modelDownloadButton.closest<HTMLElement>(".model-download-panel");

    modelDownloadButton.classList.remove("needs-model-download-target");
    if (downloadPanel) {
      downloadPanel.classList.remove("needs-model-download");
      void downloadPanel.offsetWidth;
      downloadPanel.classList.add("needs-model-download");
      downloadPanel.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto" : "smooth",
        block: "nearest",
      });
    }
    void modelDownloadButton.offsetWidth;
    modelDownloadButton.classList.add("needs-model-download-target");
    modelDownloadButton.focus({ preventScroll: true });
    setBusy(false);
    return;
  }
  if (!wasRunning && precheckedStartupCache?.modelCached && (!isFirstRunHardwareOptimizationResolved() || isQaFirstRunHardwareOptimizationArmed())) {
    const https = await refreshLocalHttpsStatus(false);
    if (!https || https.mode !== "https" || !https.configured || !https.trusted) {
      showNotice("Set up and trust local HTTPS before the first hardware check. Then click Start Server again; RDT Connect will run the check automatically.", "warning");
      const securitySection = document.getElementById("settings-security-section");
      securitySection?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
      setBusy(false);
      return;
    }
    try {
      const qaFirstRunSimulation = consumeQaFirstRunHardwareOptimization();
      const optimizationChoice = await runHardwareOptimizationFlow("first-run", qaFirstRunSimulation);
      if (optimizationChoice === "manual") { setBusy(false); return; }
      if (optimizationChoice === "optimized" && backend.running && backend.model === requestedModel) {
        showNotice("Your selected model is already running on the recommended hardware. RDT Voice is ready to open.", "good");
        setBusy(false);
        return;
      }
    } catch (error) {
      showNotice(`Hardware optimization could not safely release its managed server. The normal server was not started. ${formatHardwareDeviceText(String(error))}`, "error");
      setBusy(false);
      return;
    }
  }

      const wantsSwitch = wasRunning && !!backend.model && requestedModel !== backend.model;
  if (wantsSwitch) {
    if (modelDownloadStatus.active || modelStorageBusy) {
      showNotice(translateKey("msg.finish_or_cancel_model_downloads_and_storage_changes_before_swit.77238833", {}, getActiveLocale()), "warning");
      setBusy(false);
      return;
    }
    setBusy(true);
    try {
      const selectedCache = await requestModelStorageStatus(requestedModel);
      modelStorageStatus = selectedCache;
      renderModelStorageSummary(selectedCache);
      if (!selectedCache.modelCached) {
        setBusy(false);
        nudgeModelDownloadButton();
        return;
      }
    } catch (error) {
      showNotice(translateKey("msg.could_not_verify_the_selected_model_cache_the_current_server_was.4456b302", { arg1: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
      setBusy(false);
      return;
    }
    modelSwitchTargetAfterStop = requestedModel;
  }
  serverOperation = wasRunning ? "stopping" : "starting";
  startButton.classList.add(wasRunning ? "is-stopping" : "is-starting");
  startButton.setAttribute("aria-busy", "true");
  startProgress.classList.remove("hidden");
  startProgress.setAttribute("aria-busy", "true");
  startProgressText.textContent = wasRunning ? (wantsSwitch ? translateKey("msg.stopping_before_switching_models.fb0c9aa6", {}, getActiveLocale()) : translateKey("msg.stopping_your_local_server.dfa3b390", {}, getActiveLocale())) : translateKey("msg.starting_your_local_server.f6149329", {}, getActiveLocale());
  startProgressDescription.textContent = wasRunning
    ? benchmarkMessage("msg.local_server_stopping_detail.1d20")
    : benchmarkMessage("msg.local_server_starting_detail.1d20");
  showNotice(wasRunning ? (wantsSwitch ? translateKey("msg.stopping_the_current_server_before_switching_models.c41d642a", {}, getActiveLocale()) : translateKey("msg.closing_the_local_server_safely.ebce561c", {}, getActiveLocale())) : translateKey("msg.preparing_your_local_server.b0fafab7", {}, getActiveLocale()), "info");
  renderBackendStatus();
  renderBrowserChoiceRow();
  await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  try {
    if (wasRunning) {
      await runAction(async () => {
        backend = await invoke<BackendStatus>("stop_backend");
        pairingQr = null; qrImage.classList.add("hidden"); qrPlaceholder.classList.remove("hidden");
        qrCountdown.textContent = "";
        await refreshRemoteStatus();
        showNotice(translateKey("msg.local_server_stopped_its_managed_process_cle.2a3421b6", {}, getActiveLocale()), "warning");
      });
      if (modelSwitchTargetAfterStop !== null) {
        const targetModel = modelSwitchTargetAfterStop;
        modelSwitchTargetAfterStop = null;
        if (backend.running) {
          showNotice(translateKey("msg.the_current_server_could_not_be_confirmed_stopped_the_replacemen.c93371b4", {}, getActiveLocale()), "error");
          return;
        }
        if (conflictingBackendPresent || modelDownloadStatus.active || modelStorageBusy) {
          showNotice(translateKey("msg.the_server_stopped_but_another_backend_conflict_or_active_model.0db27a3f", {}, getActiveLocale()), "warning");
          return;
        }
        modelSelect.value = targetModel;
        modelSelect.dispatchEvent(new Event("change", { bubbles: true }));
        serverOperation = "starting";
        startButton.classList.remove("is-stopping");
        startButton.classList.add("is-starting");
        startProgressText.textContent = translateKey("msg.starting_the_selected_model.4d384f5a", {}, getActiveLocale());
        startProgressDescription.textContent = translateKey("msg.the_previous_server_is_confirmed_stopped_starting_the_replacemen.a40640b4", {}, getActiveLocale());
        showNotice(translateKey("msg.the_previous_server_is_stopped_starting_the_selected_model.b3fd7cda", {}, getActiveLocale()), "info");
        renderBackendStatus();
        await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
        await startSelectedBackendModel(precheckedStartupCache ?? undefined);
      }
    } else {
      await startSelectedBackendModel(precheckedStartupCache ?? undefined);
    }
  } finally {
    modelSwitchTargetAfterStop = null;
    serverOperation = null;
    startButton.classList.remove("is-starting", "is-stopping");
    startButton.removeAttribute("aria-busy");
    startProgress.classList.add("hidden");
    startProgress.removeAttribute("aria-busy");
    renderBackendStatus();
  }
});

const UPDATE_SUCCESS_STORAGE_KEY = "rdt_connect_pending_update_success_1d40";
type PendingUpdateSuccess = { version: string; buildId: string; installedAt: number };

function recordSuccessfulUpdateInstall(version: unknown): void {
  if (typeof version !== "string" || !/^.+\.1D\.\d+$/i.test(version)) return;
  const marker = version.match(/\.1D\.(\d+)$/i);
  if (!marker) return;
  const record: PendingUpdateSuccess = {
    version,
    buildId: `1D${marker[1]}`,
    installedAt: Date.now(),
  };
  try {
    localStorage.setItem(UPDATE_SUCCESS_STORAGE_KEY, JSON.stringify(record));
  } catch {
    // Storage may be unavailable; update installation must still succeed.
  }
}

function showUpdateSuccessDialog(version: string, buildId: string): void {
  const overlay = document.getElementById("rdt-update-success-dialog");
  const versionValue = document.getElementById("rdt-update-success-version");
  const buildValue = document.getElementById("rdt-update-success-build");
  const continueButton = document.getElementById("rdt-update-success-continue") as HTMLButtonElement | null;
  if (!overlay || !versionValue || !buildValue || !continueButton) return;

  versionValue.textContent = version;
  buildValue.textContent = buildId;
  const locale = getActiveLocale();
  const title = document.getElementById("rdt-update-success-title");
  const body = document.getElementById("rdt-update-success-body");
  const versionLabel = document.getElementById("rdt-update-success-version-label");
  const buildLabel = document.getElementById("rdt-update-success-build-label");
  if (title) title.textContent = translateKey("msg.update_success_title.1d40", {}, locale);
  if (body) body.textContent = translateKey("msg.update_success_body.1d40", {}, locale);
  if (versionLabel) versionLabel.textContent = translateKey("msg.update_success_version.1d40", {}, locale);
  if (buildLabel) buildLabel.textContent = translateKey("msg.update_success_build.1d40", {}, locale);
  continueButton.textContent = translateKey("msg.update_success_continue.1d40", {}, locale);

  const close = () => {
    overlay.setAttribute("hidden", "");
    overlay.setAttribute("aria-hidden", "true");
    continueButton.removeEventListener("click", close);
    document.removeEventListener("keydown", onKeyDown);
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Escape") close();
  };
  continueButton.addEventListener("click", close, { once: true });
  document.addEventListener("keydown", onKeyDown);
  overlay.removeAttribute("hidden");
  overlay.setAttribute("aria-hidden", "false");
  continueButton.focus();
}

function consumePendingUpdateSuccess(actualVersion: string, actualBuildId: string): void {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(UPDATE_SUCCESS_STORAGE_KEY);
    if (!raw) return;
    // Consume before display so reloads and repeated metadata refreshes cannot show it twice.
    localStorage.removeItem(UPDATE_SUCCESS_STORAGE_KEY);
    const record = JSON.parse(raw) as Partial<PendingUpdateSuccess>;
    if (
      typeof record.version !== "string" ||
      typeof record.buildId !== "string" ||
      typeof record.installedAt !== "number" ||
      !Number.isFinite(record.installedAt) ||
      Date.now() - record.installedAt < 0 ||
      Date.now() - record.installedAt > 30 * 24 * 60 * 60 * 1000
    ) return;
    if (record.version !== actualVersion || record.buildId !== actualBuildId) return;
    showUpdateSuccessDialog(actualVersion, actualBuildId);
  } catch {
    try { localStorage.removeItem(UPDATE_SUCCESS_STORAGE_KEY); } catch { /* best effort */ }
  }
}

async function refreshAppBuildInfo() {
  try {
    const info = await invoke<AppBuildInfo>("get_app_build_info");
    if (info && typeof info.version === "string" && typeof info.buildId === "string") {
      const displayVersion = formatProductVersionLabel(info.version);
      headerVersionLabel.textContent = displayVersion;
      headerBuildId.textContent = info.buildId;
      aboutVersionLabel.textContent = translateKey("msg.rdt_connect_app_version_label_build_build_id.62522b9b", { APP_VERSION_LABEL: displayVersion, BUILD_ID: info.buildId }, getActiveLocale());
      consumePendingUpdateSuccess(info.version, info.buildId);
    }
  } catch {
    // Keep the source-provided version/build fallback visible if the native metadata call fails.
  }
}
let updateCheckInFlight: Promise<void> | null = null;
let pendingTauriUpdate: TauriUpdate | null = null;
let updateInstallInProgress = false;

type UpdateStatus =
  | "idle"
  | "checking"
  | "up-to-date"
  | "offline"
  | "available"
  | "downloading"
  | "installing"
  | "restart-required"
  | "error";

type UpdateAction = "check" | "install" | "restart" | null;
type UpdateWindowAction = "install" | "installing" | "reopen" | null;

let updateStatus: UpdateStatus = "idle";
let updateUiMessageKey = "updater.checking";
let updateUiMessageParams: Record<string, string> = {};
let updateInstalledAwaitingRelaunch = false;

function getUpdateAction(status: UpdateStatus): UpdateAction {
  switch (status) {
    case "idle":
    case "up-to-date":
    case "offline":
    case "error":
      return "check";
    case "available":
      return "install";
    case "restart-required":
      return "restart";
    default:
      return null;
  }
}

function getUpdateWindowAction(status: UpdateStatus): UpdateWindowAction {
  if (status === "available") return "install";
  if (status === "downloading" || status === "installing") return "installing";
  if (status === "restart-required") return "reopen";
  return null;
}

function setApplicationUpdateStatus(status: UpdateStatus): void {
  updateStatus = status;

  const nativeState =
    status === "available" ? "available" :
    status === "checking" ? "checking" :
    status === "downloading" || status === "installing" ? "downloading" :
    status === "restart-required" ? "restart" :
    status === "up-to-date" ? (autoCheckUpdatesToggle.checked ? "up-to-date-auto" : "up-to-date-manual") :
    status === "offline" ? "offline" :
    status === "error" ? "error" : null;

  void invoke("set_update_indicator", {
    state: nativeState,
    displayText: status === "up-to-date"
      ? (autoCheckUpdatesToggle.checked
        ? "RDT Connect is up to date"
        : "Check for Update")
      : updateMessage(updateUiMessageKey, updateUiMessageParams),
  }).catch(() => undefined);

  const windowAction = getUpdateWindowAction(status);
  const buttons = [applicationUpdateActionButton, headerUpdateActionButton].filter(
    (button): button is HTMLButtonElement => button !== null,
  );
  for (const button of buttons) {
    button.hidden = windowAction === null;
    button.disabled = status === "downloading" || status === "installing";
    if (windowAction === "install") button.textContent = updateMessage("updater.downloadInstall");
    else if (windowAction === "installing") button.textContent = updateMessage("updater.downloading");
    else if (windowAction === "reopen") button.textContent = updateMessage("updater.reopenApp");
  }

  publishUpdateWindowState();
}
function publishUpdateWindowState(): void {
  void emit("rdt-connect-update-status", {
    key: updateUiMessageKey,
    params: updateUiMessageParams,
    action: getUpdateWindowAction(updateStatus),
  }).catch(() => undefined);
}
function setApplicationUpdateAction(
  action: "install" | "installing" | "reopen" | null,
): void {
  const status: UpdateStatus =
    action === "install"
      ? "available"
      : action === "installing"
        ? "downloading"
        : action === "reopen"
          ? "restart-required"
          : "idle";
  setApplicationUpdateStatus(status);
}

function updateMessage(key: string, params: Record<string, string> = {}): string {
  return translateKey(key, params, getActiveLocale());
}
function setApplicationUpdateMessage(key: string, params: Record<string, string> = {}): void {
  updateUiMessageKey = key;
  updateUiMessageParams = { ...params };
  applicationUpdateMessage.textContent = updateMessage(key, params);
  applicationUpdateMessage.classList.toggle("is-up-to-date", key === "updater.upToDate");
  applicationUpdateMessage.classList.toggle("is-update-installed", key === "updater.installedReopen");

  const nativeState =
    updateStatus === "up-to-date" ? "up-to-date" :
    updateStatus === "offline" ? "offline" :
    updateStatus === "error" ? "error" :
    updateStatus === "available" ? "available" :
    updateStatus === "checking" ? "checking" :
    updateStatus === "downloading" || updateStatus === "installing" ? "downloading" :
    updateStatus === "restart-required" ? "restart" : null;

  void invoke("set_update_indicator", {
    state: nativeState,
    displayText: updateMessage(key, params),
  }).catch(() => undefined);

  publishUpdateWindowState();
}
function updateErrorDetail(error: unknown): string {
  const raw = formatHardwareDeviceText(error instanceof Error ? error.message : String(error));
  return (raw.replace(/\s+/g, " ").trim() || "Unknown error").slice(0, 260);
}
function getVersionDisplayParts(version: string): { base: string; build: string | null } {
  const normalized = version.trim().replace(/^v/i, "");

  // Current whole-marker format: .2A05, never .2A.05.
  const wholeMarker = normalized.match(/^(.*)\.([0-5][A-Z]\d{2})$/);
  if (wholeMarker) {
    return { base: wholeMarker[1], build: wholeMarker[2] };
  }

  // Preserve display compatibility with historical releases only.
  const legacy = normalized.match(/^(.*)\.([0-9][A-Z])\.(\d+)$/);
  if (!legacy) return { base: normalized, build: null };

  return {
    base: legacy[1],
    build: `${legacy[2]}${legacy[3].padStart(2, "0")}`,
  };
}
function formatProductVersionLabel(version: string): string {
  return `v${getVersionDisplayParts(version).base}`;
}
function formatFriendlyUpdateVersion(version: string): string {
  const { base, build } = getVersionDisplayParts(version);
  return build ? `v${base} Build ${build}` : `v${base}`;
}
function formatUpdateBytes(value: number): string {
  const bytes = Math.max(0, value);
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = bytes / 1024;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${new Intl.NumberFormat(getActiveLocale(), { maximumFractionDigits: 1 }).format(amount)} ${units[index]}`;
}
function isMissingDevelopmentFeed(errorText: string): boolean {
  return /development update feed|development release feed|dev(?:elopment)?[-_ ]feed.*(?:404|not found)|(?:404|not found).*dev(?:elopment)?[-_ ]feed/i.test(errorText);
}

let updateCheckGeneration = 0;
let updateCheckInFlightManual = false;

function canRunAutomaticUpdateCheck(): boolean {
  return autoCheckUpdatesToggle.checked &&
    !offlineOnlyModeEnabled &&
    !(typeof navigator !== "undefined" && navigator.onLine === false) &&
    !updateCheckInFlight &&
    !pendingTauriUpdate &&
    !updateInstallInProgress &&
    !updateInstalledAwaitingRelaunch;
}

function checkForApplicationUpdates(manual = false): Promise<void> {
  if (updateInstallInProgress || updateInstalledAwaitingRelaunch) return Promise.resolve();
  if (!manual && offlineOnlyModeEnabled) return Promise.resolve();
  if (!manual && pendingTauriUpdate) return Promise.resolve();
  // Keep an available update alive; periodic automatic checks must not close it.
  if (!manual && pendingTauriUpdate) return Promise.resolve();
  if (!manual && typeof navigator !== "undefined" && navigator.onLine === false) {
    setApplicationUpdateStatus("offline");
    setApplicationUpdateMessage("updater.offline");
    return Promise.resolve();
  }
  if (updateCheckInFlight) {
    if (!manual || updateCheckInFlightManual) return updateCheckInFlight;
    updateCheckGeneration += 1;
    updateCheckInFlight = null;
    updateCheckInFlightManual = false;
  }
  const generation = updateCheckGeneration;
  updateCheckInFlightManual = manual;
  updateCheckInFlight = (async () => {
    if (pendingTauriUpdate) {
      const previous = pendingTauriUpdate;
      pendingTauriUpdate = null;
      try { await previous.close(); } catch { /* closing a superseded check is best-effort */ }
      if (generation !== updateCheckGeneration) return;
    }
    if (generation !== updateCheckGeneration) return;
    setApplicationUpdateStatus("checking");
    setApplicationUpdateMessage("updater.checking");
    try {
      const update = await checkForTauriUpdate({
          timeout: manual ? 30000 : 5000,
          headers: {
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
          },
        });
      if (generation !== updateCheckGeneration) {
        if (update) { try { await update.close(); } catch { /* stale result cleanup */ } }
        return;
      }
      if (!update) {
        setApplicationUpdateStatus("up-to-date");
        setApplicationUpdateMessage("updater.upToDate");
        return;
      }
      pendingTauriUpdate = update;
      setApplicationUpdateStatus("available");
      setApplicationUpdateMessage("updater.available", { VERSION: formatFriendlyUpdateVersion(update.version) });
    } catch (error) {
      if (generation !== updateCheckGeneration) return;
      const detail = updateErrorDetail(error);
      const offline =
        (typeof navigator !== "undefined" && navigator.onLine === false) ||
        /failed to fetch|networkerror|network request failed|internet connection|offline|timed?\s*out|timeout|connection (?:refused|reset|closed|aborted)|could not resolve|dns|name resolution|network is unreachable|no route to host|temporarily unavailable/i.test(detail);
      setApplicationUpdateStatus(offline ? "offline" : "error");
      if (offline && !manual) {
        setApplicationUpdateMessage("updater.offline");
        setApplicationUpdatePanelOpen(false);
      } else if (offline) {
        setApplicationUpdateMessage("updater.offline");
      } else if (isMissingDevelopmentFeed(detail)) {
        setApplicationUpdateMessage("updater.feedMissing");
      } else if (/could not fetch a valid release json/i.test(detail)) {
        setApplicationUpdateMessage("updater.checkFailed", { ERROR: "GitHub did not return a valid update manifest. This installed build has not changed." });
      } else {
        setApplicationUpdateMessage("updater.checkFailed", { ERROR: detail });
      }
    } finally {
      if (generation === updateCheckGeneration) {
        if (updateInstalledAwaitingRelaunch) setApplicationUpdateStatus("restart-required");
        else if (pendingTauriUpdate) setApplicationUpdateStatus("available");
        updateCheckInFlight = null;
        updateCheckInFlightManual = false;
      }
    }
  })();
  return updateCheckInFlight;
}

async function installAvailableApplicationUpdate(): Promise<void> {
  if (updateInstalledAwaitingRelaunch) {
    try {
      await relaunch();
    } catch {
      setApplicationUpdateMessage("updater.restartFailed");
      setApplicationUpdateStatus("restart-required");
    }
    return;
  }
  if (updateInstallInProgress || !pendingTauriUpdate) return;
  // Prevent an outstanding check from publishing stale status during installation.
  updateCheckGeneration += 1;
  updateCheckInFlight = null;
  updateCheckInFlightManual = false;
  const update = pendingTauriUpdate;
  updateInstallInProgress = true;
  aboutCheckUpdatesButton.disabled = true;
  headerCheckUpdatesButton.disabled = true;
  setApplicationUpdateStatus("downloading");
  let downloadedBytes = 0;
  let totalBytes: number | null = null;
  setApplicationUpdateMessage("updater.downloading");
  try {
    await update.download((event: TauriDownloadEvent) => {
      if (event.event === "Started") {
        totalBytes = event.data.contentLength ?? null;
        setApplicationUpdateMessage("updater.downloading");
      } else if (event.event === "Progress") {
        downloadedBytes += event.data.chunkLength;
        if (totalBytes !== null && totalBytes > 0) {
          setApplicationUpdateMessage("updater.progress", {
            DOWNLOADED: formatUpdateBytes(downloadedBytes),
            TOTAL: formatUpdateBytes(totalBytes),
          });
        }
      }
    });
    setApplicationUpdateStatus("installing");
    setApplicationUpdateMessage("updater.downloading");
    await update.install({ restartAfterInstall: false });
    recordSuccessfulUpdateInstall(update.version);
    pendingTauriUpdate = null;
    updateInstalledAwaitingRelaunch = true;
    setApplicationUpdateStatus("restart-required");
    setApplicationUpdateMessage("updater.installedReopen");
  } catch (error) {
    if (updateInstalledAwaitingRelaunch) {
      setApplicationUpdateStatus("restart-required");
      setApplicationUpdateMessage("updater.installedReopen");
    } else {
      setApplicationUpdateStatus("error");
      setApplicationUpdateMessage("updater.installFailed", { ERROR: updateErrorDetail(error) });
    }
    if (pendingTauriUpdate === update) {
      pendingTauriUpdate = null;
      try { await update.close(); } catch { /* installation failure cleanup is best-effort */ }
    }
  } finally {
    updateInstallInProgress = false;
    aboutCheckUpdatesButton.disabled = false;
    headerCheckUpdatesButton.disabled = false;
    if (updateInstalledAwaitingRelaunch) {
      setApplicationUpdateStatus("restart-required");
      setApplicationUpdateMessage("updater.installedReopen");
    } else if (pendingTauriUpdate) {
      setApplicationUpdateStatus("available");
    }
  }
}

function setApplicationUpdatePanelOpen(open: boolean) {
  applicationUpdatePanel.classList.toggle("is-open", open);
  applicationUpdatePanel.setAttribute("aria-hidden", String(!open));
  if (open) applicationUpdatePanel.removeAttribute("inert");
  else applicationUpdatePanel.setAttribute("inert", "");
  aboutCheckUpdatesButton.setAttribute("aria-expanded", String(open));
  aboutCheckUpdatesButton.classList.toggle("is-expanded", open);
}
function showApplicationUpdates(navigateToSettings: boolean) {
  if (navigateToSettings) {
    if (versionDetails) versionDetails.open = false;
    const settingsTab = viewTabs.find((tab) => tab.dataset.viewTarget === "settings");
    if (settingsTab) settingsTab.click();
    else setAppView("settings");
  }
  setApplicationUpdatePanelOpen(true);
  void checkForApplicationUpdates(true);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const destination = navigateToSettings
    ? applicationUpdatePanel.closest<HTMLElement>(".about-card")
    : applicationUpdatePanel;
  window.requestAnimationFrame(() => {
    (destination || applicationUpdatePanel).scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: navigateToSettings ? "center" : "nearest",
    });
  });
}
aboutCheckUpdatesButton.addEventListener("click", () => {
  const expanded = applicationUpdatePanel.classList.contains("is-open");
  if (expanded) setApplicationUpdatePanelOpen(false);
  else showApplicationUpdates(false);
});
headerCheckUpdatesButton.addEventListener("click", () => showApplicationUpdates(true));

void listenNative("rdt-connect-open-about", () => {
  const tabs = Array.from(
    document.querySelectorAll<HTMLButtonElement>(".view-tab")
  );
  const settingsTab = tabs.find((tab) => {
    const marker = [
      tab.id, tab.dataset.view, tab.dataset.target,
      tab.getAttribute("aria-controls"),
    ].filter(Boolean).join(" ").toLowerCase();
    return marker.includes("settings");
  }) ?? tabs.find((tab) => /settings/i.test(tab.textContent ?? ""));

  settingsTab?.click();
  requestAnimationFrame(() => {
    $("settings-updates-section")?.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto" : "smooth",
      block: "start",
    });
  });
}).catch(() => undefined);

void listenNative("rdt-connect-check-updates", () => {
  void checkForApplicationUpdates(true);
}).catch(() => undefined);
void listenNative("rdt-connect-update-window-ready", () => {
  publishUpdateWindowState();
  if (
    canRunAutomaticUpdateCheck() &&
    (
      updateStatus === "idle" ||
      updateStatus === "up-to-date" ||
      updateStatus === "error" ||
      updateStatus === "offline"
    )
  ) {
    void checkForApplicationUpdates();
  }
}).catch(() => undefined);

void listenNative("rdt-connect-update-window-state-request", () => {
  // The popup sends this only after its status listener is registered.
  // Publish the existing state, then recover a tray check that may have
  // arrived before the new popup finished initializing.
  publishUpdateWindowState();

  if (
    !pendingTauriUpdate &&
    !updateInstallInProgress &&
    !updateInstalledAwaitingRelaunch
  ) {
    void checkForApplicationUpdates(true);
  }
}).catch(() => undefined);
void listenNative("rdt-connect-update-check-request", () => {
  void checkForApplicationUpdates(true);
}).catch(() => undefined);
void listenNative("rdt-connect-update-install-request", () => {
  void installAvailableApplicationUpdate();
}).catch(() => undefined);
void listenNative("rdt-connect-update-relaunch-request", () => {
  if (updateInstalledAwaitingRelaunch) void installAvailableApplicationUpdate();
}).catch(() => undefined);
void listenNative("rdt-connect-app-restart-request", async () => {
  if (updateInstallInProgress) return;
  try {
    if (
      autoInstallUpdatesToggle.checked &&
      pendingTauriUpdate &&
      !updateInstalledAwaitingRelaunch
    ) {
      await installAvailableApplicationUpdate();
      if (!updateInstalledAwaitingRelaunch) return;
    }
    await relaunch();
  } catch (error) {
    console.error("[RDT Connect] Restart App failed:", error);
  }
}).catch(() => undefined);
applicationUpdateActionButton.addEventListener("click", () => { void installAvailableApplicationUpdate(); });
if (headerUpdateActionButton) headerUpdateActionButton.addEventListener("click", () => { void installAvailableApplicationUpdate(); });
const AUTO_CHECK_UPDATES_KEY = "rdt-connect-auto-check-updates";
try {
  autoCheckUpdatesToggle.checked = localStorage.getItem(AUTO_CHECK_UPDATES_KEY) !== "false";
} catch {
  autoCheckUpdatesToggle.checked = true;
}
syncAutomaticUpdateToggleOfflineState();
autoCheckUpdatesToggle.addEventListener("change", () => {
  try { localStorage.setItem(AUTO_CHECK_UPDATES_KEY, String(autoCheckUpdatesToggle.checked)); } catch { /* preference remains session-local if storage is unavailable */ }
  if (!autoCheckUpdatesToggle.checked && updateCheckInFlight && !updateCheckInFlightManual) {
    updateCheckGeneration += 1;
    updateCheckInFlight = null;
    updateCheckInFlightManual = false;
  }
  if (updateStatus === "up-to-date") setApplicationUpdateStatus("up-to-date");
  if (canRunAutomaticUpdateCheck()) void checkForApplicationUpdates();
});

const AUTO_INSTALL_UPDATES_KEY = "rdt-connect-auto-install-updates-on-restart";
try {
  autoInstallUpdatesToggle.checked =
    localStorage.getItem(AUTO_INSTALL_UPDATES_KEY) === "true";
} catch {
  autoInstallUpdatesToggle.checked = false;
}
autoInstallUpdatesToggle.addEventListener("change", () => {
  try {
    localStorage.setItem(
      AUTO_INSTALL_UPDATES_KEY,
      String(autoInstallUpdatesToggle.checked),
    );
  } catch {
    /* Keep the preference session-local if storage is unavailable. */
  }
});

if (canRunAutomaticUpdateCheck()) void checkForApplicationUpdates();
window.setInterval(() => {
  if (canRunAutomaticUpdateCheck()) void checkForApplicationUpdates();
}, 30 * 60 * 1000);
window.addEventListener("online", () => {
  if (canRunAutomaticUpdateCheck()) void checkForApplicationUpdates();
});
void refreshAppBuildInfo();

const legalLinkRoutes: Array<[string, string]> = [
  ["legal-rdt-voice-license-link", "rdt-voice-license"],
  ["legal-commercial-license-link", "commercial-license"],
  ["legal-license-history-link", "license-history"],
  ["legal-third-party-acknowledgements-link", "third-party-acknowledgements"],
];
for (const [id, kind] of legalLinkRoutes) {
  const link = document.getElementById(id) as HTMLAnchorElement | null;
  link?.addEventListener("click", async (event) => {
    event.preventDefault();
    try {
      await invoke("open_about_link", { kind, locale: getActiveLocale() });
    } catch (error) {
      showNotice(`Could not open ${link.textContent?.trim() || "license link"}: ${formatHardwareDeviceText(String(error))}`, "error");
    }
  });
}

async function launchRdtVoice() {
  showNotice(translateKey("msg.checking_the_local_server_and_authentication.0c0d87a2", {}, getActiveLocale()), "info");
  await refreshConnectionStatus();
  if (!browserPreflightPassed()) throw new Error(`RDT Voice was not opened. ${browserPreflightFailure()}`);

  const browserId = browserSelect.value || "default";
  const browserName = browserSelect.selectedOptions[0]?.textContent?.trim() || "System Default Browser";
  showNotice(translateKey("msg.local_server_and_authentication_passed_openi.9683dd3b", { browserName: browserName }, getActiveLocale()), "info");

  const usingOfflineCopy = await invoke<boolean>("open_voice_window", {
    browserId,
    locale: getActiveLocale(),
    offlineOnlyMode: offlineOnlyModeEnabled,
  });

  if (usingOfflineCopy) {
    showNotice("Using offline RDT Voice.", "good");
  } else {
    showNotice(translateKey("msg.rdt_voice_opened_in_browsername_the_local_se.0dba2a99", { browserName: browserName }, getActiveLocale()), "good");
  }
}

if (brandHomeLink) brandHomeLink.addEventListener("click", async (event) => {
  event.preventDefault();
  try { await runAction(launchRdtVoice); }
  catch (error) { showNotice(`Could not open RDT Voice: ${formatHardwareDeviceText(String(error))}`, "error"); }
});
aboutYoutubeButton.addEventListener("click", async () => {
  try {
    await invoke("open_youtube_channel");
  } catch (error) {
    showNotice(translateKey("msg.could_not_open_the_youtube_channel_error.df324c0a", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
  }
});
aboutCompanyLink.addEventListener("click", async (event) => {
  event.preventDefault();
  try { await invoke("open_about_link", { kind: "github", locale: getActiveLocale() }); }
  catch (error) { showNotice(translateKey("msg.could_not_open_the_company_github_page_error.fa492006", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
});
aboutBusinessContactLink.addEventListener("click", async (event) => {
  event.preventDefault();
  try { await invoke("open_about_link", { kind: "email", locale: getActiveLocale() }); }
  catch (error) { showNotice(translateKey("msg.could_not_open_the_business_contact_email_er.7af3d7e8", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error"); }
});
openButton.addEventListener("click", async () => runAction(launchRdtVoice));
setupRemoteButton.addEventListener("click", async () => {
  await refreshBackendStatus();
  if (!backend.running) {
    const message = "Start a server on the Local Server page first, then try again.";
    remoteState.textContent = message;
    remoteState.className = "remote-state warning";
    showNotice(message, "warning");
    return;
  }
  await runAction(async () => {
    setupRemoteButton.textContent = "Setting up remote access…";
    try {
      tailscale = await invoke<TailscaleStatus>("enable_private_access");
      renderRemoteStatus();
      await refreshDeviceLists();
      showNotice(tailscale.message || translateKey("msg.private_access_checked.37fb07fe", {}, getActiveLocale()), tailscale.ready ? "good" : "info");
    } finally {
      renderRemoteStatus();
    }
  });
});
generateQrButton.addEventListener("click", async () => runAction(async () => {
  // Seed the request-ID baseline before generating a fresh single-use QR.
  await loadPairRequests();
  pairingQr = await invoke<PairingQr>("generate_pairing_qr");
  pairingQrConsumed = false;
  qrImage.src = pairingQr.qrDataUrl; qrImage.classList.remove("hidden", "expired"); qrPlaceholder.classList.add("hidden");
  qrExpandedImage.classList.remove("expired");
  copyLinkButton.disabled = !pairingQr.pairingUrl;
  qrStatus.textContent = translateKey("msg.scan_with_the_browser_you_want_to_authorize.2592ea26", {}, getActiveLocale());
  qrStatus.classList.remove("hidden");
  updateQrCountdown(); await refreshDeviceLists(); startRemotePolling();
  showNotice(translateKey("msg.single_use_qr_created_it_expires_in_60_secon.4e18379a", {}, getActiveLocale()), "good");
}));
copyLinkButton.addEventListener("click", async () => {
  if (!pairingQr?.pairingUrl || !pairingQrUsable()) return;
  try { await navigator.clipboard.writeText(pairingQr.pairingUrl); showButtonFeedback(copyLinkButton); }
  catch { showNotice(translateKey("msg.clipboard_access_was_unavailable_scan_the_qr.ee918950", {}, getActiveLocale()), "error"); }
});

function markPairingQrUsed() {
  if (!pairingQr) return;
  pairingQrConsumed = true;
  qrImage.classList.add("expired");
  qrExpandedImage.classList.add("expired");
  qrCountdown.textContent = translateKey("msg.qr_expired_generate_a_new_one_below.f21385bd", {}, getActiveLocale());
  qrExpandedCountdown.textContent = qrCountdown.textContent;
  qrStatus.textContent = "";
  qrStatus.classList.add("hidden");
  copyLinkButton.disabled = true;
  expandQrButton.disabled = true;
}
function updateQrCountdown() {
  if (!pairingQr) { qrCountdown.textContent = ""; qrExpandedCountdown.textContent = ""; return; }
  if (pairingQrConsumed) { markPairingQrUsed(); return; }
  const remaining = Math.max(0, Math.ceil(pairingQr.expiresAt - Date.now() / 1000));
  qrCountdown.textContent = remaining ? translateKey("msg.expires_in_remaining_s.75000b68", { remaining: remaining }, getActiveLocale()) : "QR expired\nGenerate a new one below.";
  qrExpandedCountdown.textContent = qrCountdown.textContent;
  if (remaining === 0) {
    qrImage.classList.add("expired"); qrExpandedImage.classList.add("expired");
    qrStatus.textContent = "";
    qrStatus.classList.add("hidden");
    copyLinkButton.disabled = true;
    expandQrButton.disabled = true;
  } else {
    qrImage.classList.remove("expired"); qrExpandedImage.classList.remove("expired");
    qrStatus.classList.remove("hidden");
    copyLinkButton.disabled = busy || !pairingQr?.pairingUrl;
    expandQrButton.disabled = busy || !pairingQr?.qrDataUrl;
  }
}
function openExpandedQr() {
  if (!pairingQr?.qrDataUrl || !pairingQrUsable()) {
    showNotice(translateKey("msg.generate_a_fresh_qr_after_private_access_is.914d36e5", {}, getActiveLocale()), "warning");
    return;
  }
  qrExpandedImage.src = pairingQr.qrDataUrl;
  qrExpandedCountdown.textContent = qrCountdown.textContent;
  qrExpandedOverlay.classList.remove("hidden");
  closeExpandedQrButton.focus();
}
function closeExpandedQr() {
  qrExpandedOverlay.classList.add("hidden");
  expandQrButton.focus();
}
expandQrButton.addEventListener("click", openExpandedQr);
qrImage.addEventListener("click", openExpandedQr);
qrImage.setAttribute("title", translateKey("msg.click_to_expand_this_qr_code.d2efe14c", {}, getActiveLocale()));
qrImage.setAttribute("role", "button");
qrImage.setAttribute("tabindex", "0");
qrImage.setAttribute("aria-label", translateKey("msg.expand_the_pairing_qr_code.e1ce2439", {}, getActiveLocale()));
qrImage.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openExpandedQr(); }
});
closeExpandedQrButton.addEventListener("click", closeExpandedQr);
qrExpandedOverlay.addEventListener("click", (event) => { if (event.target === qrExpandedOverlay) closeExpandedQr(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !qrExpandedOverlay.classList.contains("hidden")) closeExpandedQr(); });
function startRemotePolling() {
  if (remotePoll !== null) window.clearInterval(remotePoll);
  remotePoll = window.setInterval(async () => {
    updateQrCountdown();
    await loadPairRequests();
    await loadDevices();
    if (pairingQr && Date.now() / 1000 > pairingQr.expiresAt + 185) {
      if (remotePoll !== null) window.clearInterval(remotePoll);
      remotePoll = null;
    }
  }, 1800);
}

keepInMenuBarInput.addEventListener("change", async () => {
  keepInMenuBarInput.disabled = true;
  try {
    await invoke("set_keep_in_menu_bar", { enabled: keepInMenuBarInput.checked });
    showNotice(isWindowsRuntime()
      ? (keepInMenuBarInput.checked
        ? "The RDT Connect notification-center icon is now visible. Closing the window hides it there."
        : "The notification-center icon is hidden. Closing the window will quit RDT Connect and stop its managed server.")
      : (keepInMenuBarInput.checked
        ? "The RDT Connect menu-bar icon is now visible. Closing the window hides it there."
        : "The menu-bar icon is hidden. Closing the window will quit RDT Connect and stop its managed server."), "info");
  } catch (error) {
    showNotice(isWindowsRuntime()
      ? `Could not save the notification-center preference: ${formatHardwareDeviceText(String(error))}`
      : translateKey("msg.could_not_save_the_menu_bar_preference_error.cf3a5e22", { error: formatHardwareDeviceText(String(error)) }, getActiveLocale()), "error");
    keepInMenuBarInput.checked = !keepInMenuBarInput.checked;
  } finally { keepInMenuBarInput.disabled = false; }
});

async function runStartupCheck(
  label: string,
  task: () => Promise<unknown>,
  timeoutMs = 10000
): Promise<void> {
  let timer: number | undefined;
  try {
    const deadline = new Promise<never>((_, reject) => {
      timer = window.setTimeout(
        () => reject(new Error(`Startup check timed out after ${timeoutMs}ms`)),
        timeoutMs
      );
    });
    await Promise.race([Promise.resolve().then(task), deadline]);
  } catch (error) {
    console.warn(`[RDT Connect] Startup check failed (${label}):`, error);
  } finally {
    if (timer !== undefined) window.clearTimeout(timer);
  }
}

void (async () => {
  startButton.disabled = true;
  keepInMenuBarInput.checked = true;

  try {
    await runStartupCheck("menu-bar preference", async () => {
      try {
        keepInMenuBarInput.checked = await invoke<boolean>("get_keep_in_menu_bar");
      } catch {
        keepInMenuBarInput.checked = true;
      }
    });
    await runStartupCheck("orphaned backend status", () => refreshOrphanedBackendStatus());
    await runStartupCheck("backend status", () => refreshBackendStatus());
    await runStartupCheck("local HTTPS status", () => refreshLocalHttpsStatus(false, false, true));
    await runStartupCheck("remote status", () => refreshRemoteStatus());
    await runStartupCheck("inference device preference", () => inferenceDevicePreferenceReady);
    try {
      recoverInterruptedHardwareOptimizationSession();
    } catch (error) {
      console.warn("[RDT Connect] Could not recover interrupted hardware optimization:", error);
    }
  } finally {
    startupReady = true;
    setBusy(false);
    void maybeShowHardwareOptimizationRetryNotice().catch((error) => {
      console.warn("[RDT Connect] Could not show the hardware retry notice:", error);
    });
  void refreshModelStorageStatus();
  void refreshModelDownloadStatus();
  void refreshModelCacheBadges();
    void (async () => {
      await runStartupCheck("model storage status", () => refreshModelStorageStatus());
      await runStartupCheck("model download status", () => refreshModelDownloadStatus());
      void refreshModelCacheBadges().catch((error) => {
      console.warn("[RDT Connect] Model cache badge refresh failed:", error);
      });
    })();
    try {
      renderBackendStatus();
    } catch (error) {
      console.error("[RDT Connect] Initial backend rendering failed:", error);
    }
  }
})();

setInterval(() => {
  if (!busy && document.visibilityState === "visible") {
    void refreshLocalHttpsStatus();
  }
}, 15_000);

/* RDT_LANGUAGE_REFRESH_FOR_DYNAMIC_UI_BEGIN */
window.addEventListener("rdt-connect-language-changed", () => {
  // Re-render stateful labels from stable catalog keys after the full DOM locale pass.
  renderBackendStatus();

  const tokenIsVisible = tokenDisplay.type === "text";
  toggleTokenButton.textContent = translateKey(
    tokenIsVisible ? "msg.hide.ac20a57b" : "msg.show.0df6f1ca",
    {},
    getActiveLocale(),
  );
  toggleTokenButton.setAttribute(
    "aria-label",
    translateKey(
      tokenIsVisible
        ? "msg.hide_the_access_token.9d4c7fa3"
        : "msg.reveal_the_access_token.c51590dd",
      {},
      getActiveLocale(),
    ),
  );

  renderRemoteStatus();
  if (currentLocalHttps) renderLocalHttpsStatus(currentLocalHttps);
  syncDeveloperToggleButton();
  updateAppLanguageStatus();
  updateRdtVoiceHomeLinkLocale();

  // The notice is dynamically written as translated text, so refresh only
  // known status messages; do not rewrite arbitrary diagnostic/error messages.
  const noticeKeys = [
    "msg.developer_tools_enabled.dc8a294c",
    "msg.ready_to_connect.5802ac49",
  ];
  const localeChoices = getLocaleChoices();
  const matchesKey = (text: string, key: string) =>
    localeChoices.some((choice) =>
      translateKey(key, {}, choice.code) === text
    );

  const currentNotice = notice.textContent ?? "";
  const matchedNoticeKey = noticeKeys.find((key) =>
    matchesKey(currentNotice, key)
  );

  if (matchedNoticeKey) {
    notice.textContent = translateKey(matchedNoticeKey, {}, getActiveLocale());
  }

  const detail = document.getElementById("developer-latest-detail");
  if (detail) {
    const currentDetail = detail.textContent ?? "";
    const matchedDetailKey = noticeKeys.find((key) =>
      matchesKey(currentDetail, key)
    );
    if (matchedDetailKey) {
      detail.textContent = translateKey(matchedDetailKey, {}, getActiveLocale());
    }
  }
});
/* RDT_LANGUAGE_REFRESH_FOR_DYNAMIC_UI_END */

/* RDT_LOCAL_TRAY_ICON_TEST_BEGIN — hidden manual toggle from Developer Options */

const panel = document.createElement("div");
  panel.id = "rdt-local-tray-icon-test";
  panel.style.cssText =
    "position:fixed;z-index:999999;right:16px;top:16px;padding:14px;" +
    "background:#101820;color:#f8fafc;border:3px solid #f0c75e;border-radius:12px;" +
    "box-shadow:0 8px 30px rgba(0,0,0,.65);display:none;gap:8px;flex-wrap:wrap;" +
    "max-width:360px;font:600 13px system-ui,sans-serif";
  panel.innerHTML = "<strong style='width:100%;font-size:14px;color:#f0c75e'>LOCAL TRAY ICON TEST — DEV ONLY</strong>";

  const addButton = (label: string, status: UpdateStatus) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.style.cssText = "padding:8px 10px;border:2px solid #64748b;border-radius:7px;background:#f8fafc;color:#111827;font-weight:800;cursor:pointer";
    button.onclick = () => setApplicationUpdateStatus(status);
    panel.appendChild(button);
  };

  addButton("Update available ↓", "available");
  addButton("Restart required ↑", "restart-required");
  addButton("Normal icon", "idle");
  document.body.appendChild(panel);

  const phraseInput = document.querySelector<HTMLInputElement>("#tray-test-phrase");
  const phraseStatus = document.querySelector<HTMLElement>("#tray-test-phrase-status");

  phraseInput?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();

    const phrase = phraseInput.value.trim().toLowerCase();
    if (["easteregg", "firework", "rdt"].includes(phrase)) {
      phraseInput.value = "";
      window.dispatchEvent(new CustomEvent("rdt-connect-fireworks", { detail: phrase }));
      if (phraseStatus) phraseStatus.textContent = "Fireworks launched";
      return;
    }

const previewCountMatch = /^([1-9]|1[0-9]|20) hardware$/.exec(phrase);
if (previewCountMatch) {
  phraseInput.value = "";
  developerHardwarePreviewCount = Number(previewCountMatch[1]);
  renderDeveloperHardwarePreviewPanels();
  if (phraseStatus) {
    phraseStatus.textContent =
      `Showing ${developerHardwarePreviewCount} simulated hardware candidates. Preview only; real hardware and benchmark settings are unchanged.`;
  }
  return;
}

if (phrase === "hardware" || phrase === "native") {
  phraseInput.value = "";
  developerHardwarePreviewCount = null;
  renderDeveloperHardwarePreviewPanels();
  if (phraseStatus) phraseStatus.textContent = "Hardware preview reset to native detected hardware.";
  return;
}

if (phrase === "hardware selector") {
  phraseInput.value = "";
  void invoke<boolean>("is_hardware_optimization_supported").then((supported) => {
    if (!supported) {
      if (phraseStatus) phraseStatus.textContent =
        "Hardware selector simulation is available on Apple Silicon Macs only.";
      return;
    }
    forceManualHardwareSelectorForDev = !forceManualHardwareSelectorForDev;
    if (phraseStatus) phraseStatus.textContent = forceManualHardwareSelectorForDev
      ? "Hardware selector simulation ON. Click Run transcription benchmark to preview the full candidate selector."
      : "Hardware selector simulation OFF. Normal hardware selection behavior restored.";
  }).catch((error) => {
    if (phraseStatus) phraseStatus.textContent =
      `Hardware selector command failed: ${String(error)}`;
  });
  return;
}

if (phrase === "unleash") {
      phraseInput.value = "";
      armQaFirstRunHardwareOptimization();
      if (phraseStatus) phraseStatus.textContent = "First-run QA armed. Click Start Server once to run the short first-run check.";
      return;
    }
    if (phrase !== "tray test") {
      if (phraseStatus) phraseStatus.textContent = "Not recognized";
      return;
    }

    const showing = panel.style.display !== "none";
    panel.style.display = showing ? "none" : "flex";
    phraseInput.value = "";
    if (phraseStatus) phraseStatus.textContent = showing ? "Hidden" : "Shown";
  });

/* RDT_LOCAL_TRAY_ICON_TEST_END */

/* RDT_HARDWARE_OPTIMIZATION_BEGIN
 * One guarded, same-engine MLX comparison. GPU and CPU candidates are launched
 * sequentially, each with a warm-up plus seven measured requests. */
const HARDWARE_OPTIMIZATION_RESOLVED_KEY = "rdt_connect_hardware_optimization_resolved";
const HARDWARE_OPTIMIZATION_PENDING_KEY = "rdt_connect_hardware_optimization_pending";
const HARDWARE_OPTIMIZATION_INTERRUPTION_COUNT_KEY = "rdt_connect_hardware_optimization_interruptions";
const HARDWARE_OPTIMIZATION_QA_FIRST_RUN_KEY = "rdt_connect_hardware_optimization_qa_first_run_once";
const HARDWARE_OPTIMIZATION_RETRY_NOTICE_PENDING_KEY = "rdt_connect_hardware_optimization_retry_notice_pending";
const HARDWARE_OPTIMIZATION_MAX_INTERRUPTED_SESSIONS = 2;

let qaFirstRunHardwareOptimizationArmedInMemory = false;

function armQaFirstRunHardwareOptimization(): void {
  qaFirstRunHardwareOptimizationArmedInMemory = true;
  try {
    localStorage.setItem(HARDWARE_OPTIMIZATION_QA_FIRST_RUN_KEY, "armed");
  } catch {
    // The one-shot test remains armed for this app session.
  }
}

function isQaFirstRunHardwareOptimizationArmed(): boolean {
  if (qaFirstRunHardwareOptimizationArmedInMemory) return true;
  try {
    return localStorage.getItem(HARDWARE_OPTIMIZATION_QA_FIRST_RUN_KEY) === "armed";
  } catch {
    return false;
  }
}

function consumeQaFirstRunHardwareOptimization(): boolean {
  if (!isQaFirstRunHardwareOptimizationArmed()) return false;
  qaFirstRunHardwareOptimizationArmedInMemory = false;
  try {
    localStorage.setItem(HARDWARE_OPTIMIZATION_QA_FIRST_RUN_KEY, "consumed");
  } catch {
    try {
      localStorage.removeItem(HARDWARE_OPTIMIZATION_QA_FIRST_RUN_KEY);
    } catch {
      // In-memory consumption still prevents a second run in this session.
    }
  }
  return true;
}

let hardwareOptimizationPendingInMemory = false;
let hardwareOptimizationInterruptionsInMemory = 0;
let hardwareOptimizationRetryNoticePendingInMemory = false;

function readHardwareOptimizationInterruptionCount(): number {
  try {
    const value = Number.parseInt(localStorage.getItem(HARDWARE_OPTIMIZATION_INTERRUPTION_COUNT_KEY) ?? "0", 10);
    return Number.isFinite(value) && value > 0 ? value : 0;
  } catch {
    return hardwareOptimizationInterruptionsInMemory;
  }
}

function writeHardwareOptimizationInterruptionCount(value: number): void {
  hardwareOptimizationInterruptionsInMemory = value;
  try {
    localStorage.setItem(HARDWARE_OPTIMIZATION_INTERRUPTION_COUNT_KEY, String(value));
  } catch {
    // Retain the count in memory if persistent storage is unavailable.
  }
}

function clearHardwareOptimizationPendingMarker(): void {
  hardwareOptimizationPendingInMemory = false;
  try {
    localStorage.removeItem(HARDWARE_OPTIMIZATION_PENDING_KEY);
  } catch {
    // Retain the in-memory state if persistent storage is unavailable.
  }
}

function setHardwareOptimizationRetryNoticePending(value: boolean): void {
  hardwareOptimizationRetryNoticePendingInMemory = value;
  try {
    if (value) {
      localStorage.setItem(HARDWARE_OPTIMIZATION_RETRY_NOTICE_PENDING_KEY, "true");
    } else {
      localStorage.removeItem(HARDWARE_OPTIMIZATION_RETRY_NOTICE_PENDING_KEY);
    }
  } catch {
    // Keep the one-time notice state in memory if persistent storage is unavailable.
  }
}

function isHardwareOptimizationRetryNoticePending(): boolean {
  try {
    return localStorage.getItem(HARDWARE_OPTIMIZATION_RETRY_NOTICE_PENDING_KEY) === "true"
      || hardwareOptimizationRetryNoticePendingInMemory;
  } catch {
    return hardwareOptimizationRetryNoticePendingInMemory;
  }
}

function beginFirstRunHardwareOptimizationSession(source: "first-run" | "manual" | "preview"): void {
  if (source !== "first-run") return;
  hardwareOptimizationPendingInMemory = true;
  try {
    localStorage.setItem(HARDWARE_OPTIMIZATION_PENDING_KEY, "mandatory-first-run");
  } catch {
    console.warn("[RDT Connect] Mandatory benchmark interruption recovery could not be persisted.");
  }
}

function completeFirstRunHardwareOptimization(): void {
  clearHardwareOptimizationPendingMarker();
  writeHardwareOptimizationInterruptionCount(0);
  setHardwareOptimizationRetryNoticePending(false);
  setFirstRunHardwareOptimizationResolved(true);
}

function recoverInterruptedHardwareOptimizationSession(): void {
  let pending = hardwareOptimizationPendingInMemory;
  try {
    const marker = localStorage.getItem(HARDWARE_OPTIMIZATION_PENDING_KEY);
    pending = marker === "mandatory-first-run";
    if (marker !== null && marker !== "mandatory-first-run") {
      localStorage.removeItem(HARDWARE_OPTIMIZATION_PENDING_KEY);
    }
  } catch {
    // Use the in-memory marker if storage cannot be read.
  }
  if (!pending) return;

  // Clear first so a single interrupted attempt is counted only once.
  clearHardwareOptimizationPendingMarker();
  const interruptions = readHardwareOptimizationInterruptionCount() + 1;
  writeHardwareOptimizationInterruptionCount(interruptions);
  const retryLimitReached = interruptions >= HARDWARE_OPTIMIZATION_MAX_INTERRUPTED_SESSIONS;
  setFirstRunHardwareOptimizationResolved(retryLimitReached);
  if (retryLimitReached) setHardwareOptimizationRetryNoticePending(true);
}

function showHardwareOptimizationRetryNotice(): void {
  if (document.getElementById("hardware-retry-notice-overlay")) return;

  const previousFocus = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  const overlay = document.createElement("div");
  overlay.id = "hardware-retry-notice-overlay";
  overlay.className = "hardware-retry-notice-overlay";

  const panel = document.createElement("section");
  panel.className = "hardware-retry-notice-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "hardware-retry-notice-title");
  panel.setAttribute("aria-describedby", "hardware-retry-notice-description");

  const title = document.createElement("h2");
  title.id = "hardware-retry-notice-title";
  title.textContent = benchmarkMessage("msg.benchmark_skipped_title.1d20");

  const description = document.createElement("p");
  description.id = "hardware-retry-notice-description";
  description.textContent = benchmarkMessage("msg.benchmark_skipped_description.1d20");

  const okButton = document.createElement("button");
  okButton.type = "button";
  okButton.className = "hardware-retry-notice-ok";
  okButton.textContent = benchmarkMessage("msg.benchmark_ok.1d20");

  panel.append(title, description, okButton);
  overlay.append(panel);
  document.body.append(overlay);

  let dismissed = false;
  const dismiss = (): void => {
    if (dismissed) return;
    dismissed = true;
    setHardwareOptimizationRetryNoticePending(false);
    overlay.remove();
    if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
  };

  okButton.addEventListener("click", dismiss);
  panel.addEventListener("keydown", (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      dismiss();
    } else if (event.key === "Tab") {
      event.preventDefault();
      okButton.focus();
    }
  });
  okButton.focus({ preventScroll: true });
}

async function maybeShowHardwareOptimizationRetryNotice(): Promise<void> {
  // After two interrupted attempts, normal app use continues. Always explain
  // the skipped comparison; do not gate this notice on benchmark eligibility.
  if (!isHardwareOptimizationRetryNoticePending()) return;
  showHardwareOptimizationRetryNotice();
}

let hardwareOptimizationResolvedInMemory = false;
let benchmarkRunInProgress = false;
let benchmarkFlowReserved = false;
type HardwareOptimizationChoice = "optimized" | "manual" | "default";
let resolveHardwareOptimizationChoice: ((choice: HardwareOptimizationChoice) => void) | null = null;
let hardwareOptimizationSource: "first-run" | "manual" | "preview" | "results" | null = null;
let latestHardwareOptimizationResult: Record<string, any> | null = null;
let keepWinnerBackendRunning = false;

function isFirstRunHardwareOptimizationResolved(): boolean {
  try {
    return localStorage.getItem(HARDWARE_OPTIMIZATION_RESOLVED_KEY) === "true";
  } catch {
    return hardwareOptimizationResolvedInMemory;
  }
}

function setFirstRunHardwareOptimizationResolved(value: boolean): void {
  hardwareOptimizationResolvedInMemory = value;
  try {
    localStorage.setItem(HARDWARE_OPTIMIZATION_RESOLVED_KEY, String(value));
  } catch {
    // Keep the one-time gate stable for this app session if storage is unavailable.
  }
}

const benchmarkInviteOverlay = document.createElement("div");
benchmarkInviteOverlay.id = "benchmark-invite-overlay";
benchmarkInviteOverlay.className = "benchmark-invite-overlay hidden";
benchmarkInviteOverlay.setAttribute("role", "dialog");
benchmarkInviteOverlay.setAttribute("aria-modal", "true");
benchmarkInviteOverlay.setAttribute("aria-labelledby", "benchmark-invite-title");
benchmarkInviteOverlay.innerHTML = `
  <section class="benchmark-invite-panel hardware-optimization-panel">
    <p class="eyebrow">${benchmarkMessage("msg.benchmark_local_benchmark.1d22")}</p>
    <h2 id="benchmark-invite-title">${benchmarkMessage("msg.benchmark_title.1d22")}</h2>
    <p class="benchmark-invite-intro"><span>${benchmarkMessage("msg.benchmark_intro_finding.1d22")}</span><span>${benchmarkMessage("msg.benchmark_intro_detail.1d22")}</span></p>
    <section id="benchmark-hardware-selector" class="benchmark-hardware-selector hidden" aria-labelledby="benchmark-hardware-selector-title">
      <h3 id="benchmark-hardware-selector-title">${benchmarkMessage("msg.benchmark_selector_title.1d22")}</h3>
      <p class="benchmark-hardware-description"><span>${benchmarkMessage("msg.benchmark_selector_detail.1d22")}</span><span>${benchmarkMessage("msg.benchmark_preference_unchanged.1d22")}</span></p>
      <label class="benchmark-device-choice">
        <input type="checkbox" data-benchmark-device="gpu" checked>
        <span><strong>${benchmarkMessage("msg.benchmark_apple_gpu.1d22")}</strong><small>${benchmarkMessage("msg.benchmark_mlx_metal_gpu.1d22")} · mlx-metal-gpu</small></span>
      </label>
      <label class="benchmark-device-choice">
        <input type="checkbox" data-benchmark-device="cpu" checked>
        <span><strong>${benchmarkMessage("msg.benchmark_apple_cpu.1d22")}</strong><small>${benchmarkMessage("msg.benchmark_mlx_cpu.1d22")} · mlx-cpu-selected</small></span>
      </label>
      <label class="benchmark-selection-order" for="benchmark-hardware-order">
        <span>${benchmarkMessage("msg.benchmark_test_order.1d22")}</span>
        <select id="benchmark-hardware-order">
          <option value="recommended">${benchmarkMessage("msg.benchmark_recommended_first.1d22")}</option>
          <option value="gpu">${benchmarkMessage("msg.benchmark_gpu_first.1d22")}</option>
          <option value="cpu">${benchmarkMessage("msg.benchmark_cpu_first.1d22")}</option>
        </select>
      </label>
      <p id="benchmark-selection-warning" class="benchmark-selection-warning hidden" role="status" aria-live="polite">${benchmarkMessage("msg.benchmark_many_options_warning.1d22")}</p>
      <p id="benchmark-selection-error" class="benchmark-selection-error hidden" role="alert">${benchmarkMessage("msg.benchmark_minimum_selection.1d22")}</p>
      <label class="benchmark-timing-limit">
        <input id="benchmark-limit-per-hardware" type="checkbox" checked>
        <span>${benchmarkMessage("msg.benchmark_timing_limit.1d22")}</span>
      </label>
      <div class="benchmark-selection-actions">
        <button id="benchmark-hardware-run" class="button primary benchmark-run-gold" type="button">${benchmarkMessage("msg.benchmark_run_button.1d22")}</button>
        <button id="benchmark-hardware-cancel" class="button secondary" type="button">${benchmarkMessage("msg.benchmark_cancel_button.1d22")}</button>
      </div>
    </section>
    <section id="benchmark-digital-progress" class="benchmark-digital-progress" aria-label="${benchmarkMessage("msg.benchmark_progress_aria.1d22")}">
      <div class="benchmark-progress-heading"><span id="benchmark-progress-candidate-label">${benchmarkMessage("msg.benchmark_initial_candidate.1d22")}</span><strong id="benchmark-progress-overall">0%</strong><span id="benchmark-progress-remaining">${benchmarkMessage("msg.benchmark_initial_remaining.1d22")}</span></div>
      <div id="benchmark-progress-candidate-ticks" class="benchmark-digital-ticks benchmark-candidate-ticks" aria-hidden="true"></div>
      <div class="benchmark-progress-subheading"><span id="benchmark-progress-step-label">${benchmarkMessage("msg.benchmark_initial_step.1d22")}</span><strong id="benchmark-progress-step-count">0 of 8 · 0%</strong></div>
      <div id="benchmark-progress-step-ticks" class="benchmark-digital-ticks benchmark-step-ticks" aria-hidden="true"></div>
      <div id="benchmark-progress-accessible" class="sr-only" role="progressbar" aria-label="${benchmarkMessage("msg.benchmark_progress_aria.1d22")}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">0%</div>
    </section>
    <p id="benchmark-invite-status" class="connection-status pending benchmark-invite-status" role="status" aria-live="polite">Getting things ready…</p>
    <div class="benchmark-invite-actions">
      <button id="benchmark-invite-apply" class="button primary hardware-optimization-apply hardware-optimization-outcome" type="button" hidden>${benchmarkMessage("msg.benchmark_button_optimize.1d20")}</button>
        <button id="benchmark-invite-close" class="button secondary hardware-optimization-outcome" type="button" hidden>${benchmarkMessage("msg.benchmark_button_default.1d20")}</button>
        <div class="benchmark-invite-secondary-actions">
          <button id="benchmark-invite-custom" class="button secondary" type="button" hidden>${benchmarkMessage("msg.benchmark_button_custom.1d20")}</button>
          <button id="benchmark-invite-export" class="button secondary compact-button" type="button" hidden>${benchmarkMessage("msg.benchmark_button_export.1d20")}</button>
        </div>
      </div>
    <div id="benchmark-invite-result" class="benchmark-invite-result hidden" aria-label="${benchmarkMessage("msg.benchmark_results_aria.1d20")}">
      <div class="benchmark-result-winner"><span>${benchmarkMessage("msg.benchmark_results_recommended_setting.1d20")}</span><strong id="benchmark-result-winner">—</strong></div>
      <p id="benchmark-result-guidance" class="benchmark-result-guidance"></p>
      <div id="benchmark-result-candidates" class="benchmark-result-candidates" aria-live="polite"></div>
      <details class="benchmark-result-details"><summary>${benchmarkMessage("msg.benchmark_results_details.1d20")}</summary><pre id="benchmark-invite-technical" aria-label="${benchmarkMessage("msg.benchmark_results_data_aria.1d20")}"></pre></details>
      <p id="benchmark-result-final-status" class="benchmark-result-final-status" role="status" aria-live="polite"></p>
    </div>
    <details id="benchmark-invite-error-details" class="benchmark-error-details hidden">
      <summary>${benchmarkMessage("msg.benchmark_error_details.1d20")}</summary>
      <pre id="benchmark-invite-error-text" aria-label="${benchmarkMessage("msg.benchmark_error_data_aria.1d20")}"></pre>
    </details>

  </section>
`;
document.body.appendChild(benchmarkInviteOverlay);

function syncBenchmarkOverlayScrollLock(): void {
  const open = !benchmarkInviteOverlay.classList.contains("hidden");
  document.documentElement.classList.toggle("benchmark-invite-open", open);
  document.body.classList.toggle("benchmark-invite-open", open);
}
const benchmarkOverlayVisibilityObserver = new MutationObserver(syncBenchmarkOverlayScrollLock);
benchmarkOverlayVisibilityObserver.observe(benchmarkInviteOverlay, {
  attributes: true,
  attributeFilter: ["class"],
});
syncBenchmarkOverlayScrollLock();

const benchmarkInvitePanel = benchmarkInviteOverlay.querySelector<HTMLElement>(".hardware-optimization-panel")!;
const benchmarkInviteIntroFirstLine = benchmarkInviteOverlay.querySelector<HTMLElement>(".benchmark-invite-intro > span:first-child")!;
benchmarkInvitePanel.tabIndex = -1;
const benchmarkInviteStatus = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-invite-status")!;
const benchmarkInviteResult = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-invite-result")!;
const benchmarkResultFinalStatus = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-result-final-status")!;
const benchmarkInviteTechnicalResult = benchmarkInviteOverlay.querySelector<HTMLPreElement>("#benchmark-invite-technical")!;
const benchmarkInviteErrorDetails = benchmarkInviteOverlay.querySelector<HTMLDetailsElement>("#benchmark-invite-error-details")!;
const benchmarkInviteErrorText = benchmarkInviteOverlay.querySelector<HTMLPreElement>("#benchmark-invite-error-text")!;
const benchmarkInviteApplyButton = benchmarkInviteOverlay.querySelector<HTMLButtonElement>("#benchmark-invite-apply")!;
const benchmarkInviteCustomButton = benchmarkInviteOverlay.querySelector<HTMLButtonElement>("#benchmark-invite-custom")!;
const benchmarkInviteExportButton = benchmarkInviteOverlay.querySelector<HTMLButtonElement>("#benchmark-invite-export")!;
const benchmarkResultCandidates = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-result-candidates")!;
const benchmarkInviteCloseButton = benchmarkInviteOverlay.querySelector<HTMLButtonElement>("#benchmark-invite-close")!;
const benchmarkProgressPanel = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-digital-progress")!;
const benchmarkHardwareSelector = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-hardware-selector")!;
const manualHardwareDeviceInputs = Array.from(benchmarkHardwareSelector.querySelectorAll<HTMLInputElement>("input[data-benchmark-device]"));
const manualHardwareOrderSelect = benchmarkHardwareSelector.querySelector<HTMLSelectElement>("#benchmark-hardware-order")!;
const manualHardwareRunButton = benchmarkHardwareSelector.querySelector<HTMLButtonElement>("#benchmark-hardware-run")!;
const manualHardwareCancelButton = benchmarkHardwareSelector.querySelector<HTMLButtonElement>("#benchmark-hardware-cancel")!;
const manualHardwareSelectionWarning = benchmarkHardwareSelector.querySelector<HTMLElement>("#benchmark-selection-warning")!;
const manualHardwareSelectionError = benchmarkHardwareSelector.querySelector<HTMLElement>("#benchmark-selection-error")!;
const manualHardwareTimingLimitCheckbox = benchmarkHardwareSelector.querySelector<HTMLInputElement>("#benchmark-limit-per-hardware")!;

type ManualHardwareDevice = "gpu" | "cpu";
type ManualHardwareOrder = "recommended" | ManualHardwareDevice;
let pendingManualHardwareSelection: { devices: ManualHardwareDevice[]; order: ManualHardwareOrder } | null = null;
let forceManualHardwareSelectorForDev = false;
let manualHardwareSelectionAttempted = false;


/* RDT_HARDWARE_PREVIEW_PANELS_1D40_BEGIN */
type DeveloperHardwarePreviewPanel = {
  root: HTMLElement;
  title: HTMLElement;
  note: HTMLElement;
  list: HTMLElement;
};

let developerHardwarePreviewCount: number | null = null;

function createDeveloperHardwarePreviewPanel(label: string): DeveloperHardwarePreviewPanel {
  const root = document.createElement("section");
  root.className = "rdt-hardware-preview-panel";
  root.hidden = true;
  root.setAttribute("aria-label", `${label} developer preview`);

  const heading = document.createElement("div");
  heading.className = "rdt-hardware-preview-heading";

  const title = document.createElement("strong");
  const note = document.createElement("small");
  heading.append(title, note);

  const list = document.createElement("div");
  list.className = "hardware-selector-preview-list rdt-hardware-preview-list";
  root.append(heading, list);

  return { root, title, note, list };
}

const inferenceHardwarePreviewPanel =
  createDeveloperHardwarePreviewPanel("Inference hardware");
inferenceDeviceStatus.insertAdjacentElement(
  "afterend",
  inferenceHardwarePreviewPanel.root,
);

const benchmarkHardwarePreviewPanel =
  createDeveloperHardwarePreviewPanel("Manual benchmark hardware selection");
benchmarkHardwareSelector.appendChild(benchmarkHardwarePreviewPanel.root);

function renderDeveloperHardwarePreviewPanels(): void {
  const count = developerHardwarePreviewCount;
  const panels = [inferenceHardwarePreviewPanel, benchmarkHardwarePreviewPanel];

  for (const panel of panels) {
    panel.root.hidden = count === null;
    panel.list.replaceChildren();
    if (count === null) continue;

    panel.title.textContent = `Developer preview · ${count} simulated hardware candidates`;
    panel.note.textContent =
      "Visual preview only. Disabled rows cannot be selected, benchmarked, or used for transcription.";

    const profiles: Array<[string, string]> = [
      ["Discrete GPU 1 · Provider A", "Simulated accelerator candidate"],
      ["Discrete GPU 1 · Provider B", "Same adapter; separate provider candidate"],
      ["Discrete GPU 2 · Provider A", "Second simulated physical adapter"],
      ["Integrated GPU · Provider B", "Simulated integrated accelerator"],
      ["ARM CPU · dot-product", "Simulated optimized CPU candidate"],
      ["Portable CPU", "Simulated fallback CPU candidate"],
    ];

    for (let index = 0; index < count; index += 1) {
      const profile = profiles[index] ??
        [`Additional hardware ${index + 1}`, "Additional simulated candidate"];

      const row = document.createElement("div");
      row.className = "hardware-selector-preview-row rdt-hardware-preview-row";
      row.setAttribute("aria-disabled", "true");

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.disabled = true;
      checkbox.tabIndex = -1;
      checkbox.className = "rdt-hardware-preview-checkbox";
      checkbox.setAttribute("aria-label", `${profile[0]}, preview only`);

      const copy = document.createElement("div");
      copy.className = "hardware-selector-preview-device-copy";

      const name = document.createElement("strong");
      name.textContent = profile[0];

      const detail = document.createElement("small");
      detail.textContent = profile[1];

      copy.append(name, detail);

      const badge = document.createElement("span");
      badge.className = "hardware-selector-preview-badge";
      badge.textContent = "PREVIEW";

      row.append(checkbox, copy, badge);
      panel.list.appendChild(row);
    }
  }
}
/* RDT_HARDWARE_PREVIEW_PANELS_1D40_END */



function selectedManualHardwareDevices(): ManualHardwareDevice[] {
  return manualHardwareDeviceInputs
    .filter((input) => input.checked)
    .map((input) => input.dataset.benchmarkDevice)
    .filter((device): device is ManualHardwareDevice => device === "gpu" || device === "cpu");
}

function updateManualHardwareSelector(): void {
  const count = selectedManualHardwareDevices().length;
  manualHardwareRunButton.disabled = count < 2;
  manualHardwareSelectionWarning.classList.toggle("hidden", count <= 5);
  manualHardwareSelectionError.classList.toggle("hidden", count >= 2 || !manualHardwareSelectionAttempted);
}

function openManualHardwareSelector(): void {
  renderDeveloperHardwarePreviewPanels();
  if (!developerOptionsEnabled) return;
  pendingManualHardwareSelection = null;
  manualHardwareSelectionAttempted = false;
  manualHardwareDeviceInputs.forEach((input) => { input.checked = true; });
  manualHardwareOrderSelect.value = "recommended";
  updateManualHardwareSelector();

  // Two supported devices are always compared; a selector is only useful for 3+.
  if (manualHardwareDeviceInputs.length <= 2 && !forceManualHardwareSelectorForDev) {
    const devices = selectedManualHardwareDevices();
    if (devices.length < 2) {
      showNotice(benchmarkMessage("msg.benchmark_minimum_devices.1d20"), "error");
      return;
    }
    pendingManualHardwareSelection = { devices, order: "recommended" };
    startSelectedManualHardwareBenchmark();
    return;
  }

  benchmarkProgressPanel.classList.add("hidden");
  benchmarkInviteStatus.classList.add("hidden");
  benchmarkInviteResult.classList.add("hidden");
  benchmarkInviteErrorDetails.classList.add("hidden");
  benchmarkHardwareSelector.classList.remove("hidden");
  benchmarkInvitePanel.classList.add("is-selecting-hardware");
  benchmarkInviteOverlay.classList.remove("hidden");
  benchmarkInvitePanel.focus({ preventScroll: true });
  manualHardwareRunButton.focus({ preventScroll: true });
}

function startSelectedManualHardwareBenchmark(): void {
  manualHardwareSelectionAttempted = true;
  updateManualHardwareSelector();
  const devices = selectedManualHardwareDevices();
  if (devices.length < 2) return;

  pendingManualHardwareSelection = {
    devices,
    order: manualHardwareOrderSelect.value as ManualHardwareOrder,
  };
  benchmarkHardwareSelector.classList.add("hidden");
  benchmarkInvitePanel.classList.remove("is-selecting-hardware");
  benchmarkProgressPanel.classList.remove("hidden");
  benchmarkInviteStatus.classList.remove("hidden");
  void runHardwareOptimizationFlow("manual").catch((error) => {
    showNotice(benchmarkMessage("msg.benchmark_shutdown_failed.1d20", { detail: formatHardwareDeviceText(String(error)) }), "error");
  });
}

manualHardwareDeviceInputs.forEach((input) => {
  input.addEventListener("change", updateManualHardwareSelector);
});
manualHardwareRunButton.addEventListener("click", startSelectedManualHardwareBenchmark);
manualHardwareCancelButton.addEventListener("click", () => {
  pendingManualHardwareSelection = null;
  benchmarkHardwareSelector.classList.add("hidden");
  benchmarkInvitePanel.classList.remove("is-selecting-hardware");
  benchmarkInviteOverlay.classList.add("hidden");
  openNativeBenchmarkButton.focus();
});
updateManualHardwareSelector();

async function transitionBenchmarkProgressToResults(): Promise<void> {
  if (benchmarkProgressPanel.classList.contains("hidden")) {
    benchmarkInviteResult.classList.remove("benchmark-result-awaiting-finalization");
    return;
  }
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    benchmarkProgressPanel.classList.add("hidden");
    benchmarkInviteResult.classList.remove("benchmark-result-awaiting-finalization");
    return;
  }

  const fade = benchmarkProgressPanel.animate(
    [
      { opacity: 1, transform: "translateY(0)" },
      { opacity: 0, transform: "translateY(-7px)" },
    ],
    { duration: 430, easing: "ease-in-out" },
  );
  await fade.finished.catch(() => undefined);

  const height = Math.ceil(benchmarkProgressPanel.getBoundingClientRect().height);
  benchmarkProgressPanel.classList.add("is-collapsing");
  const collapse = benchmarkProgressPanel.animate(
    [
      { height: `${height}px`, marginTop: "14px", marginBottom: "15px", paddingTop: "13px", paddingBottom: "12px" },
      { height: "0px", marginTop: "0px", marginBottom: "0px", paddingTop: "0px", paddingBottom: "0px" },
    ],
    { duration: 560, easing: "cubic-bezier(.2,.7,.2,1)", fill: "forwards" },
  );

  await collapse.finished.catch(() => undefined);
  benchmarkProgressPanel.classList.add("hidden");
  benchmarkProgressPanel.classList.remove("is-collapsing");
  collapse.cancel();

  if (!benchmarkInviteResult.classList.contains("hidden")) {
    benchmarkInviteResult.classList.remove("benchmark-result-awaiting-finalization");
    const reveal = benchmarkInviteResult.animate(
      [
        { opacity: 0, transform: "translateY(13px)" },
        { opacity: 1, transform: "translateY(0)" },
      ],
      { duration: 480, delay: 80, easing: "cubic-bezier(.2,.7,.2,1)", fill: "forwards" },
    );
    await reveal.finished.catch(() => undefined);
    reveal.cancel();
  } else {
    benchmarkInviteResult.classList.remove("benchmark-result-awaiting-finalization");
  }
}
const seeBenchmarkResultsButton = document.getElementById("see-benchmark-results-button") as HTMLButtonElement | null;
const benchmarkProgressCandidateLabel = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-progress-candidate-label")!;
const benchmarkProgressOverall = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-progress-overall")!;
const benchmarkProgressRemaining = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-progress-remaining")!;
let benchmarkCountdownTimer: number | null = null;
function stopBenchmarkCountdown(): void {
  if (benchmarkCountdownTimer !== null) window.clearInterval(benchmarkCountdownTimer);
  benchmarkCountdownTimer = null;
}
const benchmarkProgressCandidateTicks = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-progress-candidate-ticks")!;
const benchmarkProgressStepLabel = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-progress-step-label")!;
const benchmarkProgressStepCount = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-progress-step-count")!;
const benchmarkProgressStepTicks = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-progress-step-ticks")!;
const benchmarkProgressAccessible = benchmarkInviteOverlay.querySelector<HTMLElement>("#benchmark-progress-accessible")!;


/* RDT_NATIVE_BENCHMARK_SEVEN_STAGE_UI_START */
type NativeBenchmarkStageInfo = {
  candidateIndex?: number;
  candidateCount?: number;
  candidateLabel?: string;
  measuredCompleted?: number;
  measuredTotal?: number;
  remainingStepAllowanceSeconds?: number;
  phase?: string;
};

function benchmarkMessage(key: string, params: Record<string, string | number> = {}): string {
  return translateKey(key, params, getActiveLocale());
}

const NATIVE_BENCHMARK_STAGE_DEFINITIONS = [
  { key: "prepare", labelKey: "msg.benchmark_stage_getting_ready.1d20" },
  { key: "backend", labelKey: "msg.benchmark_stage_starting_local_service.1d20" },
  { key: "provider", labelKey: "msg.benchmark_stage_checking_hardware.1d20" },
  { key: "runtime", labelKey: "msg.benchmark_stage_loading_model.1d20" },
  { key: "warmup", labelKey: "msg.benchmark_stage_warming_up.1d20" },
  { key: "measure", labelKey: "msg.benchmark_stage_measuring_transcription.1d20" },
  { key: "cleanup", labelKey: "msg.benchmark_stage_finishing_up.1d20" },
] as const;

const NATIVE_BENCHMARK_USER_STAGE_LABELS = [
  "msg.benchmark_stage_getting_ready.1d20",
  "msg.benchmark_stage_starting_local_service.1d20",
  "msg.benchmark_stage_checking_hardware.1d20",
  "msg.benchmark_stage_loading_model.1d20",
  "msg.benchmark_stage_warming_up.1d20",
  "msg.benchmark_stage_measuring_transcription.1d20",
  "msg.benchmark_stage_finishing_up.1d20",
] as const;
const NATIVE_BENCHMARK_USER_STAGE_NOTES = [
  "msg.benchmark_stage_note_preparing_comparison.1d20",
  "msg.benchmark_stage_note_starting_local_service.1d20",
  "msg.benchmark_stage_note_checking_hardware.1d20",
  "msg.benchmark_stage_note_loading_model.1d20",
  "msg.benchmark_stage_note_warming_up.1d20",
  "msg.benchmark_stage_note_measuring_transcription.1d20",
  "msg.benchmark_stage_note_finishing_up.1d20",
] as const;

let nativeBenchmarkStageUiState = {
  stage: -1,
  note: "Preparing the hardware comparison…",
  candidateIndex: 0,
  candidateCount: 0,
  candidateLabel: "",
  measuredCompleted: 0,
  measuredTotal: 7,
  remainingStepAllowanceSeconds: null as number | null,
  failed: false,
};

function ensureNativeBenchmarkStageUi(): HTMLElement | null {
  const root = document.getElementById("benchmark-digital-progress");
  if (!root) return null;

  const existing = root.querySelector<HTMLElement>("#benchmark-seven-stage-ui");
  if (existing) return existing;

  const panel = document.createElement("section");
  panel.id = "benchmark-seven-stage-ui";
  panel.className = "benchmark-seven-stage-ui";
  panel.setAttribute("aria-label", benchmarkMessage("msg.benchmark_progress_heading.1d20"));

  const header = document.createElement("div");
  header.className = "benchmark-seven-stage-header";

  const heading = document.createElement("span");
  heading.className = "benchmark-seven-stage-eyebrow";
  heading.textContent = benchmarkMessage("msg.benchmark_progress_heading.1d20");
  heading.hidden = true;

  const candidate = document.createElement("span");
  candidate.id = "benchmark-seven-stage-candidate";
  candidate.className = "benchmark-seven-stage-candidate";
  candidate.textContent = benchmarkMessage("msg.benchmark_candidate_preparing.1d20");

  header.append(heading, candidate);

  const stageList = document.createElement("div");
  stageList.id = "benchmark-seven-stage-list";
  stageList.className = "benchmark-seven-stage-list";
  stageList.setAttribute("role", "list");
  stageList.hidden = true;
  stageList.setAttribute("aria-hidden", "true");

  for (const [index, definition] of NATIVE_BENCHMARK_STAGE_DEFINITIONS.entries()) {
    const item = document.createElement("div");
    item.className = "benchmark-seven-stage-item";
    item.dataset.stageIndex = String(index);
    item.dataset.state = "pending";
    item.setAttribute("role", "listitem");

    const marker = document.createElement("span");
    marker.className = "benchmark-seven-stage-marker";
    marker.textContent = String(index + 1);
    marker.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.className = "benchmark-seven-stage-label";
    label.textContent = benchmarkMessage(definition.labelKey);
    label.dataset.benchmarkStageLabelKey = definition.labelKey;

    item.append(marker, label);
    stageList.appendChild(item);
  }

  const footer = document.createElement("div");
  footer.className = "benchmark-seven-stage-footer";

  const activity = document.createElement("div");
  activity.className = "benchmark-seven-stage-activity";

  const activeTitle = document.createElement("strong");
  activeTitle.id = "benchmark-seven-stage-title";
  activeTitle.textContent = benchmarkMessage(NATIVE_BENCHMARK_USER_STAGE_LABELS[0]);

  const activeNote = document.createElement("span");
  activeNote.id = "benchmark-seven-stage-note";
  activeNote.textContent = benchmarkMessage(NATIVE_BENCHMARK_USER_STAGE_NOTES[0]);

  activity.append(activeTitle, activeNote);

  const metrics = document.createElement("div");
  metrics.className = "benchmark-seven-stage-metrics";

  const runs = document.createElement("span");
  runs.id = "benchmark-seven-stage-runs";
  runs.textContent = "Warmup: pending · Measurements: 0 / 7";
  runs.hidden = true;

  const allowance = document.createElement("span");
  allowance.id = "benchmark-seven-stage-allowance";
  allowance.className = "benchmark-seven-stage-allowance";
  allowance.hidden = true;

  metrics.append(runs, allowance);
  footer.append(activity, metrics);
  panel.append(header, stageList, footer);
  root.appendChild(panel);
  return panel;
}

function formatNativeBenchmarkStepAllowance(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "Finishing…";
  if (seconds > 25) return "~30s";
  if (seconds > 15) return "~20s";
  if (seconds > 5) return "~10s";
  return "~5s";
}

function updateNativeBenchmarkStage(
  stageIndex: number,
  note: string,
  info: NativeBenchmarkStageInfo = {},
  failed = false,
): void {
  const panel = ensureNativeBenchmarkStageUi();
  if (!panel) return;

  const stage = Math.max(0, Math.min(6, Math.trunc(stageIndex)));
  nativeBenchmarkStageUiState = {
    ...nativeBenchmarkStageUiState,
    stage,
    note,
    failed,
    candidateIndex: info.candidateIndex ?? nativeBenchmarkStageUiState.candidateIndex,
    candidateCount: info.candidateCount ?? nativeBenchmarkStageUiState.candidateCount,
    candidateLabel: info.candidateLabel ?? nativeBenchmarkStageUiState.candidateLabel,
    measuredCompleted: info.measuredCompleted ?? nativeBenchmarkStageUiState.measuredCompleted,
    measuredTotal: info.measuredTotal ?? 7,
    remainingStepAllowanceSeconds:
      typeof info.remainingStepAllowanceSeconds === "number"
        && Number.isFinite(info.remainingStepAllowanceSeconds)
        && info.remainingStepAllowanceSeconds > 0
        ? info.remainingStepAllowanceSeconds
        : null,
  };

  const state = nativeBenchmarkStageUiState;
  const items = panel.querySelectorAll<HTMLElement>("[data-stage-index]");
  items.forEach((item) => {
    const index = Number(item.dataset.stageIndex);
    item.dataset.state = failed && index === stage
      ? "failed"
      : index < stage ? "complete"
      : index === stage ? "active"
      : "pending";
    if (index === stage && !failed) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });

  const title = panel.querySelector<HTMLElement>("#benchmark-seven-stage-title");
  const noteNode = panel.querySelector<HTMLElement>("#benchmark-seven-stage-note");
  const candidateNode = panel.querySelector<HTMLElement>("#benchmark-seven-stage-candidate");
  const runsNode = panel.querySelector<HTMLElement>("#benchmark-seven-stage-runs");
  const allowanceNode = panel.querySelector<HTMLElement>("#benchmark-seven-stage-allowance");

  if (title) title.textContent = failed
    ? benchmarkMessage("msg.benchmark_stage_failed_title.1d20")
    : benchmarkMessage(NATIVE_BENCHMARK_USER_STAGE_LABELS[stage]);
  if (noteNode) noteNode.textContent = failed
    ? benchmarkMessage("msg.benchmark_stage_failed_note.1d20")
    : benchmarkMessage(NATIVE_BENCHMARK_USER_STAGE_NOTES[stage]);
  if (candidateNode) {
    candidateNode.textContent = state.candidateIndex > 0
      ? benchmarkMessage("msg.benchmark_candidate_position.1d23", {
          index: state.candidateIndex,
          count: state.candidateCount || 2,
          device: state.candidateLabel || benchmarkMessage("msg.benchmark_hardware_generic.1d23"),
        })
      : benchmarkMessage("msg.benchmark_candidate_preparing.1d23");
  }
  if (runsNode) {
    const warmup = stage >= 5
      ? benchmarkMessage("msg.benchmark_warmup_complete.1d23")
      : stage === 4
        ? benchmarkMessage("msg.benchmark_warmup_active.1d23")
        : benchmarkMessage("msg.benchmark_warmup_pending.1d23");
    runsNode.textContent = benchmarkMessage("msg.benchmark_runs_status.1d23", {
      warmup, completed: state.measuredCompleted, total: state.measuredTotal,
    });
  }

  if (allowanceNode) {
    const remaining = state.remainingStepAllowanceSeconds;
    // This is a stage allowance, not a verified completion-time estimate.
    // Keep it out of the user-facing UI until comparable timing history exists.
    allowanceNode.hidden = true;
    if (remaining !== null) {
      allowanceNode.textContent =
        `Step allowance ${formatNativeBenchmarkStepAllowance(remaining)}`;
    }
  }
}

window.addEventListener("rdt-connect-language-changed", () => {
  const panel = document.getElementById("benchmark-seven-stage-ui");
  if (!panel) return;

  panel.setAttribute("aria-label", benchmarkMessage("msg.benchmark_progress_heading.1d20"));

  const heading = panel.querySelector<HTMLElement>(".benchmark-seven-stage-eyebrow");
  if (heading) heading.textContent = benchmarkMessage("msg.benchmark_progress_heading.1d20");

  panel.querySelectorAll<HTMLElement>("[data-benchmark-stage-label-key]").forEach((node) => {
    const key = node.dataset.benchmarkStageLabelKey;
    if (key) node.textContent = benchmarkMessage(key);
  });

  const state = nativeBenchmarkStageUiState;
  const stage = Math.max(0, Math.min(6, Math.trunc(state.stage)));
  const title = panel.querySelector<HTMLElement>("#benchmark-seven-stage-title");
  const note = panel.querySelector<HTMLElement>("#benchmark-seven-stage-note");
  const candidate = panel.querySelector<HTMLElement>("#benchmark-seven-stage-candidate");

  if (title) title.textContent = state.failed
    ? benchmarkMessage("msg.benchmark_stage_failed_title.1d20")
    : benchmarkMessage(NATIVE_BENCHMARK_USER_STAGE_LABELS[stage]);
  if (note) note.textContent = state.failed
    ? benchmarkMessage("msg.benchmark_stage_failed_note.1d20")
    : benchmarkMessage(NATIVE_BENCHMARK_USER_STAGE_NOTES[stage]);
  if (candidate) {
    candidate.textContent = state.candidateIndex > 0
      ? benchmarkMessage("msg.benchmark_candidate_position.1d23", {
          index: state.candidateIndex,
          count: state.candidateCount || 2,
          device: state.candidateLabel || benchmarkMessage("msg.benchmark_hardware_generic.1d23"),
        })
      : benchmarkMessage("msg.benchmark_candidate_preparing.1d23");
  }
});

function updateNativeBenchmarkProgressFromEvent(payload: NativeBenchmarkStageInfo): void {
  const info = {
    candidateIndex: payload.candidateIndex,
    candidateCount: payload.candidateCount,
    candidateLabel: payload.candidateLabel,
    measuredCompleted: payload.measuredCompleted,
    measuredTotal: payload.measuredTotal ?? 7,
    remainingStepAllowanceSeconds: payload.remainingStepAllowanceSeconds,
  };

  if (payload.phase === "warmup") {
    updateNativeBenchmarkStage(4, benchmarkMessage("msg.benchmark_stage_warming_up.1d20"), info);
  } else if (payload.phase === "measuring") {
    updateNativeBenchmarkStage(
      5,
      benchmarkMessage("msg.benchmark_stage_measuring_transcription.1d20"),
      info,
    );
  } else if (payload.phase === "complete") {
    updateNativeBenchmarkStage(6, benchmarkMessage("msg.benchmark_stage_finishing_up.1d20"), info);
  } else if (payload.phase === "failed") {
    updateNativeBenchmarkStage(
      nativeBenchmarkStageUiState.stage,
      benchmarkMessage("msg.benchmark_stage_failed_note.1d20"),
      info,
      true,
    );
  }
}
/* RDT_NATIVE_BENCHMARK_SEVEN_STAGE_UI_END */

function renderDigitalBenchmarkProgress(
  candidateIndex: number,
  candidateCount: number,
  candidateLabel: string,
  completedCandidates: number,
  completedSteps: number,
  totalSteps: number,
): void {
  ensureNativeBenchmarkStageUi();
  if (nativeBenchmarkStageUiState.stage < 0) {
    updateNativeBenchmarkStage(0, benchmarkMessage("msg.benchmark_candidate_preparing.1d20"));
  }

  const safeCount = Math.max(1, Math.min(20, Math.floor(candidateCount) || 1));
  const safeIndex = Math.max(1, Math.min(safeCount, Math.floor(candidateIndex) || 1));
  const safeCompletedCandidates = Math.max(0, Math.min(safeCount, Math.floor(completedCandidates) || 0));
  const safeTotalSteps = Math.max(1, Math.min(32, Math.floor(totalSteps) || 8));
  const safeCompletedSteps = Math.max(0, Math.min(safeTotalSteps, Math.floor(completedSteps) || 0));
  const stepPercent = Math.round((safeCompletedSteps / safeTotalSteps) * 100);
  const overallPercent = Math.max(0, Math.min(100, Math.round(((safeIndex - 1 + (safeCompletedSteps / safeTotalSteps)) / safeCount) * 100)));

  benchmarkProgressCandidateLabel.textContent = `${safeIndex} / ${safeCount} · ${candidateLabel}`;
  benchmarkProgressOverall.textContent = `${overallPercent}%`;
  benchmarkProgressCandidateTicks.replaceChildren();
  for (let index = 0; index < safeCount; index += 1) {
    const tick = document.createElement("span");
    tick.className = "benchmark-digital-tick";
    if (index < safeCompletedCandidates) tick.classList.add("is-complete");
    else if (index === safeCompletedCandidates && safeCompletedCandidates < safeCount) tick.classList.add("is-active");
    benchmarkProgressCandidateTicks.appendChild(tick);
  }

  benchmarkProgressStepLabel.textContent = benchmarkMessage("msg.benchmark_stage_measuring_transcription.1d20");
  benchmarkProgressStepCount.textContent = safeTotalSteps === 8
    ? `${safeCompletedSteps} / 8`
    : `${safeCompletedSteps} / ${safeTotalSteps} · ${stepPercent}%`;
  benchmarkProgressStepTicks.replaceChildren();
  for (let index = 0; index < safeTotalSteps; index += 1) {
    const tick = document.createElement("span");
    tick.className = "benchmark-digital-tick";
    if (index < safeCompletedSteps) tick.classList.add("is-complete");
    else if (index === safeCompletedSteps && safeCompletedSteps < safeTotalSteps) tick.classList.add("is-active");
    benchmarkProgressStepTicks.appendChild(tick);
  }
  benchmarkProgressAccessible.setAttribute("aria-valuenow", String(overallPercent));
  benchmarkProgressAccessible.textContent = `${overallPercent}%`;
}

function clearNativeBenchmarkResult(): void {
  const winner = benchmarkInviteResult.querySelector<HTMLElement>("#benchmark-result-winner");
  if (winner) winner.textContent = "—";
  const guidance = benchmarkInviteResult.querySelector<HTMLElement>("#benchmark-result-guidance");
  if (guidance) guidance.textContent = "";
  benchmarkInviteTechnicalResult.textContent = "";
  benchmarkResultCandidates.replaceChildren();
  benchmarkInviteApplyButton.hidden = true;
  benchmarkInviteApplyButton.disabled = true;
  benchmarkInviteApplyButton.textContent = benchmarkMessage("msg.benchmark_button_optimize.1d20");
  benchmarkInviteCustomButton.hidden = true;
  benchmarkInviteCustomButton.disabled = false;
  benchmarkInviteExportButton.hidden = true;
  benchmarkInviteExportButton.disabled = false;
  benchmarkInviteCloseButton.hidden = true;
  benchmarkInviteCloseButton.textContent = benchmarkMessage("msg.benchmark_button_default.1d20");
  benchmarkInviteCloseButton.classList.remove("primary");
  benchmarkInviteCloseButton.classList.add("secondary");
  benchmarkInviteResult.classList.remove("benchmark-result-awaiting-finalization");
  benchmarkInviteResult.classList.remove("is-complete");
  latestHardwareOptimizationResult = null;
  benchmarkInviteErrorText.textContent = "";
  benchmarkInviteErrorDetails.classList.add("hidden");
  benchmarkInviteErrorDetails.open = false;
  const details = benchmarkInviteResult.querySelector<HTMLDetailsElement>(".benchmark-result-details");
  if (details) details.open = false;
}

function showHardwareOptimizationMessage(message: string, tone: "pending" | "good" | "warning" = "pending"): void {
  benchmarkInviteStatus.hidden = false;
  benchmarkInviteStatus.classList.remove("hidden");
  benchmarkInviteStatus.textContent = message;
  benchmarkInviteStatus.className = `connection-status ${tone === "good" ? "connected" : tone === "warning" ? "warning" : "pending"} benchmark-invite-status`;
}

function renderHardwareComparison(value: unknown): boolean {
  const record = (item: unknown): item is Record<string, any> =>
    typeof item === "object" && item !== null && !Array.isArray(item);
  if (!record(value) || value.schema !== "rdt-connect-hardware-optimization-v1"
      || value.optimizationEligible !== true
      || !record(value.candidates)) {
    throw new Error(benchmarkMessage("msg.benchmark_saved_result_missing.1d20"));
  }

  const entries: Array<{
    device: "gpu" | "cpu";
    label: string;
    provider: string;
    ms: number;
    wer: number;
    cer: number;
    strictCer: number;
    rtf: number;
    profile: Record<string, any>;
  }> = [];

  for (const device of ["gpu", "cpu"] as const) {
    const candidate = value.candidates[device];
    if (!record(candidate) || !record(candidate.summary)) continue;

    const provider = typeof candidate.provider === "string" ? candidate.provider : "";
    const providerValid = device === "gpu"
      ? provider === "mlx-metal-gpu"
      : provider === "mlx-cpu-selected";
    const summary = candidate.summary;
    const ms = Number(summary.medianInferenceMs);
    const wer = Number(summary.medianWordErrorRate);
    const cer = Number(summary.medianCharacterErrorRate);
    const strictCer = Number(summary.medianStrictCharacterErrorRate);
    const rtf = Number(summary.medianRealtimeFactor);
    const inferenceSeconds = Number(summary.medianInferenceSeconds);
    const audioDuration = Number(summary.audioDurationSeconds);
    const stable = summary.providerStableAcrossAllRequests === true;
    const runs = Number(summary.measuredRunCount);
    const rawRuns = Array.isArray(candidate.runs) ? candidate.runs : [];

    if (!providerValid || !stable || !Number.isInteger(runs)
        || runs < 1 || runs > 7 || rawRuns.length !== runs
        || !Number.isFinite(ms) || ms <= 0
        || !Number.isFinite(wer) || wer < 0 || wer > 0.10
        || !Number.isFinite(cer) || cer < 0
        || !Number.isFinite(strictCer) || strictCer < 0
        || !Number.isFinite(inferenceSeconds) || inferenceSeconds <= 0
        || !Number.isFinite(audioDuration) || audioDuration <= 0
        || !Number.isFinite(rtf) || rtf <= 0) {
      continue;
    }

    entries.push({
      device,
      label: device === "gpu" ? "Apple GPU" : "Apple CPU",
      provider,
      ms,
      wer,
      cer,
      strictCer,
      rtf,
      profile: candidate,
    });
  }

  if (!entries.length) {
    throw new Error(benchmarkMessage("msg.benchmark_saved_result_missing.1d20"));
  }

  // One candidate is provisional. Two candidates use the transcript-quality allowance.
  entries.sort((a, b) => (a.rtf - b.rtf) || (a.ms - b.ms));
  const winnerDevice = value.winnerDevice === "gpu" || value.winnerDevice === "cpu"
    ? value.winnerDevice : "";
  if (!winnerDevice || !entries.some((entry) => entry.device === winnerDevice)) {
    throw new Error(benchmarkMessage("msg.benchmark_saved_recommendation_invalid.1d20"));
  }

  const winner = entries.find((entry) => entry.device === winnerDevice)!;
  const bestWer = Math.min(...entries.map((entry) => entry.wer));
  let qualityLimit = bestWer;
  if (entries.length === 2) {
    const qualityAllowance = Number(value.qualityAllowanceAbsoluteWer);
    if (!Number.isFinite(qualityAllowance) || qualityAllowance < 0 || qualityAllowance > 0.10) {
      throw new Error(benchmarkMessage("msg.benchmark_saved_quality_allowance_invalid.1d20"));
    }
    qualityLimit = bestWer + qualityAllowance;
    const fastestQualityQualified = [...entries]
      .filter((entry) => entry.wer <= qualityLimit)
      .sort((a, b) => (a.rtf - b.rtf) || (a.ms - b.ms))[0];
    if (!fastestQualityQualified || fastestQualityQualified.device !== winnerDevice) {
      throw new Error(benchmarkMessage("msg.benchmark_saved_recommendation_mismatch.1d20"));
    }
  }
  const comparisonComplete = value.comparisonComplete === true
    && value.providerComparisonEligible === true
    && entries.length === 2
    && entries.every((entry) =>
      entry.profile.candidateComplete === true
      && entry.profile.leaseReleaseConfirmed === true
      && Number(entry.profile.measuredRunCount) === 7
    );
  const winnerHeading = benchmarkInviteResult.querySelector<HTMLElement>(".benchmark-result-winner span");
  if (winnerHeading) winnerHeading.textContent = benchmarkMessage("msg.benchmark_results_recommended_setting.1d20");
  benchmarkInviteResult.querySelector<HTMLElement>("#benchmark-result-winner")!.textContent = winner.label;
  benchmarkInviteResult.querySelector<HTMLElement>("#benchmark-result-guidance")!.textContent = comparisonComplete
    ? benchmarkMessage("msg.benchmark_guidance_complete.1d20", { winner: winner.label })
    : benchmarkMessage("msg.benchmark_guidance_provisional.1d20", { winner: winner.label, runs: Number(winner.profile.measuredRunCount) });
  const detailsSummary = benchmarkInviteResult.querySelector<HTMLElement>(".benchmark-result-details summary");
  if (detailsSummary) detailsSummary.textContent = benchmarkMessage("msg.benchmark_results_details_summary.1d20");

  benchmarkResultCandidates.replaceChildren();
  for (const entry of entries) {
    const card = document.createElement("article");
    card.className = `benchmark-result-candidate${entry.device === winnerDevice ? " is-recommended" : ""}`;
    const heading = document.createElement("div");
    heading.className = "benchmark-result-candidate-heading";
    const name = document.createElement("strong");
    name.textContent = entry.label;
    heading.appendChild(name);
    if (entry.device === winnerDevice) {
      const badge = document.createElement("span");
      badge.className = "benchmark-result-recommended-pill";
      badge.textContent = translateKey("msg.recommended.d70604e8", {}, getActiveLocale()).toUpperCase();
      heading.appendChild(badge);
    } else {
      const slower = document.createElement("span");
      slower.className = "benchmark-result-slower-pill";
      const relativeSpeed = entry.rtf / winner.rtf;
      const qualityQualified = entry.wer <= qualityLimit;
      if (relativeSpeed > 1 + 1e-9) {
        slower.textContent = `${Number(relativeSpeed.toFixed(1))}× ${benchmarkMessage("msg.benchmark_slower.1d20")}${qualityQualified ? "" : ` · ${benchmarkMessage("msg.benchmark_quality_target_missed.1d20")}`}`;
        slower.title = qualityQualified
          ? benchmarkMessage("msg.benchmark_tooltip_slower.1d20")
          : benchmarkMessage("msg.benchmark_tooltip_quality_limit.1d20", { context: benchmarkMessage("msg.benchmark_tooltip_slower_context.1d20"), wer: (entry.wer * 100).toFixed(2), limit: (qualityLimit * 100).toFixed(2) });
      } else if (relativeSpeed < 1 - 1e-9) {
        if (qualityQualified) {
          throw new Error("A faster quality-qualified candidate was not selected as the recommendation.");
        }
        slower.textContent = benchmarkMessage("msg.benchmark_quality_target_missed.1d20");
        slower.title = benchmarkMessage("msg.benchmark_tooltip_quality_limit.1d20", { context: benchmarkMessage("msg.benchmark_tooltip_faster_context.1d20"), wer: (entry.wer * 100).toFixed(2), limit: (qualityLimit * 100).toFixed(2) });
      } else {
        slower.textContent = qualityQualified ? benchmarkMessage("msg.benchmark_similar_speed.1d20") : benchmarkMessage("msg.benchmark_quality_target_missed.1d20");
        slower.title = qualityQualified
          ? benchmarkMessage("msg.benchmark_tooltip_similar_speed.1d20")
          : benchmarkMessage("msg.benchmark_tooltip_quality_limit.1d20", { context: benchmarkMessage("msg.benchmark_tooltip_near_tie_context.1d20"), wer: (entry.wer * 100).toFixed(2), limit: (qualityLimit * 100).toFixed(2) });
      }
      slower.setAttribute("aria-label", slower.title);
      heading.appendChild(slower);
    }

    const metrics = document.createElement("div");
    metrics.className = "benchmark-result-metrics";
    for (const metric of [
      { label: benchmarkMessage("msg.benchmark_metric_median.1d20"), value: `${Math.round(entry.ms)} ms`, detail: benchmarkMessage("msg.benchmark_metric_lower_faster.1d20") },
      { label: benchmarkMessage("msg.benchmark_metric_wer.1d20"), value: `${(entry.wer * 100).toFixed(2)}%`, detail: benchmarkMessage("msg.benchmark_metric_lower_better.1d20") },
      { label: benchmarkMessage("msg.benchmark_metric_cer.1d20"), value: `${(entry.cer * 100).toFixed(2)}%`, detail: benchmarkMessage("msg.benchmark_metric_lower_better.1d20") },
      { label: benchmarkMessage("msg.benchmark_metric_strict_cer.1d20"), value: `${(entry.strictCer * 100).toFixed(2)}%`, detail: benchmarkMessage("msg.benchmark_metric_lower_better.1d20") },
      { label: benchmarkMessage("msg.benchmark_metric_rtf.1d20"), value: entry.rtf.toFixed(3), detail: benchmarkMessage("msg.benchmark_metric_rtf_detail.1d20") },
    ]) {
      const item = document.createElement("div");
      item.className = "benchmark-result-metric";
      const label = document.createElement("span");
      label.textContent = metric.label;
      const strong = document.createElement("strong");
      strong.textContent = metric.value;
      const small = document.createElement("small");
      small.textContent = metric.detail;
      item.append(label, strong, small);
      metrics.appendChild(item);
    }
    card.append(heading, metrics);
    benchmarkResultCandidates.appendChild(card);
  }

  for (const device of ["gpu", "cpu"] as const) {
    if (entries.some((entry) => entry.device === device)) continue;
    const failure = document.createElement("p");
    failure.className = "benchmark-result-failure";
    failure.textContent = benchmarkMessage("msg.benchmark_candidate_failed.1d20", { device: device === "gpu" ? "Apple GPU" : "Apple CPU" });
    benchmarkResultCandidates.appendChild(failure);
  }

  benchmarkInviteTechnicalResult.textContent = JSON.stringify(value, null, 2);
  latestHardwareOptimizationResult = { ...value, comparisonComplete };
  benchmarkInviteResult.classList.remove("hidden");
  benchmarkInviteResult.classList.add("is-complete");
  // Keep the early renderer safe until final server/provider verification.
  benchmarkInviteCloseButton.hidden = false;
  benchmarkInviteCloseButton.disabled = false;
  benchmarkInviteCloseButton.textContent = benchmarkMessage("msg.benchmark_button_default.1d20");
  benchmarkInviteCloseButton.classList.toggle("primary", false);
  benchmarkInviteCloseButton.classList.toggle("secondary", true);
  benchmarkInviteApplyButton.hidden = true;
  benchmarkInviteApplyButton.disabled = true;
  benchmarkInviteApplyButton.textContent = benchmarkMessage("msg.benchmark_button_optimize.1d20");
  benchmarkInviteApplyButton.classList.toggle("primary", false);
  benchmarkInviteApplyButton.classList.toggle("secondary", true);
  benchmarkInviteCustomButton.hidden = false;
  benchmarkInviteExportButton.hidden = false;
  return comparisonComplete;
}

function updateSavedBenchmarkAction(): void {
  if (!seeBenchmarkResultsButton) return;
  const requestedModel = modelSelect.value;
  void invoke<unknown>("get_hardware_optimization_profile", { model: requestedModel })
    .then((value) => {
      const valid = typeof value === "object" && value !== null
        && (value as Record<string, unknown>).schema === "rdt-connect-hardware-optimization-v1"
        && (value as Record<string, unknown>).model === requestedModel
        && requestedModel === modelSelect.value;
      seeBenchmarkResultsButton.classList.toggle("hidden", !valid);
      seeBenchmarkResultsButton.disabled = !valid;
    })
    .catch(() => {
      seeBenchmarkResultsButton.classList.add("hidden");
      seeBenchmarkResultsButton.disabled = true;
    });
}

// Surface an existing model-specific result as soon as the app is ready.
updateSavedBenchmarkAction();

function downloadHardwareBenchmarkJson(): void {
  if (!latestHardwareOptimizationResult) return;
  const modelSlug = modelSelect.value.split("/").pop()?.replace(/[^a-z0-9-]+/gi, "-") || "whisper-model";
  const createdAt = Number(latestHardwareOptimizationResult.createdAtUnixMs) || Date.now();
  const date = new Date(createdAt).toISOString().replace(/[:.]/g, "-");
  const blob = new Blob([JSON.stringify(latestHardwareOptimizationResult, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `rdt-connect-hardware-benchmark-${modelSlug}-${date}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function openManualHardwareSelection(): void {
  setDeveloperOptions(true);
  setAppView("local");
  if (localServerDetails) localServerDetails.open = true;
  const hardwareControls = inferenceDeviceOptions.closest<HTMLElement>(".inference-device-card") || inferenceBenchmarkCard;
  hardwareControls.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
  window.setTimeout(() => inferenceDeviceRadios.find((radio) => radio.value === "auto")?.focus(), 80);
}

async function showSavedHardwareResults(): Promise<void> {
  if (benchmarkRunInProgress) return;
  try {
    const profile = await invoke<unknown>("get_hardware_optimization_profile", { model: modelSelect.value });
    if (!profile) {
      updateSavedBenchmarkAction();
      showNotice(benchmarkMessage("msg.benchmark_saved_results_missing.1d20"), "info");
      return;
    }
    renderHardwareComparison(profile);
    const secondaryActions = document.querySelector<HTMLElement>(
      ".benchmark-invite-secondary-actions",
    );
    if (secondaryActions) {
      const availableButtons = Array.from(
        secondaryActions.querySelectorAll<HTMLButtonElement>("button"),
      ).filter((button) => !button.hidden);
      for (const button of availableButtons) {
        button.classList.remove("hidden");
      }
      secondaryActions.classList.toggle("hidden", availableButtons.length === 0);
    }
    hardwareOptimizationSource = "results";
    benchmarkInviteOverlay.classList.remove("hidden");
    benchmarkProgressPanel.classList.add("hidden");
    benchmarkInviteErrorDetails.classList.add("hidden");
    benchmarkInviteErrorDetails.open = false;
    benchmarkInviteApplyButton.disabled = false;
    benchmarkInviteCustomButton.disabled = false;
    benchmarkInviteExportButton.disabled = false;
    benchmarkInviteCloseButton.hidden = false;
    benchmarkInviteCloseButton.textContent = benchmarkMessage("msg.benchmark_button_close.1d20");
    benchmarkInviteCloseButton.classList.remove("primary");
    benchmarkInviteCloseButton.classList.add("secondary");
    showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_saved_results_status.1d20"), "good");
    benchmarkInvitePanel.classList.add("has-optimizer-result");
    benchmarkInvitePanel.focus({ preventScroll: true });
  } catch (error) {
    showNotice(benchmarkMessage("msg.benchmark_saved_results_open_failed.1d20", { detail: formatHardwareDeviceText(String(error)) }), "warning");
  }
}

let benchmarkInviteClosePromise: Promise<void> | null = null;

function animateBenchmarkOutcomeButton(button: HTMLButtonElement): void {
  button.classList.remove("is-confirming");
  void button.offsetWidth;
  button.classList.add("is-confirming");
  window.setTimeout(() => button.classList.remove("is-confirming"), 280);
}

function closeBenchmarkInvite(): Promise<void> {
  if (benchmarkInviteClosePromise) return benchmarkInviteClosePromise;
  if (benchmarkInviteOverlay.classList.contains("hidden")) return Promise.resolve();

  benchmarkInviteClosePromise = new Promise<void>((resolve) => {
    let finished = false;
    let timer = 0;
    const finish = (): void => {
      if (finished) return;
      finished = true;
      benchmarkInvitePanel.removeEventListener("animationend", onAnimationEnd);
      window.clearTimeout(timer);
      benchmarkInviteOverlay.classList.add("hidden");
      benchmarkInvitePanel.classList.remove("is-selecting-hardware");
      benchmarkInvitePanel.classList.remove("rdt-update-exit", "has-optimizer-result");
      if (hardwareOptimizationSource === "manual") {
        document.getElementById("open-native-benchmark-button")?.focus();
      } else if (hardwareOptimizationSource === "results") {
        seeBenchmarkResultsButton?.focus();
      } else {
        startButton.focus();
      }
      benchmarkInviteClosePromise = null;
      resolve();
    };
    const onAnimationEnd = (event: AnimationEvent): void => {
      if (event.target === benchmarkInvitePanel && event.animationName === "rdtCrtOff") finish();
    };
    benchmarkInvitePanel.classList.remove("rdt-update-exit");
    void benchmarkInvitePanel.offsetWidth;
    benchmarkInvitePanel.addEventListener("animationend", onAnimationEnd);
    benchmarkInvitePanel.classList.add("rdt-update-exit");
    timer = window.setTimeout(finish, 380);
  });
  return benchmarkInviteClosePromise;
}

// First-run optimization is intentionally non-dismissible by outside click or Escape.
benchmarkInviteOverlay.addEventListener("click", (event) => {
  if (event.target === benchmarkInviteOverlay) event.preventDefault();
});
window.addEventListener("keydown", (event) => {
  if (benchmarkInviteOverlay.classList.contains("hidden")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  if (event.key === "Tab") {
    const focusable = Array.from(benchmarkInvitePanel.querySelectorAll<HTMLElement>(
      'button:not([hidden]):not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
    )).filter((node) => node.getClientRects().length > 0);
    if (!focusable.length) {
      event.preventDefault();
      benchmarkInvitePanel.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !benchmarkInvitePanel.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !benchmarkInvitePanel.contains(document.activeElement))) {
      event.preventDefault();
      first.focus();
    }
  }
}, true);

function resetFirstRunOptimization(): void {
  if (benchmarkRunInProgress) {
    if (trayTestPhraseStatus) trayTestPhraseStatus.textContent =
      "Hardware optimization is already running.";
    return;
  }
  setFirstRunHardwareOptimizationResolved(false);
  clearHardwareOptimizationPendingMarker();
  writeHardwareOptimizationInterruptionCount(0);
  setHardwareOptimizationRetryNoticePending(false);
  if (trayTestPhraseStatus) trayTestPhraseStatus.textContent =
    "First-run hardware check reset. It will appear the next time you click Start Server.";
}

benchmarkInviteCloseButton.addEventListener("click", async () => {
  if (benchmarkInviteCloseButton.disabled) return;
  animateBenchmarkOutcomeButton(benchmarkInviteCloseButton);
  await closeBenchmarkInvite();
  resolveHardwareOptimizationChoice?.("default");
  resolveHardwareOptimizationChoice = null;
  hardwareOptimizationSource = null;
});

benchmarkInviteApplyButton.addEventListener("click", async () => {
  if (benchmarkInviteApplyButton.disabled) return;
  animateBenchmarkOutcomeButton(benchmarkInviteApplyButton);
  if (latestHardwareOptimizationResult?.comparisonComplete !== true) {
    showHardwareOptimizationMessage(
      benchmarkMessage("msg.benchmark_incomplete_save_warning.1d20"),
      "warning",
    );
    return;
  }
  benchmarkInviteApplyButton.disabled = true;
  benchmarkInviteApplyButton.textContent = benchmarkMessage("msg.benchmark_saving_optimized.1d20");
  try {
    const recommendationModel = latestHardwareOptimizationResult?.model;
    if (typeof recommendationModel !== "string" || !recommendationModel.trim()) {
      throw new Error(benchmarkMessage("msg.benchmark_model_unavailable.1d20"));
    }
    await invoke("apply_hardware_optimization_profile", { model: recommendationModel });
    await closeBenchmarkInvite();
    resolveHardwareOptimizationChoice?.("optimized");
    resolveHardwareOptimizationChoice = null;
    hardwareOptimizationSource = null;
    const winnerDevice = latestHardwareOptimizationResult?.winnerDevice;
    const currentProvider = lastConnectionCheck?.inferenceProvider?.provider ?? "";
    const recommendationIsLoaded = (winnerDevice === "gpu" || winnerDevice === "cpu")
      && backend.running
      && backend.model === modelSelect.value
      && lastConnectionCheck?.healthOk === true
      && lastConnectionCheck?.authOk === true
      && providerMatchesHardwareChoice(winnerDevice, currentProvider);
    showNotice(
      inferenceDevicePreference !== "auto"
      ? benchmarkMessage("msg.benchmark_saved_manual_preference.1d20", { model: modelSelect.value, preference: inferenceDevicePreference.toUpperCase() })
      : recommendationIsLoaded
      ? benchmarkMessage("msg.benchmark_saved_currently_running.1d20")
      : benchmarkMessage("msg.benchmark_saved_next_model_start.1d20"),
      "good",
    );
    renderInferenceHardwareStatus();
    updateSavedBenchmarkAction();
  } catch (error) {
    benchmarkInviteApplyButton.disabled = false;
    benchmarkInviteApplyButton.textContent = benchmarkMessage("msg.benchmark_button_optimize.1d20");
    showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_save_failed_preserved.1d20", { detail: formatHardwareDeviceText(String(error)) }), "warning");
  }
});

benchmarkInviteCustomButton.addEventListener("click", async () => {
  const source = hardwareOptimizationSource;
  benchmarkInviteCustomButton.disabled = true;
  try {
    if (source === "results" || source === "manual" || source === "first-run") {
      if (busy || serverOperation !== null || modelDownloadStatus.active || modelStorageBusy) {
        throw new Error(benchmarkMessage("msg.benchmark_operation_in_progress.1d20"));
      }
      await refreshBackendStatus();
    }
    await closeBenchmarkInvite();
    resolveHardwareOptimizationChoice?.("manual");
    resolveHardwareOptimizationChoice = null;
    if (source === "results" || source === "manual" || source === "first-run") {
      hardwareOptimizationSource = null;
      openManualHardwareSelection();
    }
  } catch (error) {
    benchmarkInviteCustomButton.disabled = false;
    showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_manual_settings_failed.1d20", { detail: formatHardwareDeviceText(String(error)) }), "warning");
  }
});

benchmarkInviteExportButton.addEventListener("click", downloadHardwareBenchmarkJson);
seeBenchmarkResultsButton?.addEventListener("click", () => void showSavedHardwareResults());

function isNonRecoverableBenchmarkContractError(errorText: string): boolean {
  // A 404 for the capability/lease routes means the installed bundled backend
  // is from an older build. Restarting that same executable cannot add routes.
  // Authentication and startup errors remain retryable because a fresh managed
  // process can regenerate credentials and recover the local session.
  return /Benchmark capability check failed \(HTTP 404\)|Benchmark capability check failed \(404\)|Benchmark lease acquisition failed \(HTTP 404\)|missing the current benchmark safety API|benchmark fixture.*(?:SHA-256|metadata|attribution).*invalid/i.test(errorText);
}

async function waitForHardwareBackendIdle(timeoutMs = 30_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const status = await invoke<BackendStatus>("get_backend_status");
    backend = status;
    if (!status.running) {
      lastConnectionCheck = null;
      renderBackendStatus();
      renderConnectionStatus();
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 300));
  }
  throw new Error(benchmarkMessage("msg.benchmark_previous_server_not_released.1d20"));
}

async function stopHardwareCandidateAndWait(): Promise<void> {
  backend = await invoke<BackendStatus>("stop_hardware_candidate_backend");
  lastConnectionCheck = null;
  renderBackendStatus();
  await waitForHardwareBackendIdle();
}

function providerMatchesHardwareChoice(device: "gpu" | "cpu", provider: string): boolean {
  return device === "gpu"
    ? provider === "mlx-metal-gpu"
    : provider === "mlx-cpu-selected";
}

async function runHardwareOptimizationFlow(
  source: "first-run" | "manual" | "preview",
  qaFirstRunSimulation = false,
): Promise<HardwareOptimizationChoice | "skipped"> {
  if (benchmarkRunInProgress || benchmarkFlowReserved) return "skipped";
  if (source !== "first-run" && !developerOptionsEnabled) return "skipped";
  benchmarkFlowReserved = true;
  try {
    return await runHardwareOptimizationFlowReserved(source, qaFirstRunSimulation);
  } finally {
    benchmarkFlowReserved = false;
  }
}

async function runHardwareOptimizationFlowReserved(
  source: "first-run" | "manual" | "preview",
  qaFirstRunSimulation = false,
): Promise<HardwareOptimizationChoice | "skipped"> {
  if (benchmarkRunInProgress) return "skipped";
  benchmarkInviteIntroFirstLine.textContent = benchmarkMessage("msg.benchmark_intro_finding.1d22");
  if (source !== "first-run" && !developerOptionsEnabled) return "skipped";
  const manualFiniteCandidateCount =
    source === "manual" && manualHardwareTimingLimitCheckbox.checked
      ? (pendingManualHardwareSelection?.devices.length ?? 0)
      : 0;
  const benchmarkBudgetMs = source === "first-run"
    ? 250_000
    : manualFiniteCandidateCount > 0
      ? manualFiniteCandidateCount * 60_000
      : Number.POSITIVE_INFINITY;
  let benchmarkDeadline = 0;
  let activeCandidateDeadline = Number.POSITIVE_INFINITY;
  let nativeBenchmarkInProgress = false;
  let candidateStartCutoff = 0;
  keepWinnerBackendRunning = false;

  // Manual MLX support is deliberately separate from mandatory first-run policy.
  // Apple Silicon's integrated GPU/CPU remain available through the manual action.
  if (source === "first-run" && !qaFirstRunSimulation) {
    const required = await invoke<boolean>(
      "is_first_run_hardware_optimization_required",
    ).catch(() => false);
    if (!required) {
      completeFirstRunHardwareOptimization();
      showNotice(
        benchmarkMessage("msg.benchmark_not_required_notice.1d21"),
        "info",
      );
      return "skipped";
    }
  }

  const supported = await invoke<boolean>("is_hardware_optimization_supported").catch(() => false);
  if (!supported) {
    if (source === "first-run" && !qaFirstRunSimulation) completeFirstRunHardwareOptimization();
    showNotice(benchmarkMessage("msg.benchmark_unsupported_notice.1d21"), "info");
    return "skipped";
  }

  if (busy || serverOperation !== null || modelDownloadStatus.active || modelStorageBusy) {
    showNotice(benchmarkMessage("msg.benchmark_busy_notice.1d21"), "warning");
    return "skipped";
  }

  const resolvedModel = await resolveBenchmarkModel();
  if (!resolvedModel) return "skipped";
  const selectedModel = resolvedModel.model;
  const cache = resolvedModel.cache;
  modelStorageStatus = cache;
  renderModelStorageSummary(cache);
  renderModelDownloadUI();
  const https = await refreshLocalHttpsStatus(false);
  if (!https || https.mode !== "https" || !https.configured || !https.trusted) {
    showNotice(benchmarkMessage("msg.benchmark_https_required_notice.1d21"), "warning");
    return "skipped";
  }

  benchmarkInviteOverlay.classList.remove("hidden");
  if (source === "first-run") {
    benchmarkDeadline = performance.now() + benchmarkBudgetMs;
    candidateStartCutoff = benchmarkDeadline - 100_000;
  } else {
    benchmarkDeadline = Number.isFinite(benchmarkBudgetMs)
      ? performance.now() + benchmarkBudgetMs
      : Number.POSITIVE_INFINITY;
    candidateStartCutoff = Number.POSITIVE_INFINITY;
  }
  benchmarkProgressPanel.classList.remove("hidden");
  benchmarkInviteResult.classList.add("hidden");
  benchmarkInviteResult.classList.remove("is-complete");
  benchmarkInviteErrorDetails.classList.add("hidden");
  benchmarkInviteErrorDetails.open = false;
  benchmarkInviteErrorText.textContent = "";
  benchmarkInviteApplyButton.hidden = true;
  benchmarkInviteCustomButton.hidden = true;
  benchmarkInviteExportButton.hidden = true;
  benchmarkInviteApplyButton.disabled = true;
  benchmarkInviteCloseButton.hidden = true;
  benchmarkInviteCloseButton.textContent = benchmarkMessage("msg.benchmark_button_default.1d20");
  benchmarkInvitePanel.focus({ preventScroll: true });
  showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_getting_ready.1d20"), "pending");
  renderDigitalBenchmarkProgress(1, 2, benchmarkMessage("msg.benchmark_apple_gpu.1d22"), 0, 0, 8);
  clearNativeBenchmarkResult();
  updateSavedBenchmarkAction();

  benchmarkRunInProgress = true;
  hardwareOptimizationSource = source;
  if (source === "first-run" && !qaFirstRunSimulation) beginFirstRunHardwareOptimizationSession(source);
  let candidateStarted = false;
  let reusedOriginalBackend = false;
  let startingGuard = false;
  let startupTimedOut = false;
  let lifecycleUnsafe = false;
  let resultChoice: HardwareOptimizationChoice = "default";
  let progressUnlisten: (() => void) | null = null;
  const selectedPort = Number(preferredPortInput.value) || 8888;
  let originalBackendWasRunning = false;
  let originalBackendSnapshot: BackendStatus | null = null;
  let originalBackendProvider: string | null = null;
  let originalBackendProtocol: string | null = null;
  let originalBackendStoppedForBenchmark = false;
  let originalBackendRestored = false;
  let originalBackendPreserved = false;
  const originalInferenceDevicePreference = inferenceDevicePreference;

  const verifyAndPreserveReusedOriginal = async (notifyUser = true): Promise<"preserved" | "stopped" | "unsafe"> => {
    const unsafe = (message: string): "unsafe" => {
      lifecycleUnsafe = true;
      candidateStarted = false;
      reusedOriginalBackend = false;
      keepWinnerBackendRunning = false;
      showNotice(message, "error");
      return "unsafe";
    };

    if (!originalBackendSnapshot) {
      return unsafe("The reused original server has no complete identity snapshot. It was left untouched.");
    }

    try {
      const observed = await invoke<BackendStatus>("get_backend_status");
      if (!observed.running) {
        backend = observed;
        candidateStarted = false;
        reusedOriginalBackend = false;
        originalBackendStoppedForBenchmark = true;
        renderBackendStatus();
        return "stopped";
      }

      const observedProtocol = observed.localUrl ? new URL(observed.localUrl).protocol : null;
      if (!observed.pid
          || observed.pid !== originalBackendSnapshot.pid
          || observed.model !== originalBackendSnapshot.model
          || observed.port !== originalBackendSnapshot.port
          || observed.localUrl !== originalBackendSnapshot.localUrl
          || (originalBackendProtocol && observedProtocol !== originalBackendProtocol)
          || inferenceDevicePreference !== originalInferenceDevicePreference) {
        backend = observed;
        return unsafe("A running server no longer matches the original PID, model, port, URL, transport, or preference. It was left untouched.");
      }

      backend = observed;
      await refreshConnectionStatus();
      const check = lastConnectionCheck;
      const checkedProtocol = check?.localUrl ? new URL(check.localUrl).protocol : null;
      const actualProvider = check?.inferenceProvider?.provider ?? "";
      if (!check?.running
          || check.pid !== originalBackendSnapshot.pid
          || check.port !== originalBackendSnapshot.port
          || check.localUrl !== originalBackendSnapshot.localUrl
          || check.healthOk !== true
          || check.authOk !== true
          || (originalBackendProtocol && checkedProtocol !== originalBackendProtocol)
          || (originalBackendProvider && actualProvider !== originalBackendProvider)
          || inferenceDevicePreference !== originalInferenceDevicePreference) {
        return unsafe("The original server is still running, but fresh authenticated health/provider verification did not pass. It was left untouched.");
      }

      originalBackendPreserved = true;
      candidateStarted = false;
      reusedOriginalBackend = false;
      keepWinnerBackendRunning = true;
      renderBackendStatus();
      if (notifyUser) {
      showHardwareOptimizationMessage(
        benchmarkMessage("msg.benchmark_attempt_incomplete_original_verified.1d20"),
        "warning",
      );
      }
      return "preserved";
    } catch (error) {
      return unsafe(`The original server could not be safely verified and was left untouched. ${String(error)}`);
    }
  };

  const restoreOriginalBackendIfNeeded = async (): Promise<void> => {
    if (originalBackendWasRunning && originalBackendStoppedForBenchmark
        && !originalBackendPreserved && !originalBackendRestored
        && !lifecycleUnsafe && !keepWinnerBackendRunning && originalBackendSnapshot) {
      try {
        if (inferenceDevicePreference !== originalInferenceDevicePreference) {
          throw new Error(benchmarkMessage("msg.benchmark_preference_changed.1d20"));
        }

        const current = await invoke<BackendStatus>("get_backend_status");
        if (current.running) {
          if (current.pid !== originalBackendSnapshot.pid
              || current.model !== originalBackendSnapshot.model
              || current.port !== originalBackendSnapshot.port) {
            throw new Error(benchmarkMessage("msg.benchmark_different_backend.1d20"));
          }
          backend = current;
        } else {
          backend = await invoke<BackendStatus>("start_backend", {
            model: originalBackendSnapshot.model,
            preferredPort: originalBackendSnapshot.port,
          });
        }

        if (!backend.running
            || backend.model !== originalBackendSnapshot.model
            || backend.port !== originalBackendSnapshot.port
            || !backend.pid) {
          throw new Error("The original server did not return with its exact model and port.");
        }

        const restoredProtocol = backend.localUrl ? new URL(backend.localUrl).protocol : null;
        if (originalBackendProtocol && restoredProtocol !== originalBackendProtocol) {
          throw new Error("The original local HTTP/HTTPS transport could not be restored.");
        }

        await refreshConnectionStatus();
        const check = lastConnectionCheck;
        if (!check?.healthOk || check.authOk !== true
            || check.port !== originalBackendSnapshot.port
            || (check.pid !== null && check.pid !== backend.pid)) {
          throw new Error("The restored server failed its authenticated health or process-identity check.");
        }
        const restoredProvider = check.inferenceProvider?.provider ?? "";
        if (originalBackendProvider && restoredProvider !== originalBackendProvider) {
          throw new Error("The restored server's actual inference provider differs from its original provider.");
        }
        if (inferenceDevicePreference !== originalInferenceDevicePreference) {
          throw new Error("The explicit hardware preference no longer matches its original value.");
        }

        originalBackendRestored = true;
        showNotice(
          `The original local server was restored on ${originalBackendSnapshot.model}, port ${originalBackendSnapshot.port}, with its original verified transport and provider.`,
          "good",
        );
      } catch (restoreError) {
        lifecycleUnsafe = true;
        showNotice(
          `RDT Connect could not safely restore the original server. No replacement server will be started. ${formatHardwareDeviceText(String(restoreError))}`,
          "error",
        );
      }
    }
  };

  const updateBenchmarkCountdown = (): void => {
    const countdownDeadline = source === "manual" ? activeCandidateDeadline : benchmarkDeadline;
    const hasFiniteDeadline = Number.isFinite(countdownDeadline);
    benchmarkProgressRemaining.hidden = !hasFiniteDeadline;
    benchmarkProgressRemaining.classList.toggle("hidden", !hasFiniteDeadline);
    if (!hasFiniteDeadline) {
      benchmarkProgressRemaining.textContent = "";
      return;
    }
    const remaining = Math.max(0, Math.ceil((countdownDeadline - performance.now()) / 1000));
    benchmarkProgressRemaining.textContent = remaining === 0 && nativeBenchmarkInProgress
      ? benchmarkMessage("msg.benchmark_stage_finishing_up.1d20")
      : benchmarkMessage("msg.benchmark_countdown_remaining.1d23", { seconds: remaining });
  };
  updateBenchmarkCountdown();
  benchmarkCountdownTimer = window.setInterval(updateBenchmarkCountdown, 1000);
  updateBenchmarkCountdown();

  try {
  let preferredFirstDevice: "gpu" | "cpu" = "gpu";
  const manualHardwareSelection = source === "manual" ? pendingManualHardwareSelection : null;
  const selectedDeviceIds: ManualHardwareDevice[] = source === "manual"
    ? [...(manualHardwareSelection?.devices ?? [])]
    : ["gpu", "cpu"];
  if (source === "manual" && (
    !manualHardwareSelection
    || selectedDeviceIds.length !== 2
    || !selectedDeviceIds.includes("gpu")
    || !selectedDeviceIds.includes("cpu")
    || new Set(selectedDeviceIds).size !== selectedDeviceIds.length
  )) {
    throw new Error(benchmarkMessage("msg.benchmark_minimum_devices.1d20"));
  }
  const loadPreferredFirstDevice = async (): Promise<"gpu" | "cpu"> => {
    const demoted = await invoke<string[]>("get_hardware_candidate_demotions");
    const gpuDemoted = demoted.includes("gpu:mlx-metal-gpu");
    const cpuDemoted = demoted.includes("cpu:mlx-cpu-selected");
    return gpuDemoted && !cpuDemoted ? "cpu" : "gpu";
  };

    progressUnlisten = await listenNative<{
      phase?: string;
      completed?: number;
      measuredCompleted?: number;
      message?: string;
      candidateIndex?: number;
      candidateCount?: number;
      candidateLabel?: string;
      stepCompleted?: number;
      stepTotal?: number;
    }>("rdt-native-benchmark-progress", ({ payload }) => {
      updateNativeBenchmarkProgressFromEvent(payload);
      if (typeof payload.candidateIndex === "number") {
        renderDigitalBenchmarkProgress(
          payload.candidateIndex,
          payload.candidateCount ?? 2,
          payload.candidateLabel ?? (payload.candidateIndex === 1 ? "Apple GPU" : "Apple CPU"),
          Math.max(0, payload.candidateIndex - 1),
          payload.stepCompleted ?? 0,
          payload.stepTotal ?? 8,
        );
      }
      if (payload.phase === "warmup") showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_phase_warmup.1d21"), "pending");
      else if (payload.phase === "measuring") showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_phase_measuring.1d21"), "pending");
      else if (payload.phase === "failed") showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_phase_retry.1d21"), "pending");
    }).catch(() => null);

    await invoke("set_native_benchmark_starting", { enabled: true });
    startingGuard = true;
    const recommendedFirstDevice = await loadPreferredFirstDevice();
    preferredFirstDevice = manualHardwareSelection?.order === "gpu"
      ? "gpu"
      : manualHardwareSelection?.order === "cpu"
        ? "cpu"
        : recommendedFirstDevice;
    if (!selectedDeviceIds.includes(preferredFirstDevice)) {
      preferredFirstDevice = selectedDeviceIds[0];
    }

    // Reuse a verified server for the first candidate when it already has the
    // selected model loaded. This avoids throwing away a warm, ready model.
    await refreshBackendStatus();
    let reusableDevice: "gpu" | "cpu" | null = null;
    originalBackendWasRunning = backend.running;
    if (originalBackendWasRunning) {
      if (!backend.model || !backend.port || backend.port < 1 || !backend.pid) {
        throw new Error("The running server did not report its exact model, port, and process identity; it was left untouched.");
      }
      originalBackendSnapshot = { ...backend };
      originalBackendProtocol = backend.localUrl ? new URL(backend.localUrl).protocol : null;
      await refreshConnectionStatus();
      const provider = lastConnectionCheck?.inferenceProvider?.provider ?? "";
      originalBackendProvider = provider || null;
      if (backend.model === selectedModel
          && lastConnectionCheck?.healthOk === true
          && lastConnectionCheck.authOk === true) {
        if (providerMatchesHardwareChoice("gpu", provider)) reusableDevice = "gpu";
        else if (providerMatchesHardwareChoice("cpu", provider)) reusableDevice = "cpu";
      }
      if (reusableDevice === preferredFirstDevice) {
        showHardwareOptimizationMessage(`${benchmarkMessage("msg.benchmark_reuse_original_server.1d23", { device: reusableDevice === "gpu" ? benchmarkMessage("msg.benchmark_apple_gpu.1d22") : benchmarkMessage("msg.benchmark_apple_cpu.1d22") })}`, "pending");
      } else {
        showHardwareOptimizationMessage(
          reusableDevice
            ? `${benchmarkMessage("msg.benchmark_stop_original_for_first.1d23", { device: reusableDevice === "gpu" ? benchmarkMessage("msg.benchmark_apple_gpu.1d22") : benchmarkMessage("msg.benchmark_apple_cpu.1d22"), nextDevice: preferredFirstDevice === "gpu" ? benchmarkMessage("msg.benchmark_apple_gpu.1d22") : benchmarkMessage("msg.benchmark_apple_cpu.1d22") })}`
            : benchmarkMessage("msg.benchmark_stop_current_server.1d23"),
          "pending",
        );
        try {
          await stopHardwareCandidateAndWait();
          originalBackendStoppedForBenchmark = true;
        } catch (stopError) {
          const observed = await invoke<BackendStatus>("get_backend_status").catch(() => null);
          if (observed && !observed.running) {
            backend = observed;
            originalBackendStoppedForBenchmark = true;
          } else {
            if (observed) backend = observed;
            lifecycleUnsafe = true;
          }
          throw new Error(benchmarkMessage("msg.benchmark_original_stop_failed.1d23", { detail: formatHardwareDeviceText(String(stopError)) }));
        }
      }
    }

    const gpuCandidate = { device: "gpu" as const, label: benchmarkMessage("msg.benchmark_apple_gpu.1d22"), provider: "mlx-metal-gpu", reuse: reusableDevice === "gpu" && !originalBackendStoppedForBenchmark };
    const cpuCandidate = { device: "cpu" as const, label: benchmarkMessage("msg.benchmark_apple_cpu.1d22"), provider: "mlx-cpu-selected", reuse: reusableDevice === "cpu" && !originalBackendStoppedForBenchmark };
    const candidateByDevice = { gpu: gpuCandidate, cpu: cpuCandidate };
    const candidateOrder: ManualHardwareDevice[] = preferredFirstDevice === "cpu"
      ? ["cpu", "gpu"]
      : ["gpu", "cpu"];
    const candidates = candidateOrder
      .filter((device) => selectedDeviceIds.includes(device))
      .map((device) => candidateByDevice[device]);
    const selectedDevicesForFinalizer = candidates.map((candidate) => candidate.device);
    const optimizationSessionId = typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;

    let completedCandidates = 0;
    const candidateCount = candidates.length;
    const candidateFailures: Record<string, string> = {};
    for (const [candidateOffset, candidate] of candidates.entries()) {
      const candidateDeadline = source === "manual"
        ? (manualHardwareTimingLimitCheckbox.checked
            ? performance.now() + 60_000
            : Number.POSITIVE_INFINITY)
        : candidateStartCutoff;
      activeCandidateDeadline = candidateDeadline;
      updateBenchmarkCountdown();
      if (performance.now() >= candidateDeadline) {
        candidateFailures[candidate.device] = benchmarkMessage("msg.benchmark_time_reserve_candidate.1d20");
        break;
      }
      const candidateIndex = candidateOffset + 1;
      let completed = false;
      let skipRemainingCandidates = false;
      let lastError = "";
      let timeoutCount = 0;
      let startupRecoveryAttempts = 0;
      let startupRecoveryFailures = 0;
      // Up to 90 seconds for process startup, local health/auth checks,
      // and recovery. The eight benchmark requests never count against this clock.
      let candidateStartupSpentMs = 0;
      renderDigitalBenchmarkProgress(candidateIndex, candidateCount, candidate.label, completedCandidates, 0, 8);

      for (let attempt = 1; attempt <= 3; attempt += 1) {
        if (performance.now() >= candidateDeadline) {
          lastError = benchmarkMessage("msg.benchmark_time_reserve_attempt.1d20");
          break;
        }
        const remainingStartupMs = Math.min(
          90_000 - candidateStartupSpentMs,
          Math.floor(candidateDeadline - performance.now()) - 20_750,
        );
        if (remainingStartupMs < 1_000) {
          lastError = benchmarkMessage("msg.benchmark_startup_allowance_expired.1d20");
          break;
        }
        const reusingCandidate = candidate.reuse && attempt === 1;
        if (!reusingCandidate) startupRecoveryAttempts += 1;
        let nativeBenchmarkStarted = false;
        let startupCallStartedAt = Date.now();
        try {
          if (candidate.reuse && attempt === 1) {
            candidateStarted = true;
            reusedOriginalBackend = originalBackendWasRunning;
            backend = await invoke<BackendStatus>("get_backend_status");
            candidateStartupSpentMs = 0;
            startupCallStartedAt = 0;
          } else {
            showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_prepare_attempt.1d23", { device: candidate.device === "gpu" ? benchmarkMessage("msg.benchmark_apple_gpu.1d22") : benchmarkMessage("msg.benchmark_apple_cpu.1d22"), attempt }), "pending");
            updateNativeBenchmarkStage(1, benchmarkMessage("msg.benchmark_starting_isolated.1d23", { device: candidate.device === "gpu" ? benchmarkMessage("msg.benchmark_apple_gpu.1d22") : benchmarkMessage("msg.benchmark_apple_cpu.1d22") }), { candidateIndex, candidateCount, candidateLabel: candidate.label });
            backend = await invoke<BackendStatus>("start_hardware_candidate_backend", {
              model: selectedModel,
              preferredPort: selectedPort,
              device: candidate.device,
              startupTimeoutSeconds: Math.max(1, Math.min(90, Math.floor(Math.min(remainingStartupMs, benchmarkDeadline - performance.now()) / 1000))),
            });
            updateNativeBenchmarkStage(2, benchmarkMessage("msg.benchmark_provider_check.1d23"), {
              candidateIndex, candidateCount, candidateLabel: candidate.label,
            });

            candidateStartupSpentMs += Date.now() - startupCallStartedAt;
            startupCallStartedAt = 0;
            candidateStarted = true;
            renderBackendStatus();
          }
          if (candidateStartupSpentMs > 90_000) throw new Error(`${candidate.label} exceeded the 90-second startup allowance.`);

          const healthCheckStartedAt = Date.now();
          try {
            await refreshConnectionStatus();
          } finally {
            candidateStartupSpentMs += Date.now() - healthCheckStartedAt;
          }
          if (candidateStartupSpentMs > 90_000) throw new Error(`${candidate.label} exceeded the 90-second startup and verification allowance.`);
          const actual = lastConnectionCheck?.inferenceProvider?.provider ?? "";
          if (!lastConnectionCheck?.healthOk || lastConnectionCheck.authOk !== true) {
            throw new Error(`${candidate.label} did not pass the verified local connection check.`);
          }
          // Fresh candidates have not run inference yet; passive health checks
          // intentionally report the cached "not-yet-selected" provider.
          // Native benchmark responses qualify the provider on every transcription.
          // Reused backends must already match before they can be benchmarked.
          if (candidate.reuse && attempt === 1 && !providerMatchesHardwareChoice(candidate.device, actual)) {
            throw new Error(`${candidate.label} could not be confirmed as the active provider. The fallback or mismatched result was rejected.`);
          }

          showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_measuring_device.1d23", { device: candidate.device === "gpu" ? benchmarkMessage("msg.benchmark_apple_gpu.1d22") : benchmarkMessage("msg.benchmark_apple_cpu.1d22") }), "pending");
          // Inference runtime is intentionally outside the startup/recovery clock.
          updateNativeBenchmarkStage(3, benchmarkMessage("msg.benchmark_initializing_runtime.1d23"), {
            candidateLabel: candidate.label,
          });
          const remainingCandidateBudgetMs = source === "manual"
            ? 150_000
            : Math.max(0, Math.floor(candidateDeadline - performance.now()) - 750);
          if (source !== "manual" && Number.isFinite(candidateDeadline) && remainingCandidateBudgetMs < 20_000) {
            lastError = "Less than 20 seconds remain in the first-run candidate phase; no new inference test was started.";
            skipRemainingCandidates = true;
            break;
          }
nativeBenchmarkStarted = true;
            const nativeRunPromise = invoke<unknown>("run_hardware_native_benchmark", {
              remainingBudgetMs: remainingCandidateBudgetMs,
              fixtureMode: source === "manual" ? "manual" : "first-run",
              candidate: candidate.device,
              optimizationSessionId,
              backendReadyMs: candidate.reuse && attempt === 1 ? 0 : Math.max(1, Math.round(candidateStartupSpentMs)),
              backendReused: candidate.reuse && attempt === 1,
              candidateIndex,
              candidateCount,
            });
            // The allowance controls UI timing only. Never cancel active native inference.
            nativeBenchmarkInProgress = true;
            try {
              await nativeRunPromise;
            } finally {
              nativeBenchmarkInProgress = false;
              updateBenchmarkCountdown();
            }
          completed = true;
          completedCandidates += 1;
          await invoke<string[]>("record_hardware_candidate_outcome", {
            device: candidate.device,
            successful: true,
          }).catch((historyError) => {
            console.warn("Could not clear the temporary hardware-candidate demotion after success.", historyError);
          });
          if (candidateOffset < candidateCount - 1) {
            showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_candidate_passed.1d23", { device: candidate.device === "gpu" ? benchmarkMessage("msg.benchmark_apple_gpu.1d22") : benchmarkMessage("msg.benchmark_apple_cpu.1d22") }), "pending");
            try {
              const stoppingReusedOriginal = reusedOriginalBackend;
              await stopHardwareCandidateAndWait();
              if (stoppingReusedOriginal) originalBackendStoppedForBenchmark = true;
              candidateStarted = false;
              reusedOriginalBackend = false;
              await new Promise((resolve) => window.setTimeout(resolve, 650));
            } catch (unloadError) {
              // If shutdown is slow but the successful candidate is still healthy,
              // preserve it as a single-device result rather than losing good work
              // or starting a second process over the one that remains managed.
              const observed = await invoke<BackendStatus>("get_backend_status").catch(() => null);
              if (observed && !observed.running) {
                backend = observed;
                if (reusedOriginalBackend) originalBackendStoppedForBenchmark = true;
                candidateStarted = false;
                reusedOriginalBackend = false;
              } else if (observed?.running && observed.model === selectedModel) {
                backend = observed;
                await refreshConnectionStatus().catch(() => undefined);
                const provider = lastConnectionCheck?.inferenceProvider?.provider ?? "";
                if (lastConnectionCheck?.healthOk === true
                    && lastConnectionCheck.authOk === true
                    && providerMatchesHardwareChoice(candidate.device, provider)) {
                  const skipped = candidates[candidateOffset + 1];
                  candidateFailures[skipped.device] = benchmarkMessage("msg.benchmark_candidate_unloaded_failure.1d21", { device: candidate.label });
                  skipRemainingCandidates = true;
                  showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_other_candidate_skipped.1d21", { device: candidate.label }), "warning");
                } else {
                  lifecycleUnsafe = true;
                  throw new Error(`RDT Connect could not safely unload ${candidate.label}, and the remaining server's health or provider could not be verified. ${String(unloadError)}`);
                }
              } else {
                lifecycleUnsafe = true;
                throw new Error(`RDT Connect could not confirm that ${candidate.label} had stopped. It will not start another backend. ${String(unloadError)}`);
              }
            }
          } else {
            // Keep the last loaded model alive. After the comparison, we only
            // change hardware if that final loaded candidate is not the winner.
            renderBackendStatus();
            candidateStarted = true;
          }
          renderDigitalBenchmarkProgress(candidateIndex, candidateCount, candidate.label, completedCandidates, 8, 8);
          break;
        } catch (error) {
          if (startupCallStartedAt > 0) {
            candidateStartupSpentMs += Date.now() - startupCallStartedAt;
            startupCallStartedAt = 0;
          }
          lastError = String(error);
          const portCollision = /address already in use|address in use|eaddrinuse|errno (?:48|98)|os error (?:48|98)|10048/i.test(lastError);
          const portSelectionFailure = /could not obtain an os-assigned loopback port|could not read the os-assigned loopback port|could not find an available randomized five-digit loopback port|checked loopback-port selections/i.test(lastError);
          const cutoffOrCancellation = /shared 150-second candidate cutoff|benchmark time reserve|less than 20 seconds|cancel(?:led|lation|ed)/i.test(lastError);
          if (!nativeBenchmarkStarted && !reusingCandidate && !portCollision && !portSelectionFailure && !cutoffOrCancellation) startupRecoveryFailures += 1;
          if (/did not become healthy within \d+ seconds|startup.*timed out|timed out.*startup|90-second candidate startup/i.test(lastError)) timeoutCount += 1;

          // A failed start can time out after the process has been spawned. Check
          // the manager before retrying so we never launch a duplicate backend.
          if (!candidateStarted) {
            const observed = await invoke<BackendStatus>("get_backend_status").catch(() => null);
            if (observed?.running && observed.model === selectedModel) {
              backend = observed;
              candidateStarted = true;
            }
          }
          if (candidateStarted && reusedOriginalBackend && !originalBackendStoppedForBenchmark) {
            const cleanupStartedAt = Date.now();
            const disposition = await verifyAndPreserveReusedOriginal();
            candidateStartupSpentMs += Date.now() - cleanupStartedAt;
            if (disposition === "preserved") {
              throw new Error(benchmarkMessage("msg.benchmark_attempt_failed_original_preserved.1d20", { detail: lastError }));
            }
            if (disposition === "unsafe") {
              throw new Error(benchmarkMessage("msg.benchmark_original_unverified.1d20", { detail: lastError }));
            }
          } else if (candidateStarted) {
            const cleanupStartedAt = Date.now();
            try {
              await stopHardwareCandidateAndWait();
              candidateStarted = false;
              reusedOriginalBackend = false;
              candidateStartupSpentMs += Date.now() - cleanupStartedAt;
            } catch (stopError) {
              candidateStartupSpentMs += Date.now() - cleanupStartedAt;
              lifecycleUnsafe = true;
              throw new Error(`RDT Connect could not confirm the temporary candidate had stopped. ${String(stopError)}`);
            }
          }
          if (isNonRecoverableBenchmarkContractError(lastError)) {
            console.error("The bundled benchmark server contract cannot be repaired by restarting the same installed backend.", lastError);
            break;
          }
          if (attempt < 3 && candidateStartupSpentMs < 90_000) {
            renderDigitalBenchmarkProgress(candidateIndex, candidateCount, candidate.label, completedCandidates, 0, 8);
            showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_recovery_retry.1d21", { device: candidate.label }), "pending");
            const recoveryPauseStartedAt = Date.now();
            await new Promise((resolve) => window.setTimeout(resolve, Math.min(900, 250 * attempt)));
            candidateStartupSpentMs += Date.now() - recoveryPauseStartedAt;
          }
        }
      }

      if (!completed) {
        if (startupRecoveryAttempts === 3 && startupRecoveryFailures === 3) {
          await invoke<string[]>("record_hardware_candidate_outcome", {
            device: candidate.device,
            successful: false,
          }).catch((historyError) => {
            showNotice(benchmarkMessage("msg.benchmark_history_save_failed.1d21", { device: candidate.label, detail: formatHardwareDeviceText(String(historyError)) }), "warning");
          });
        }
        startupTimedOut = startupTimedOut || candidateStartupSpentMs >= 90_000 || (timeoutCount > 0 && candidateStartupSpentMs >= 85_000);
        candidateFailures[candidate.device] = lastError || benchmarkMessage("msg.benchmark_candidate_incomplete.1d21");
        renderDigitalBenchmarkProgress(candidateIndex, candidateCount, candidate.label, completedCandidates, 8, 8);
        if (candidateOffset < candidateCount - 1) {
          showHardwareOptimizationMessage(benchmarkMessage("msg.benchmark_candidate_preserve_other.1d21", { device: candidate.label }), "warning");
          await waitForHardwareBackendIdle();
          continue;
        }
      }
      if (skipRemainingCandidates) break;
    }

    activeCandidateDeadline = Number.POSITIVE_INFINITY;
    updateBenchmarkCountdown();

    // Candidate profiles are independently persisted and validated by the
    // native finalizer. Do not discard a usable single-device or partial result
    // merely because the other candidate did not complete.
    if (completedCandidates === 0) {
  const candidateFailureDetail = [candidateFailures.gpu ? `GPU: ${candidateFailures.gpu}` : "", candidateFailures.cpu ? `CPU: ${candidateFailures.cpu}` : ""].filter(Boolean).join(" · ");
        throw new Error(benchmarkMessage("msg.benchmark_no_usable_candidate.1d20", { detail: candidateFailureDetail }));
    }

    renderDigitalBenchmarkProgress(candidateCount, candidateCount, benchmarkMessage("msg.benchmark_finalizing_results.1d20"), candidateCount, 8, 8);
    // Results transition already provides the finalization feedback.
    const comparison = await invoke<unknown>("finalize_hardware_optimization", {
      model: selectedModel,
      optimizationSessionId,
      selectedDevices: selectedDevicesForFinalizer,
    });
    const comparisonRecord = typeof comparison === "object" && comparison !== null
      ? comparison as Record<string, unknown> : null;
    const completeHeadToHead = renderHardwareComparison(comparison);
    benchmarkInviteResult.classList.add("benchmark-result-awaiting-finalization");

    // The comparison qualifies the recommendation; model loading remains tied to the user's choice.
    const optimizedSettingsReady = completeHeadToHead;
    benchmarkInviteIntroFirstLine.textContent = optimizedSettingsReady
    ? benchmarkMessage("msg.benchmark_intro_optimized.1d20")
    : benchmarkMessage("msg.benchmark_intro_results_ready.1d20");
    const provisionalSettingsReady = !completeHeadToHead
      && comparisonRecord?.optimizationEligible === true;
    // Backend retention is a lifecycle decision, not benchmark qualification.

    // Release the last candidate while the guarded benchmark lifecycle is
    // still active. A reused original server is verified, never stopped here.
    if (candidateStarted) {
      if (reusedOriginalBackend && !originalBackendStoppedForBenchmark) {
        const disposition = await verifyAndPreserveReusedOriginal(false);
        if (disposition === "unsafe") {
          throw new Error(benchmarkMessage("msg.benchmark_original_server_unverified.1d20"));
        }
      } else {
        await stopHardwareCandidateAndWait();
        candidateStarted = false;
        reusedOriginalBackend = false;
      }
    }

    stopBenchmarkCountdown();
    benchmarkProgressRemaining.textContent = benchmarkMessage("msg.benchmark_work_complete.1d20");
    await invoke("set_native_benchmark_starting", { enabled: false });
    startingGuard = false;
    // Restore only the exact server that was running before this benchmark.
    // This is lifecycle restoration, not winner warm-up or a new default start.
    if (originalBackendWasRunning && originalBackendStoppedForBenchmark
        && !originalBackendPreserved && !originalBackendRestored) {
      await restoreOriginalBackendIfNeeded();
    }
    if (source === "first-run" && !qaFirstRunSimulation) {
      completeFirstRunHardwareOptimization();
    }
    // Retain a candidate only when both its benchmark record and live backend
    // pass qualification. An unqualified partial result follows safe cleanup.
    keepWinnerBackendRunning = false;
    benchmarkInvitePanel.classList.add("has-optimizer-result");
    await transitionBenchmarkProgressToResults();
    benchmarkResultFinalStatus.textContent = optimizedSettingsReady
    ? benchmarkMessage("msg.benchmark_final_complete.1d20")
    : provisionalSettingsReady
    ? benchmarkMessage("msg.benchmark_final_provisional.1d20")
    : benchmarkMessage("msg.benchmark_final_unqualified.1d20");
    benchmarkInviteStatus.hidden = true;
    benchmarkInviteStatus.classList.add("hidden");

    benchmarkInviteApplyButton.hidden = !optimizedSettingsReady;
    benchmarkInviteApplyButton.disabled = !optimizedSettingsReady;
    benchmarkInviteApplyButton.textContent = benchmarkMessage("msg.benchmark_button_optimize.1d20");
    benchmarkInviteApplyButton.classList.toggle("primary", optimizedSettingsReady);
    benchmarkInviteApplyButton.classList.toggle("secondary", !optimizedSettingsReady);
    benchmarkInviteCustomButton.hidden = false;
    benchmarkInviteCustomButton.disabled = false;
    benchmarkInviteExportButton.hidden = false;

    benchmarkInviteCloseButton.hidden = optimizedSettingsReady;
    benchmarkInviteCloseButton.disabled = false;
    benchmarkInviteCloseButton.textContent = benchmarkMessage("msg.benchmark_button_default.1d20");
    benchmarkInviteCloseButton.classList.toggle("primary", !optimizedSettingsReady);
    benchmarkInviteCloseButton.classList.toggle("secondary", optimizedSettingsReady);

    resultChoice = await new Promise<HardwareOptimizationChoice>((resolve) => {
      resolveHardwareOptimizationChoice = resolve;
    });
    resolveHardwareOptimizationChoice = null;
    keepWinnerBackendRunning = false;
  } catch (error) {
    stopBenchmarkCountdown();
    benchmarkProgressRemaining.textContent = benchmarkMessage("msg.benchmark_work_stopped.1d20");
    if (candidateStarted && reusedOriginalBackend && !originalBackendStoppedForBenchmark) {
      const disposition = await verifyAndPreserveReusedOriginal();
      if (disposition === "unsafe") lifecycleUnsafe = true;
    } else if (candidateStarted) {
      keepWinnerBackendRunning = false;
      try {
        await stopHardwareCandidateAndWait();
        candidateStarted = false;
        reusedOriginalBackend = false;
      } catch (stopError) {
        lifecycleUnsafe = true;
        showNotice(benchmarkMessage("msg.benchmark_temp_process_stop_unconfirmed.1d20", { detail: String(stopError) }), "error");
      }
    } else if (!originalBackendPreserved) {
      keepWinnerBackendRunning = false;
    }

    let fallbackServerReady = originalBackendPreserved || originalBackendRestored;
    let fallbackPreparationError = "";

    // The normal Start Server command is blocked while optimization owns the
    // lifecycle. Release that reservation before restoration/default startup.
    if (startingGuard) {
      try {
        await invoke("set_native_benchmark_starting", { enabled: false });
        startingGuard = false;
      } catch (guardError) {
        lifecycleUnsafe = true;
        fallbackPreparationError = benchmarkMessage("msg.benchmark_reservation_release_failed.1d20", { detail: String(guardError) });
      }
    }

    if (!lifecycleUnsafe && originalBackendWasRunning) {
      if (!originalBackendPreserved && !originalBackendRestored
          && !originalBackendStoppedForBenchmark) {
        const disposition = await verifyAndPreserveReusedOriginal();
        if (disposition === "unsafe") lifecycleUnsafe = true;
      }
      if (!lifecycleUnsafe && originalBackendStoppedForBenchmark
          && !originalBackendPreserved && !originalBackendRestored) {
        await restoreOriginalBackendIfNeeded();
      }
      fallbackServerReady = originalBackendPreserved || originalBackendRestored;
    } else if (!lifecycleUnsafe && !originalBackendWasRunning) {
      // No server existed before this benchmark: leave the app stopped.
      try {
        const current = await invoke<BackendStatus>("get_backend_status");
        backend = current;
        if (current.running) {
          lifecycleUnsafe = true;
          fallbackPreparationError =
            benchmarkMessage("msg.benchmark_backend_active_after_cleanup.1d20");
        } else {
          fallbackServerReady = false;
        }
      } catch (statusError) {
        lifecycleUnsafe = true;
        fallbackPreparationError =
          benchmarkMessage("msg.benchmark_temp_backend_stop_unverified.1d20", { detail: String(statusError) });
      }
    }

    const errorText = formatHardwareDeviceText(String(error));
    const staleBundledBackend = /Benchmark capability check failed \(HTTP 404\)|Benchmark capability check failed \(404\)|Benchmark lease acquisition failed \(HTTP 404\)|missing the current benchmark safety API/i.test(errorText);
    const fallback = originalBackendPreserved || originalBackendRestored
      ? benchmarkMessage("msg.benchmark_fallback_original_restored.1d20")
      : fallbackServerReady
        ? benchmarkMessage("msg.benchmark_fallback_selected_model_running.1d20")
        : fallbackPreparationError
          ? benchmarkMessage("msg.benchmark_fallback_default_unverified.1d20", { detail: formatHardwareDeviceText(fallbackPreparationError) })
          : staleBundledBackend
            ? benchmarkMessage("msg.benchmark_fallback_stale_engine.1d20")
            : startupTimedOut
              ? benchmarkMessage("msg.benchmark_fallback_startup_timeout.1d20")
              : benchmarkMessage("msg.benchmark_fallback_recovery_failed.1d20");
    if (source === "first-run" && !qaFirstRunSimulation && !lifecycleUnsafe) {
      completeFirstRunHardwareOptimization();
    }
    benchmarkInviteErrorText.textContent = errorText.slice(0, 1600);
    benchmarkInviteErrorDetails.classList.remove("hidden");
    // A failed rerun must not erase a previously qualified per-model recommendation.
    // With no saved profile, Automatic is already the safe default.
    console.error("RDT Connect hardware optimization attempt failed:", error);
    showHardwareOptimizationMessage(fallback, "warning");
    benchmarkInviteResult.classList.add("hidden");
    benchmarkInviteApplyButton.hidden = true;
    benchmarkInviteCustomButton.hidden = true;
    benchmarkInviteExportButton.hidden = true;
    benchmarkInviteCloseButton.hidden = false;
    benchmarkInviteCloseButton.textContent = lifecycleUnsafe ? benchmarkMessage("msg.benchmark_button_close.1d20") : benchmarkMessage("msg.benchmark_button_default.1d20");
    benchmarkInviteCloseButton.classList.toggle("primary", !lifecycleUnsafe);
    benchmarkInviteCloseButton.classList.toggle("secondary", lifecycleUnsafe);
    await transitionBenchmarkProgressToResults();
    resultChoice = await new Promise<HardwareOptimizationChoice>((resolve) => {
      resolveHardwareOptimizationChoice = resolve;
    });
    resolveHardwareOptimizationChoice = null;
    showNotice(fallback, "warning");
  } finally {
    stopBenchmarkCountdown();
    if (candidateStarted && !keepWinnerBackendRunning) {
      if (reusedOriginalBackend && !originalBackendStoppedForBenchmark) {
        await verifyAndPreserveReusedOriginal();
      } else {
        try {
          await stopHardwareCandidateAndWait();
          candidateStarted = false;
          reusedOriginalBackend = false;
        } catch (stopError) {
          lifecycleUnsafe = true;
          showNotice(benchmarkMessage("msg.benchmark_temp_backend_reserved.1d20", { detail: String(stopError) }), "error");
        }
      }
    }
    if (startingGuard) {
      await invoke("set_native_benchmark_starting", { enabled: false }).catch((error) => {
        lifecycleUnsafe = true;
        showNotice(benchmarkMessage("msg.benchmark_lifecycle_reservation_unreleased.1d20", { detail: formatHardwareDeviceText(String(error)) }), "error");
      });
    }
    await restoreOriginalBackendIfNeeded();
    progressUnlisten?.();
    benchmarkRunInProgress = false;
    resolveHardwareOptimizationChoice = null;
    renderBackendStatus();
    renderInferenceHardwareStatus();
    benchmarkInvitePanel.classList.remove("has-optimizer-result");
    updateSavedBenchmarkAction();
  }

  if (lifecycleUnsafe) {
    hardwareOptimizationSource = null;
    throw new Error(benchmarkMessage("msg.benchmark_lifecycle_release_failed.1d20"));
  }

  if (source === "first-run" && !qaFirstRunSimulation) {
    completeFirstRunHardwareOptimization();
  }

  if (source !== "first-run" && resultChoice !== "manual"
      && !originalBackendRestored && !originalBackendPreserved
      && !lifecycleUnsafe) {
    try {
      if (!(backend.running && backend.model === selectedModel)) {
        showNotice(benchmarkMessage("msg.benchmark_start_saved_preference.1d20"), "info");
        backend = await invoke<BackendStatus>("start_backend", { model: selectedModel, preferredPort: selectedPort });
        renderBackendStatus();
        await refreshConnectionStatus();
        await refreshRemoteStatus();
      }
      showNotice(resultChoice === "optimized"
        ? benchmarkMessage("msg.benchmark_optimized_server_ready.1d20")
        : benchmarkMessage("msg.benchmark_server_ready_saved_preference.1d20"), "good");
    } catch (error) {
      showNotice(benchmarkMessage("msg.benchmark_finished_restart_failed.1d20", { detail: formatHardwareDeviceText(String(error)) }), "error");
    }
  }
  hardwareOptimizationSource = null;
  return resultChoice;
}

// The first Start click runs the comparison only after the selected model and trusted HTTPS are ready.
window.addEventListener("rdt-connect-open-benchmark-invite", () => {
  openManualHardwareSelector();
});
document.getElementById("open-native-benchmark-button")?.addEventListener("click", () => {
  if (developerOptionsEnabled) window.dispatchEvent(new Event("rdt-connect-open-benchmark-invite"));
});
/* RDT_HARDWARE_OPTIMIZATION_END */

// HANDOFF: Hardware-selection safeguard
(function(){
  function updateHw(){
    const sel = document.querySelector('[data-hardware]') as HTMLSelectElement;
    if(!sel) return;
    // @ts-ignore
    const running = (window as any).__serverRunning === true || document.querySelector('[data-running="true"]') !== null;
    sel.disabled = running;
    sel.title = running ? "Stop the server first to change hardware selection." : "";
    sel.style.cursor = running ? "not-allowed" : "";
  }
  setInterval(updateHw, 1000);
})();

}

void bootstrapRdtConnect().catch((error: unknown) => {
  console.error("RDT Connect application initialization failed.", error);
});
