import { invoke } from "@tauri-apps/api/core";
import { emit, listen } from "@tauri-apps/api/event";
import { check as checkForTauriUpdate, type Update as TauriUpdate, type DownloadEvent as TauriDownloadEvent } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import "./style.css";
import { initializeLocalization, getActiveLocale, getLocaleChoices, setActiveLocale, translateKey } from "./i18n";

// Select a saved language or the system-preferred supported language, then translate the complete UI.
initializeLocalization();

type BackendStatus = {
  running: boolean;
  pid: number | null;
  port: number | null;
  model: string | null;
  localUrl: string | null;
  logPath: string;
};
type OrphanBackend = { pid: number; port: number | null; description: string };
type OrphanScan = { managerLockedElsewhere: boolean; instances: OrphanBackend[] };
type BackendConnectionCheck = { running: boolean; pid: number | null; port: number | null; localUrl: string | null; healthOk: boolean; authOk: boolean | null; authHttpStatus: number | null; message: string };
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
type ModelStorageStatus = { model: string; locations: string[]; locationsAvailability: string[]; activeRoot: string; modelCached: boolean; serverRunning: boolean };
type ModelDownloadStatus = { active: boolean; model: string | null; destinationRoot: string | null; phase: string; message: string; progressPercent: number; downloadedBytes: number; totalBytes: number | null; plannedFiles: number | null; sizeIsEstimate: boolean };
type ModelCacheDeleteResult = { deletedRepositories: number; locationsChecked: number; approximateBytesFreed: number; message: string };
type AppBuildInfo = { version: string; buildId: string };

const $ = <T extends HTMLElement>(id: string) => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`Missing UI element: ${id}`);
  return el as T;
};

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
  try { localStorage.setItem(OFFLINE_ONLY_MODE_KEY, String(offlineOnlyModeEnabled)); } catch { /* optional preference */ }
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
    if (autoCheckUpdatesToggle.checked) void checkForApplicationUpdates();
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
}
function setDeveloperOptions(enabled: boolean) {
  developerOptionsEnabled = enabled;
  syncDeveloperVisibility();
  if (developerOptionsToggle) developerOptionsToggle.checked = enabled;
  syncDeveloperToggleButton();
  try { localStorage.setItem("rdt_connect_developer_options", String(enabled)); } catch { /* optional preference */ }
  localPortTools.classList.toggle("hidden", !enabled);
  if (localServerDetails) localServerDetails.open = enabled;
  if (!enabled && document.getElementById("view-developer")?.classList.contains("hidden") === false) setAppView("local");
  // Keep the model panel in sync immediately when Developer Options changes.
  renderModelDownloadUI();
}
for (const tab of viewTabs) tab.addEventListener("click", () => {
  const target = tab.dataset.viewTarget || "local";
  if (target === "developer" && !developerOptionsEnabled) {
    showNotice(translateKey("msg.enable_dev_to_open_developer_options.9f67a54e", {}, getActiveLocale()), "info");
    return;
  }
  setAppView(target);
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
  const languageName = getLocaleChoices().find((language) => language.code === active)?.languageName ?? "English";
  appLanguageSelect.value = active;
  appLanguageSelect.setAttribute("aria-label", translateKey("msg.interface_language.8027f432", {}, active));
  appLanguageSelect.setAttribute("title", translateKey("msg.choose_the_language_used_throughout_rdt_conn.81f7adea", {}, active));
  appLanguageStatus.textContent = translateKey("settings.language.activeStatus", { languageName }, active);
}
updateAppLanguageStatus();
updateRdtVoiceHomeLinkLocale();
// Keep native tray labels in the same language as the window; no error means the UI can still work offline.
void invoke("set_app_language", { locale: getActiveLocale() }).catch(() => undefined);
appLanguageSelect.addEventListener("change", () => {
  const next = setActiveLocale(appLanguageSelect.value, true);
  updateAppLanguageStatus();
  updateRdtVoiceHomeLinkLocale();
  void invoke("set_app_language", { locale: next }).catch(() => {
    // The web UI preference is still saved; Rust tray labels will refresh on the next launch.
  });
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
const settingsLockHint = document.querySelector<HTMLElement>("#settings-lock-hint");
const localAddress = $("local-address");
const preferredPortInput = $("preferred-port") as HTMLInputElement;
const localPortTools = $("local-port-tools");
localPortTools.classList.toggle("hidden", !developerOptionsEnabled);
if (localServerDetails) localServerDetails.open = developerOptionsEnabled;
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
const modelMenuDetails: Record<string, { name: string; description: string; size: string; recommended?: boolean }> = {
  "mlx-community/whisper-tiny-mlx": { name: "Tiny", description: "Fastest · smallest download", size: "~74 MB" },
  "mlx-community/whisper-base-mlx": { name: "Base", description: "Lightweight choice", size: "~144 MB" },
  "mlx-community/whisper-small-mlx": { name: "Small", description: "Middle-size model", size: "~481 MB" },
  "mlx-community/whisper-large-v3-turbo": { name: "Large v3 Turbo", description: "Strong balance of speed and quality", size: "~1.61 GB", recommended: true },
  "mlx-community/whisper-large-v3-mlx": { name: "Large v3", description: "Largest download in this list", size: "~3.08 GB" },
};
function modelMenuInfo(option: HTMLOptionElement) {
  return modelMenuDetails[option.value] || { name: option.textContent || option.value, description: "Whisper model", size: "Size varies" };
}
function modelToneFor(model: string): "tiny" | "base" | "small" | "turbo" | "large" {
  if (model === "tiny" || model.endsWith("whisper-tiny-mlx")) return "tiny";
  if (model === "base" || model.endsWith("whisper-base-mlx")) return "base";
  if (model === "small" || model.endsWith("whisper-small-mlx")) return "small";
  if (model === "large-v3-turbo" || model.endsWith("whisper-large-v3-turbo")) return "turbo";
  return "large";
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
    if (label) label.textContent = isNativeGgmlCatalog() ? "Choose a model — GGML (CPU baseline):" : translateKey("msg.choose_a_model_apple_mlx.248ca57c", {}, getActiveLocale());
    modelIdDetail.textContent = modelSelect.value;
    modelSelectCurrentDetail.textContent = `${modelMenuInfo(modelSelect.options[modelSelect.selectedIndex]).size}${modelMenuInfo(modelSelect.options[modelSelect.selectedIndex]).recommended ? " · Recommended" : ""}`;
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
    modelSelectCurrentDetail.textContent = `${info.size}${info.recommended ? " · Recommended" : ""}`;
    modelSelectCurrentBadge.hidden = selected.dataset.cacheState !== "downloaded";
  }
  modelSelectTrigger.disabled = modelSelect.disabled;
  modelSelectTrigger.title = modelSelect.title || translateKey("msg.choose_a_transcription_model.569c22c8", {}, getActiveLocale());
  for (const item of Array.from(modelSelectMenu.querySelectorAll<HTMLButtonElement>(".model-select-option"))) {
    item.disabled = modelSelect.disabled;
    const selected = item.dataset.modelValue === modelSelect.value;
    item.setAttribute("aria-selected", String(selected));
    item.classList.toggle("selected", selected);
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
    empty.textContent = unknown
      ? "Cache status is still being checked or could not be verified. Reopen Settings to retry."
      : "No downloaded RDT Connect models were found in your configured storage locations.";
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
let modelBadgeRefreshSerial = 0;
async function refreshModelCacheBadges() {
  const serial = ++modelBadgeRefreshSerial;
  const options = Array.from(modelSelect.options);
  const results = await Promise.all(options.map(async (option) => {
    try {
      const status = await invoke<ModelStorageStatus>("get_model_storage_status", { model: option.value });
      return { value: option.value, cached: Boolean(status.modelCached) };
    } catch {
      return { value: option.value, cached: null as boolean | null };
    }
  }));
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
const aboutInstallationGuideButton = $("about-installation-guide-button") as HTMLButtonElement;
aboutInstallationGuideButton.hidden = !/Windows/i.test(navigator.userAgent);
const aboutBusinessContactLink = $("about-business-contact-link") as HTMLAnchorElement;
const openButton = $("open-button") as HTMLButtonElement;
const browserSelect = $("browser-select") as HTMLSelectElement;
const footerServerStatusDot = $("footer-server-status-dot");
const headerServerStatusDot = $("header-server-status-dot");
const setupRemoteButton = $("setup-remote") as HTMLButtonElement;
const refreshRemoteButton = $("refresh-remote") as HTMLButtonElement;
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
    showNotice(translateKey("msg.could_not_copy_the_backend_log_error.78131054", { error: String(error) }, getActiveLocale()), "error");
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
        const reason = String(error).replace(/(authorization:\s*bearer\s+|\bbearer\s+)[^\s,;]+/ig, "$1[REDACTED]").replace(/\/(?:Users|home)\/[^/\s]+/g, "~");
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
    showNotice(translateKey("msg.diagnostics_could_not_complete_redacted.14a83a85", { error: String(error).replace(/\bBearer\s+[^\s,;]+/ig, "Bearer [REDACTED]") }, getActiveLocale()), "error");
  } finally {
    diagnosticButtons.forEach((button) => { button.disabled = false; });
  }
}
runDiagnosticsButton.addEventListener("click", () => void loadDiagnostics(false));
copyDiagnosticsButton.addEventListener("click", () => void loadDiagnostics(true));
remoteRunDiagnosticsButton.addEventListener("click", () => void loadDiagnostics(false, "remote"));
remoteCopyDiagnosticsButton.addEventListener("click", () => void loadDiagnostics(true, "remote"));
copyActivityButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(sanitizedActivityText());
    showButtonFeedback(copyActivityButton);
  } catch (error) { showNotice(translateKey("msg.could_not_copy_activity_error.9bc99bf3", { error: String(error) }, getActiveLocale()), "error"); }
});

copyEndpointButton.addEventListener("click", async () => {
  if (!backend.localUrl) return;
  try {
    await navigator.clipboard.writeText(backend.localUrl);
    showButtonFeedback(copyEndpointButton);
  } catch (error) { showNotice(translateKey("msg.could_not_copy_address_error.5ef57c80", { error: String(error) }, getActiveLocale()), "error"); }
});
copyRemoteUrlButton.addEventListener("click", async () => {
  const url = remoteUrlValue.textContent?.trim() || "";
  if (!url || !/^https:\/\//i.test(url)) return;
  try {
    await navigator.clipboard.writeText(url);
    showButtonFeedback(copyRemoteUrlButton, translateKey("msg.copied.8d525e5f", {}, getActiveLocale()));
    showNotice(translateKey("msg.private_https_url_copied_it_is_only_reachabl.56e52882", {}, getActiveLocale()), "good");
  } catch (error) { showNotice(translateKey("msg.could_not_copy_the_private_https_url_error.7b31d0ea", { error: String(error) }, getActiveLocale()), "error"); }
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
    showNotice(translateKey("msg.could_not_reveal_the_access_token_error.12941737", { error: String(error) }, getActiveLocale()), "error");
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
    showNotice(translateKey("msg.could_not_copy_the_access_token_error.9674c325", { error: String(error) }, getActiveLocale()), "error");
  } finally { copyTokenButton.disabled = false; }
});

openLogButton.addEventListener("click", async () => {
  openLogButton.disabled = true;
  try {
    await invoke("open_backend_log");
    showButtonFeedback(openLogButton, "Opened");
  } catch (error) {
    showNotice(translateKey("msg.couldn_t_open_the_backend_log_error.52d916e5", { error: String(error) }, getActiveLocale()), "error");
  } finally {
    openLogButton.disabled = false;
  }
});

let backend: BackendStatus = { running: false, pid: null, port: null, model: null, localUrl: null, logPath: "" };
let lastConnectionCheck: BackendConnectionCheck | null = null;
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
  try { localStorage.setItem("rdt_connect_preferred_browser", preferredBrowserId); } catch { /* optional preference */ }
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
    browserSelect.value = options.some((option) => option.id === preferredBrowserId) ? preferredBrowserId : "default";
    if (browserSelect.value !== preferredBrowserId) {
      preferredBrowserId = "default";
      try { localStorage.setItem("rdt_connect_preferred_browser", "default"); } catch { /* optional preference */ }
    }
  } catch {
    // Keep the static System Default Browser option if native discovery is unavailable.
    browserSelect.title = translateKey("msg.browser_discovery_is_unavailable_the_system.ca8d09f1", {}, getActiveLocale());
  }
}
void loadAvailableBrowsers();

function renderFooterServerStatusDot() {
  const state = serverOperation === "starting" || serverOperation === "stopping"
    ? "processing"
    : backend.running ? "running" : "stopped";
  footerServerStatusDot.className = `foot-dot ${state}`;
  const label = state === "running" ? "Server running" : state === "processing" ? "Server starting or stopping" : "Server stopped";
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
  if (!backend.running) return "The local server is stopped. Start it before opening RDT Voice.";
  const check = lastConnectionCheck;
  if (!check || !check.running || check.pid !== backend.pid || check.port !== backend.port)
    return "The current server has not passed its health and authentication check yet. Open RDT Voice remains disabled.";
  if (!check.healthOk)
    return `The local health check failed. Open RDT Voice remains disabled. ${check.message || "Check the local server status and retry."}`;
  if (check.authOk !== true) {
    if (check.authHttpStatus === 401)
      return "RDT Connect's local API token was rejected (HTTP 401). RDT Voice was not opened; fix the server authentication/token state and retry.";
    if (check.authHttpStatus === 403)
      return "The local server denied the authenticated API request (HTTP 403). RDT Voice was not opened; check the server access policy and retry.";
    return `The authenticated API check did not pass. RDT Voice remains disabled. ${check.message || "Retry the connection test."}`;
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
  if (kind === "error") return "Something needs attention. Enable Developer Options for details.";
  if (/started on|backend is ready|server started/i.test(message)) return "Server started.";
  if (/stopped and verified|server stopped|previous server stopped/i.test(message)) return "Server stopped.";
  if (/connection.*(pass|ready|healthy)|authenticated API/i.test(message)) return "Connection checked.";
  if (/could not inspect|diagnostics/i.test(message)) return "Check complete. Details are available in Developer Options.";
  if (message.length > 115) return message.slice(0, 112).trimEnd() + "…";
  return message;
}
function showNotice(message: string, kind: "info" | "good" | "error" | "warning" = "info") {
  notice.textContent = conciseNotice(message, kind);
  notice.className = `notice ${kind}`;
  notice.setAttribute("data-state", kind);
  const detail = document.getElementById("developer-latest-detail");
  if (detail) detail.textContent = message;
  activityEntries.push({ at: Date.now(), kind, message });
  if (activityEntries.length > 500) activityEntries.splice(0, activityEntries.length - 500);
}

void listen<string>("rdt-backend-state-changed", (event) => {
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
void listen<string>("rdt-tray-start-failed", (event) => {
  setAppView("local");
  showNotice(event.payload, "error");
});
void listen<string>("rdt-tray-open-voice-failed", (event) => {
  setAppView("local");
  showNotice(event.payload, "error");
});
void listen<string>("rdt-tray-model-needs-download", (event) => {
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
void listen<ModelDownloadStatus>("rdt-model-download-status", (event) => {
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

void listen<string>("rdt-connect-stop-failed", (event) => {
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
  modelSelect.disabled = value || modelDownloadStatus.active || platformModelCatalogFailed;
  modelSelect.title = backend.running
    ? "Browse models while running; select another model and choose Switch Model to restart safely."
    : (isNativeGgmlCatalog() ? "GGML models use the bundled whisper.cpp CPU worker. GPU use is not implied." : "All choices use the inference backend supported by this device.");
  modelSelectTooltip.setAttribute("title", modelSelect.title);
  modelSelectTooltip.setAttribute("data-tooltip", modelSelect.title);
  syncModelSelectPresentation();
  modelHelp.textContent = isNativeGgmlCatalog()
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
  setupRemoteButton.disabled = value || !backend.running;
  refreshRemoteButton.disabled = value || !backend.running;
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
  if (!backend.running) { lastConnectionCheck = null; renderConnectionStatus(); return; }
  lastConnectionCheck = null;
  openButton.disabled = true;
  testConnectionButton.disabled = true;
  connectionStatus.textContent = translateKey("msg.checking_local_health_and_rdt_connect_s_auth.f8e14e85", {}, getActiveLocale());
  connectionStatus.className = "connection-status pending";
  connectionSummary.textContent = translateKey("msg.checking_connection.de9fc337", {}, getActiveLocale());
  try {
    lastConnectionCheck = await invoke<BackendConnectionCheck>("get_backend_connection_status");
    renderConnectionStatus();
  } catch (error) {
    lastConnectionCheck = null;
    connectionStatus.textContent = translateKey("msg.connection_check_failed_error_this_result_re.43825799", { error: String(error) }, getActiveLocale());
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
    localHttpsStatusEl.textContent = translateKey("msg.could_not_verify_local_https_status_error.a81f2529", { error: String(error) }, getActiveLocale());
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


    if (showErrors) showNotice(translateKey("msg.could_not_read_https_settings_error.8e18eb97", { error: String(error) }, getActiveLocale()), "error");
    return null;
  }
}

let storageLocationDraft: string[] = [];
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
  if (!target || typeof target.getBoundingClientRect !== "function") return;

  const rect = target.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return;

  const viewportWidth = Math.max(
    window.innerWidth || 0,
    document.documentElement.clientWidth || 0,
    1
  );
  const viewportHeight = Math.max(
    window.innerHeight || 0,
    document.documentElement.clientHeight || 0,
    1
  );

  const centerX = Math.max(
    0,
    Math.min(viewportWidth, rect.left + rect.width / 2)
  );
  const centerY = Math.max(
    0,
    Math.min(viewportHeight, rect.top + rect.height / 2)
  );

  // The mask is fully clear through 62% of each radius.
  // Add a comfortable margin around the highlighted control.
  const radiusX = Math.max(110, (rect.width / 2 + 22) / 0.70);
  const radiusY = Math.max(48, (rect.height / 2 + 18) / 0.70);

  // Set coordinates on the overlay itself, not on the button or body.
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
  return JSON.stringify(storageLocationDraft) !== JSON.stringify(savedStorageLocations);
}

function renderModelStorageSettings() {
  modelStorageLocationsEl.replaceChildren();
  storageLocationDraft.forEach((path, index) => {
    const row = document.createElement("div"); row.className = "model-storage-row";
    const priority = document.createElement("div"); priority.className = "model-storage-priority"; priority.textContent = `#${index + 1}`;
    const pathWrap = document.createElement("div"); pathWrap.className = "model-storage-path-wrap";
    const pathText = document.createElement("code"); pathText.className = "model-storage-path"; pathText.textContent = path; pathText.title = path;
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
    pathWrap.append(pathText, availability);
    const actions = document.createElement("div"); actions.className = "model-storage-row-actions";
    const up = document.createElement("button"); up.className = "mini-button"; up.type = "button"; up.textContent = "↑"; up.dataset.baseTitle = translateKey("msg.move_this_location_earlier_in_priority.aea044f6", {}, getActiveLocale()); up.title = stopServerSettingTitle(up.dataset.baseTitle); up.setAttribute("aria-label", translateKey("msg.move_location_index_earlier.63908921", { index: index + 1 }, getActiveLocale())); up.disabled = index === 0 || backend.running || modelStorageBusy;
    up.addEventListener("click", () => { [storageLocationDraft[index - 1], storageLocationDraft[index]] = [storageLocationDraft[index], storageLocationDraft[index - 1]]; renderModelStorageSettings(); });
    const down = document.createElement("button"); down.className = "mini-button"; down.type = "button"; down.textContent = "↓"; down.dataset.baseTitle = translateKey("msg.move_this_location_later_in_priority.7555aea5", {}, getActiveLocale()); down.title = stopServerSettingTitle(down.dataset.baseTitle); down.setAttribute("aria-label", translateKey("msg.move_location_index_later.a9e022f9", { index: index + 1 }, getActiveLocale())); down.disabled = index === storageLocationDraft.length - 1 || backend.running || modelStorageBusy;
    down.addEventListener("click", () => { [storageLocationDraft[index], storageLocationDraft[index + 1]] = [storageLocationDraft[index + 1], storageLocationDraft[index]]; renderModelStorageSettings(); });
    const remove = document.createElement("button"); remove.className = "mini-button deny"; remove.type = "button"; remove.textContent = translateKey("msg.remove.c3812fc4", {}, getActiveLocale()); remove.dataset.baseTitle = translateKey("msg.remove_this_model_storage_folder_from_the_li.c967032d", {}, getActiveLocale()); remove.title = stopServerSettingTitle(remove.dataset.baseTitle); remove.disabled = storageLocationDraft.length <= 1 || backend.running || modelStorageBusy;
    remove.addEventListener("click", () => { storageLocationDraft.splice(index, 1); renderModelStorageSettings(); });
    if (storageLocationDraft.length <= 1) { up.hidden = true; down.hidden = true; }
    actions.append(up, down, remove); row.append(priority, pathWrap, actions); modelStorageLocationsEl.appendChild(row);
  });
  modelStorageAddButton.title = translateKey("msg.add_a_model_storage_folder.d01586c6", {}, getActiveLocale());
  modelStorageSaveButton.title = stopServerSettingTitle(translateKey("msg.save_the_storage_folder_priority_for_future.31018b39", {}, getActiveLocale()));
  modelStorageAddButton.disabled = modelStorageBusy || storageLocationDraft.length >= 20;
  modelStorageSaveButton.disabled = backend.running || modelStorageBusy || !modelStorageDraftIsDirty() || storageLocationDraft.length < 1 || storageLocationDraft.length > 20;
  modelStorageSaveButton.textContent = modelStorageBusy ? translateKey("msg.saving.23e39291", {}, getActiveLocale()) : translateKey("msg.save_storage_priority.c84a39d5", {}, getActiveLocale());
}

function renderModelStorageSummary(status: ModelStorageStatus) {
  if (status.modelCached) {
    modelStorageSummary.textContent = translateKey("msg.verified_local_model_copy_found_rdt_connect.af333d61", { activeRoot: status.activeRoot, model: status.model }, getActiveLocale());
    modelStorageSummary.className = "tls-status tls-good";
  } else {
    modelStorageSummary.textContent = translateKey("msg.no_verified_local_snapshot_found_for_model_a.ecf84dd0", { model: status.model, activeRoot: status.activeRoot }, getActiveLocale());
    modelStorageSummary.className = "tls-status tls-warning";
  }
  if (backend.running) modelStorageSummary.textContent += " Stop the server before editing storage locations.";
}

async function refreshModelStorageStatus(showErrors = false) {
  try {
    const status = await invoke<ModelStorageStatus>("get_model_storage_status", { model: modelSelect.value });
    modelStorageStatus = status;
    if (!modelStorageDraftIsDirty()) {
      storageLocationDraft = [...status.locations];
      savedStorageLocations = [...status.locations];
    }
    renderModelStorageSummary(status);
    renderModelStorageSettings();
    renderModelDownloadUI();
    scheduleVisualSetupGuideUpdate();
    renderModelCleanupButtons();
  } catch (error) {
    modelStorageSummary.textContent = translateKey("msg.could_not_read_model_storage_settings_error.c837f48b", { error: String(error) }, getActiveLocale());
    modelStorageSummary.className = "tls-status tls-error";
    if (showErrors) showNotice(translateKey("msg.could_not_read_model_storage_settings_error.c837f48b", { error: String(error) }, getActiveLocale()), "error");
    renderModelStorageSettings();
    renderModelDownloadUI();
    scheduleVisualSetupGuideUpdate();
    renderModelCleanupButtons();
  }
}

function friendlyModelName(model: string): string {
  const names: Record<string, string> = {
    "mlx-community/whisper-large-v3-turbo": "Whisper Large v3 Turbo",
    "mlx-community/whisper-large-v3-mlx": "Whisper Large v3",
    "mlx-community/whisper-small-mlx": "Whisper Small",
    "mlx-community/whisper-base-mlx": "Whisper Base",
    "mlx-community/whisper-tiny-mlx": "Whisper Tiny",
    "tiny": "Whisper Tiny",
    "base": "Whisper Base",
    "small": "Whisper Small",
    "large-v3-turbo": "Whisper Large v3 Turbo",
    "large-v3": "Whisper Large v3",
  };
  return names[model] || model.split("/").pop() || "Whisper model";
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
  const settingsHint = "Change this path in Settings → Model storage.";
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
    modelDownloadStatusEl.textContent = `${download?.message || translateKey("msg.the_download_did_not_finish.2450f84a", {}, getActiveLocale())}${active ? " The downloader may still be stopping; retry Cancel if needed." : " Completed files remain available for retry."}`;
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
      : "Checking whether a local copy is available…";
  }
  if (backend.running && !backend.model) {
    modelDownloadStatusEl.textContent = translateKey("msg.the_running_server_s_active_model_could_not_be_verified_download.cd60bf94", {}, getActiveLocale());
  } else if (backend.running && backend.model === selectedModel) {
    modelDownloadStatusEl.textContent = translateKey("msg.this_model_is_running_select_a_different_model_to_download_anoth.812491f9", {}, getActiveLocale());
  }
  modelDownloadStatusEl.classList.toggle("error-text", failed);
  modelDownloadButton.hidden = Boolean(cached || active);
  modelDownloadButton.disabled = runningModelCacheProtected || active || modelDownloadStatus.phase === "canceling" || !modelStorageStatus;
  modelDownloadButton.textContent = failed || cancelled ? translateKey("msg.retry_download.9e90b868", {}, getActiveLocale()) : `Download model · ${approximateModelSizeLabel(selectedModel)}`;
  // Keep Cancel visible during cancellation so the user can see that work is in progress.
  // If cancellation cannot be verified, the button becomes a retry action rather than disappearing.
  modelDownloadCancelButton.classList.toggle("hidden", !active);
  modelDownloadCancelButton.disabled = !active || phase === "canceling";
  modelDownloadCancelButton.textContent = phase === "canceling" ? translateKey("msg.stopping.bbe85741", {}, getActiveLocale()) : (failed && active ? "Try cancel again" : translateKey("msg.cancel.19766ed6", {}, getActiveLocale()));
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
    modelDownloadProgressPhase.textContent = phase === "checking" ? "Checking model size…" : phase === "canceling" ? "Stopping download workers…" : phase === "verifying" ? translateKey("msg.verifying_model_files.992d6123", {}, getActiveLocale()) : verifiedComplete ? "Model ready" : failed ? "Download stopped" : cancelled ? translateKey("msg.download_canceled.1f39d555", {}, getActiveLocale()) : "Downloading model files…";
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
    modelDownloadStatusEl.textContent = translateKey("msg.could_not_read_download_status_error.1ab50fe8", { error: String(error) }, getActiveLocale());
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
    ? "The running server's active model could not be verified, so cache deletion is paused for safety."
    : downloadActive ? "Finish or cancel the active model download before deleting model caches."
    : modelStorageBusy ? "Finish saving model-storage changes before deleting model caches."
    : selected.length > 0 ? `${selected.length} inactive model cache${selected.length === 1 ? " is" : "s are"} selected for confirmation.${backend.running ? " The running model is protected." : ""}`
    : backend.running ? "Select inactive downloaded model caches. The running model is protected and cannot be selected."
    : "Select one or more downloaded model caches above. Nothing is preselected.";
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
    acceptModelDownloadStatus({ ...modelDownloadStatus, active: false, phase: "failed", message: String(error) });
    renderModelDownloadUI();
    showNotice(translateKey("msg.could_not_download_the_model_error.a226b9d2", { error: String(error) }, getActiveLocale()), "error");
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
      message: translateKey("msg.cancel_could_not_be_confirmed_error_select_t.e38eb04c", { error: String(error) }, getActiveLocale()),
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
    modelCleanupStatus.textContent = translateKey("msg.could_not_delete_selected_model_caches_arg1.7dfa9acc", { arg1: String(error) }, getActiveLocale());
    showNotice(translateKey("msg.could_not_delete_selected_model_caches_arg1.7dfa9acc", { arg1: String(error) }, getActiveLocale()), "error");
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
    renderModelStorageSettings();
    showNotice(translateKey("msg.folder_added_to_the_draft_list_save_storage.c0819cab", {}, getActiveLocale()), "info");
  } catch (error) { showNotice(translateKey("msg.could_not_choose_a_model_storage_folder_erro.0f408147", { error: String(error) }, getActiveLocale()), "error"); }
  finally { modelStorageBusy = false; renderModelStorageSettings(); }
});

modelStorageSaveButton.addEventListener("click", async () => {
  if (backend.running) { showNotice(translateKey("msg.stop_the_local_server_before_saving_model_st.1949e9c3", {}, getActiveLocale()), "warning"); return; }
  modelStorageBusy = true; renderModelStorageSettings();
  try {
    const status = await invoke<ModelStorageStatus>("set_model_storage_locations", { model: modelSelect.value, locations: storageLocationDraft });
    modelStorageStatus = status;
    storageLocationDraft = [...status.locations];
    savedStorageLocations = [...status.locations];
    renderModelStorageSummary(status); renderModelStorageSettings();
    void refreshModelCacheBadges();
    showNotice(translateKey("msg.model_storage_locations_saved_the_next_serve.abe7ee05", {}, getActiveLocale()), "good");
  } catch (error) {
    showNotice(translateKey("msg.could_not_save_model_storage_settings_error.3a4cc317", { error: String(error) }, getActiveLocale()), "error");
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
  } catch (error) { showNotice(translateKey("msg.could_not_choose_that_file_error.b4bb7e62", { error: String(error) }, getActiveLocale()), "error"); }
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
  } catch (error) { showNotice(translateKey("msg.could_not_link_that_certificate_error.f99f0898", { error: String(error) }, getActiveLocale()), "error"); }
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
  } catch (error) { showNotice(translateKey("msg.could_not_trust_that_ca_error.ea84346e", { error: String(error) }, getActiveLocale()), "error"); }
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
  } catch (error) { showNotice(translateKey("msg.could_not_remove_every_tracked_trust_entry_e.d78b5b48", { error: String(error) }, getActiveLocale()), "error"); }
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
  } catch (error) { showNotice(translateKey("msg.could_not_change_local_transport_error.6c187215", { error: String(error) }, getActiveLocale()), "error"); await refreshLocalHttpsStatus(); }
}
transportHttpsInput.addEventListener("change", () => { if (transportHttpsInput.checked) void changeLocalTransport("https"); });
transportHttpInput.addEventListener("change", () => { if (transportHttpInput.checked) void changeLocalTransport("http"); });

function renderBackendStatus() {
  const operationLabel = serverOperation === "starting" ? "STARTING…" : serverOperation === "stopping" ? "STOPPING…" : null;
  statusPill.textContent = operationLabel || (backend.running ? translateKey("msg.running_value.5f2c3539", { value: backend.port || Number(preferredPortInput.value) || 8888 }, getActiveLocale()) : translateKey("msg.stopped.133c4945", {}, getActiveLocale()));
  statusPill.className = `status-pill ${serverOperation ? "processing" : backend.running ? "running" : "stopped"}`;
  statusDetail.textContent = serverOperation === "starting" ? translateKey("msg.starting_the_local_transcription_server_and.f4aff6a0", {}, getActiveLocale()) : serverOperation === "stopping" ? translateKey("msg.stopping_the_local_transcription_server_and.041ff878", {}, getActiveLocale()) : backend.running ? translateKey("msg.local_transcription_server_is_running.559685bc", {}, getActiveLocale()) : translateKey("msg.local_transcription_server_is_stopped.7ef01073", {}, getActiveLocale());
  startButtonLabel.textContent = serverOperation === "starting" ? "Starting…" : serverOperation === "stopping" ? translateKey("msg.stopping.bbe85741", {}, getActiveLocale()) : backend.running ? "Stop Server" : "Start Server";
  startButton.classList.toggle("server-running", backend.running);
  startButton.classList.toggle("is-idle-start", startupReady && !busy && !backend.running && serverOperation === null && !conflictingBackendPresent);
  startButton.parentElement?.classList.toggle("is-running", backend.running);
  syncModelSwitchControl();
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
          orphanDetail.textContent = translateKey("msg.could_not_automatically_stop_verified_proces.4acc1f2f", { pid: instance.pid, error: String(error) }, getActiveLocale());
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
    orphanDetail.textContent = translateKey("msg.could_not_verify_local_server_state_error.19b913a5", { error: String(error) }, getActiveLocale());
    conflictingBackendPresent = true;
    orphanPanel.classList.add("hidden");
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
    : (tailscale.message || (backend.running ? "Check Tailscale to configure private HTTPS access." : "Start the local server first."));
  remoteState.className = `remote-state ${privateUrlReady ? "hidden" : ""}`;
  setupRemoteButton.textContent = tailscale.ready ? translateKey("msg.refresh_private_access.978c0605", {}, getActiveLocale()) : translateKey("msg.set_up_remote_access.aab18c87", {}, getActiveLocale());
  remoteActionHelp.textContent = !backend.running
    ? "Start the local server first. This does not enable public access."
    : !tailscale.ready
      ? "Make sure Tailscale is installed and connected before generating a QR."
      : !pairingQr
        ? "Copy the private URL for manual setup, or generate a short-lived QR for pairing."
        : "The pairing QR is single-use, expires automatically, and still requires your approval.";
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
  const selectedCache = preverifiedCache ?? await invoke<ModelStorageStatus>("get_model_storage_status", { model: modelSelect.value });
  modelStorageStatus = selectedCache;
  renderModelStorageSummary(selectedCache);
  renderModelDownloadUI();
  if (!selectedCache.modelCached) {
    nudgeModelDownloadButton();
    return;
  }
  await runAction(async () => {
    backend = await invoke<BackendStatus>("start_backend", { model: modelSelect.value, preferredPort: Number(preferredPortInput.value) || 8888 });
    renderBackendStatus();
    await refreshConnectionStatus();
    if (browserPreflightPassed()) {
      showNotice(translateKey("msg.rdt_connect_started_on_localurl_server_and_a.e4c94cb9", { localUrl: backend.localUrl ?? "" }, getActiveLocale()), "good");
    } else {
      showNotice(translateKey("msg.the_server_started_but_open_rdt_voice_is_dis.086dfa50", { browserPreflightFailure: browserPreflightFailure() }, getActiveLocale()), "warning");
    }
    await refreshRemoteStatus();
  });
}

startButton.addEventListener("click", async () => {
  if (busy) return;
  const wasRunning = backend.running;
  const requestedModel = modelSelect.value;
  let precheckedStartupCache: ModelStorageStatus | null = null;
  if (!wasRunning) {
    if (modelAvailabilityCheckInFlight) return;
    modelAvailabilityCheckInFlight = true;
    try {
      const cache = await invoke<ModelStorageStatus>("get_model_storage_status", { model: requestedModel });
      modelStorageStatus = cache;
      renderModelStorageSummary(cache);
      renderModelDownloadUI();
      if (!cache.modelCached) {
        nudgeModelDownloadButton();
        return;
      }
      precheckedStartupCache = cache;
    } catch (error) {
      showNotice(`Could not verify the selected model cache; the server was not started. ${String(error)}`, "error");
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
    return;
  }
  const wantsSwitch = wasRunning && !!backend.model && requestedModel !== backend.model;
  if (wantsSwitch) {
    if (modelDownloadStatus.active || modelStorageBusy) {
      showNotice(translateKey("msg.finish_or_cancel_model_downloads_and_storage_changes_before_swit.77238833", {}, getActiveLocale()), "warning");
      return;
    }
    setBusy(true);
    try {
      const selectedCache = await invoke<ModelStorageStatus>("get_model_storage_status", { model: requestedModel });
      modelStorageStatus = selectedCache;
      renderModelStorageSummary(selectedCache);
      if (!selectedCache.modelCached) {
        setBusy(false);
        nudgeModelDownloadButton();
        return;
      }
    } catch (error) {
      showNotice(translateKey("msg.could_not_verify_the_selected_model_cache_the_current_server_was.4456b302", { arg1: String(error) }, getActiveLocale()), "error");
      setBusy(false);
      return;
    }
    setBusy(false);
    modelSwitchTargetAfterStop = requestedModel;
  }
  serverOperation = wasRunning ? "stopping" : "starting";
  startButton.classList.add(wasRunning ? "is-stopping" : "is-starting");
  startButton.setAttribute("aria-busy", "true");
  startProgress.classList.remove("hidden");
  startProgress.setAttribute("aria-busy", "true");
  startProgressText.textContent = wasRunning ? (wantsSwitch ? translateKey("msg.stopping_before_switching_models.fb0c9aa6", {}, getActiveLocale()) : translateKey("msg.stopping_your_local_server.dfa3b390", {}, getActiveLocale())) : translateKey("msg.starting_your_local_server.f6149329", {}, getActiveLocale());
  startProgressDescription.textContent = wasRunning
    ? "Closing the managed process and checking that its port is released."
    : "Starting the backend and checking that the local endpoint responds.";
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
async function refreshAppBuildInfo() {
  try {
    const info = await invoke<AppBuildInfo>("get_app_build_info");
    if (info && typeof info.version === "string" && typeof info.buildId === "string") {
      const displayVersion = formatProductVersionLabel(info.version);
      headerVersionLabel.textContent = displayVersion;
      headerBuildId.textContent = info.buildId;
      aboutVersionLabel.textContent = `RDT Connect · ${displayVersion} · Build ${info.buildId}`;
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
    status === "up-to-date" ? "up-to-date" :
    status === "offline" ? "offline" :
    status === "error" ? "error" : null;

  void invoke("set_update_indicator", {
    state: nativeState,
    displayText: updateMessage(updateUiMessageKey, updateUiMessageParams),
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
  const raw = error instanceof Error ? error.message : String(error);
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

void listen("rdt-connect-open-about", () => {
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

void listen("rdt-connect-check-updates", () => {
  void checkForApplicationUpdates(true);
}).catch(() => undefined);
void listen("rdt-connect-update-window-ready", () => {
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

void listen("rdt-connect-update-window-state-request", () => {
  publishUpdateWindowState();
}).catch(() => undefined);
void listen("rdt-connect-update-check-request", () => {
  void checkForApplicationUpdates(true);
}).catch(() => undefined);
void listen("rdt-connect-update-install-request", () => {
  void installAvailableApplicationUpdate();
}).catch(() => undefined);
void listen("rdt-connect-update-relaunch-request", () => {
  if (updateInstalledAwaitingRelaunch) void installAvailableApplicationUpdate();
}).catch(() => undefined);
void listen("rdt-connect-app-restart-request", async () => {
  try { await relaunch(); } catch (error) {
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
autoCheckUpdatesToggle.addEventListener("change", () => {
  try { localStorage.setItem(AUTO_CHECK_UPDATES_KEY, String(autoCheckUpdatesToggle.checked)); } catch { /* preference remains session-local if storage is unavailable */ }
  if (!autoCheckUpdatesToggle.checked && updateCheckInFlight && !updateCheckInFlightManual) {
    updateCheckGeneration += 1;
    updateCheckInFlight = null;
    updateCheckInFlightManual = false;
  }
  if (canRunAutomaticUpdateCheck()) void checkForApplicationUpdates();
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
      showNotice(`Could not open ${link.textContent?.trim() || "license link"}: ${String(error)}`, "error");
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
  catch (error) { showNotice(`Could not open RDT Voice: ${String(error)}`, "error"); }
});
aboutInstallationGuideButton.addEventListener("click", async () => {
  try {
    await invoke("open_windows_installation_guide");
  } catch (error) {
    showNotice(`Could not open the Windows installation guide: ${String(error)}`, "error");
  }
});
aboutYoutubeButton.addEventListener("click", async () => {
  try {
    await invoke("open_youtube_channel");
  } catch (error) {
    showNotice(translateKey("msg.could_not_open_the_youtube_channel_error.df324c0a", { error: String(error) }, getActiveLocale()), "error");
  }
});
aboutCompanyLink.addEventListener("click", async (event) => {
  event.preventDefault();
  try { await invoke("open_about_link", { kind: "github", locale: getActiveLocale() }); }
  catch (error) { showNotice(translateKey("msg.could_not_open_the_company_github_page_error.fa492006", { error: String(error) }, getActiveLocale()), "error"); }
});
aboutBusinessContactLink.addEventListener("click", async (event) => {
  event.preventDefault();
  try { await invoke("open_about_link", { kind: "email", locale: getActiveLocale() }); }
  catch (error) { showNotice(translateKey("msg.could_not_open_the_business_contact_email_er.7af3d7e8", { error: String(error) }, getActiveLocale()), "error"); }
});
openButton.addEventListener("click", async () => runAction(launchRdtVoice));
refreshRemoteButton.addEventListener("click", async () => runAction(async () => {
  await refreshRemoteStatus(true); await refreshDeviceLists();
}));
setupRemoteButton.addEventListener("click", async () => runAction(async () => {
  tailscale = await invoke<TailscaleStatus>("enable_private_access");
  renderRemoteStatus();
  await refreshDeviceLists();
  showNotice(tailscale.message || translateKey("msg.private_access_checked.37fb07fe", {}, getActiveLocale()), tailscale.ready ? "good" : "info");
}));
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
    showNotice(keepInMenuBarInput.checked
      ? "The RDT Connect menu-bar icon is now visible. Closing the window hides it there."
      : "The menu-bar icon is hidden. Closing the window will quit RDT Connect and stop its managed server.", "info");
  } catch (error) {
    showNotice(translateKey("msg.could_not_save_the_menu_bar_preference_error.cf3a5e22", { error: String(error) }, getActiveLocale()), "error");
    keepInMenuBarInput.checked = !keepInMenuBarInput.checked;
  } finally { keepInMenuBarInput.disabled = false; }
});

void (async () => {
  startButton.disabled = true;
  try { keepInMenuBarInput.checked = await invoke<boolean>("get_keep_in_menu_bar"); }
  catch { keepInMenuBarInput.checked = true; }
  await refreshOrphanedBackendStatus();
  await refreshBackendStatus();
  await refreshLocalHttpsStatus(false, false, true);
  await refreshModelStorageStatus();
  await refreshModelDownloadStatus();
  void refreshModelCacheBadges();
  await refreshRemoteStatus();
  startupReady = true;
  setBusy(false);
  renderBackendStatus();
})();
setInterval(() => {
  if (!busy) void refreshBackendStatus().then(() => { if (!backend.running) void refreshOrphanedBackendStatus(); });
}, 5000);
window.addEventListener("focus", () => {
  if (!busy) void refreshBackendStatus().then(() => { if (!backend.running) void refreshOrphanedBackendStatus(); });
  void refreshLocalHttpsStatus();
});

// Recheck while visible so external certificate or Keychain changes are picked up.
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

    if (phraseInput.value.trim().toLowerCase() !== "tray test") {
      if (phraseStatus) phraseStatus.textContent = "Not recognized";
      return;
    }

    const showing = panel.style.display !== "none";
    panel.style.display = showing ? "none" : "flex";
    phraseInput.value = "";
    if (phraseStatus) phraseStatus.textContent = showing ? "Hidden" : "Shown";
  });

/* RDT_LOCAL_TRAY_ICON_TEST_END */
