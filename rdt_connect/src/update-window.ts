import { emit, listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { getActiveLocale, initializeLocalization, translateKey } from "./i18n";
import "./update.css";

type UpdateUiAction = "install" | "installing" | "reopen" | null;
type UpdateUiState = { key: string; params?: Record<string, string>; action?: UpdateUiAction };

initializeLocalization();
const shell = document.querySelector<HTMLElement>(".update-window-shell");
const status = document.querySelector<HTMLElement>(".update-window-status");
const message = document.getElementById("update-window-message");
const checkButton = document.getElementById("update-window-check-button") as HTMLButtonElement;
const primaryButton = document.getElementById("update-window-primary-button") as HTMLButtonElement;
const heading = document.querySelector<HTMLHeadingElement>(".update-window-header h1");
let currentAction: UpdateUiAction = null;

function text(key: string, params: Record<string, string> = {}): string {
  return translateKey(key, params, getActiveLocale());
}
function render(state: UpdateUiState): void {
  currentAction = state.action ?? null;
  if (message) message.textContent = text(state.key, state.params ?? {});
  status?.classList.toggle("is-up-to-date", state.key === "updater.upToDate");
  status?.classList.toggle("is-update-available", state.key === "updater.available");
  status?.classList.toggle("is-error", state.key === "updater.checkFailed" || state.key === "updater.feedMissing" || state.key === "updater.installFailed" || state.key === "updater.restartFailed");
  const busy = state.key === "updater.checking" || state.action === "installing";
  shell?.classList.toggle("is-busy", busy);
  checkButton.disabled = busy || state.action === "installing";
  primaryButton.hidden = !currentAction;
  primaryButton.disabled = currentAction === "installing";
  if (currentAction === "install") primaryButton.textContent = text("updater.downloadInstall");
  else if (currentAction === "installing") primaryButton.textContent = text("updater.downloading");
  else if (currentAction === "reopen") primaryButton.textContent = text("updater.reopenApp");
  checkButton.hidden = currentAction === "install" || currentAction === "installing" || currentAction === "reopen";

  // Keep results visible so users can read them and manually retry checks.
}

async function requestMainState(): Promise<void> {
  await emit("rdt-connect-update-window-state-request", {});
}

async function start(): Promise<void> {
  if (heading) heading.textContent = text("msg.application_updates.4dae8ce0");
  checkButton.textContent = text("msg.check_for_updates.f26f3275");
  if (message) message.textContent = text("updater.checking");
  await listen<UpdateUiState>("rdt-connect-update-status", (event) => render(event.payload));
  await listen("rdt-connect-update-window-recheck", () => {
    void emit("rdt-connect-update-check-request", {});
  });
  checkButton.addEventListener("click", () => { void emit("rdt-connect-update-check-request", {}); });
  primaryButton.addEventListener("click", () => {
    if (currentAction === "install") void emit("rdt-connect-update-install-request", {});
    else if (currentAction === "reopen") void emit("rdt-connect-update-relaunch-request", {});
  });
  await requestMainState();
}

void start().catch((error) => {
  if (message) message.textContent = text("updater.checkFailed");
  shell?.classList.remove("is-busy");
});
