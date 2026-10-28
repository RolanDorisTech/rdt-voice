/// <reference types="vite/client" />
/** Native RDT Connect localization runtime.
 *
 * Translates existing user-facing DOM strings from the frozen English catalog,
 * including interpolated messages, accessible attributes, and rich messages
 * marked with data-l10n-rich. It deliberately never evaluates translated HTML.
 */
type LocaleCatalog = {
  _meta: { locale: string; languageName: string; direction: "ltr" | "rtl"; catalogVersion: number; source: string };
  messages: Record<string, string>;
};
type Match = { key: string; params: Record<string, string> };
type TemplateMatcher = { key: string; regex: RegExp; names: string[]; literalLength: number; prefix: string; suffix: string };

const PREFERENCE_KEY = "rdt_connect_app_language";
const LANGUAGE_ORDER = [
  "en", "zh-CN", "zh-TW", "fr", "es", "ko", "ja", "de", "th", "vi", "tl", "id", "hi", "ms", "pt", "ru", "it", "lo", "ar", "tr", "pl", "nl", "uk", "sv", "cs", "no",
];
const localeModules = import.meta.glob("./locales/*.json", { eager: true, import: "default" }) as Record<string, LocaleCatalog>;
const catalogs = new Map<string, LocaleCatalog>();
for (const [path, catalog] of Object.entries(localeModules)) {
  const code = path.slice("./locales/".length, -".json".length);
  if (catalog && catalog._meta?.locale === code && catalog.messages) catalogs.set(code, catalog);
}
const english = catalogs.get("en");
if (!english) throw new Error("RDT Connect English locale is missing; cannot initialize localization safely.");

const exactIndexes = new Map<string, Map<string, string>>();
const templateIndexes = new Map<string, TemplateMatcher[]>();
const originalText = new WeakMap<Text, string>();
const originalAttributes = new WeakMap<Element, Map<string, string>>();
const richOriginalSlots = new WeakMap<HTMLElement, Map<string, HTMLElement>>();
const richRenderedLocale = new WeakMap<HTMLElement, string>();
let activeLocale = "en";
let initialized = false;
let observer: MutationObserver | null = null;

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function compileTemplate(template: string): { regex: RegExp; names: string[]; literalLength: number; prefix: string; suffix: string } | null {
  const token = /\{([A-Za-z][A-Za-z0-9_]*)\}/g;
  let cursor = 0;
  let source = "^";
  const names: string[] = [];
  let match: RegExpExecArray | null;
  while ((match = token.exec(template)) !== null) {
    source += escapeRegex(template.slice(cursor, match.index)) + "([\\s\\S]*?)";
    names.push(match[1]);
    cursor = match.index + match[0].length;
  }
  if (!names.length) return null;
  source += escapeRegex(template.slice(cursor)) + "$";
  const literals = template.replace(token, "");
  const prefix = template.split(/\{[A-Za-z][A-Za-z0-9_]*\}/, 1)[0] || "";
  const suffixParts = template.split(/\{[A-Za-z][A-Za-z0-9_]*\}/);
  const suffix = suffixParts[suffixParts.length - 1] || "";
  return { regex: new RegExp(source), names, literalLength: literals.length, prefix, suffix };
}
function buildIndex(locale: string): void {
  if (exactIndexes.has(locale) || templateIndexes.has(locale)) return;
  const catalog = catalogs.get(locale);
  if (!catalog) return;
  const exact = new Map<string, string>();
  const templates: TemplateMatcher[] = [];
  for (const [key, value] of Object.entries(catalog.messages)) {
    const compiled = compileTemplate(value);
    if (compiled) templates.push({ key, ...compiled });
    else if (!exact.has(value)) exact.set(value, key);
  }
  templates.sort((a, b) => b.literalLength - a.literalLength || b.prefix.length - a.prefix.length);
  exactIndexes.set(locale, exact);
  templateIndexes.set(locale, templates);
}
for (const code of catalogs.keys()) buildIndex(code);

function findMatch(locale: string, value: string): Match | null {
  buildIndex(locale);
  const exact = exactIndexes.get(locale)?.get(value);
  if (exact) return { key: exact, params: {} };
  for (const candidate of templateIndexes.get(locale) || []) {
    if (candidate.prefix && !value.startsWith(candidate.prefix)) continue;
    if (candidate.suffix && !value.endsWith(candidate.suffix)) continue;
    const result = candidate.regex.exec(value);
    if (!result) continue;
    const params: Record<string, string> = {};
    candidate.names.forEach((name, index) => {
      if (!(name in params)) params[name] = result[index + 1] ?? "";
    });
    return { key: candidate.key, params };
  }
  return null;
}
function interpolate(template: string, params: Record<string, string>): string {
  return template.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (whole, name: string) => name in params ? params[name] : whole);
}
function translatedFromMatch(match: Match, targetLocale: string): string | null {
  const target = catalogs.get(targetLocale)?.messages[match.key];
  return typeof target === "string" ? interpolate(target, match.params) : null;
}
function translateRaw(
  value: string,
  targetLocale: string,
  sourceHint?: string,
  knownEnglishMatch?: Match | null,
): string {
  if (!value) return value;
  const englishMatch = knownEnglishMatch === undefined
    ? findMatch("en", sourceHint ?? value)
    : knownEnglishMatch;
  if (englishMatch) return translatedFromMatch(englishMatch, targetLocale) ?? (sourceHint ?? value);
  // Never infer source text by reverse-matching a previous translation.
  // Translated nodes are restored through their saved English originals.
  return value;
}
function shouldIgnoreElement(element: Element | null): boolean {
  return !!element?.closest('[data-l10n-ignore], #app-language');
}
function inRichMessage(element: Element | null): boolean {
  return !!element?.closest("[data-l10n-rich]");
}
function splitWhitespace(value: string): { leading: string; core: string; trailing: string } {
  const leading = value.match(/^\s*/)?.[0] ?? "";
  const trailing = value.match(/\s*$/)?.[0] ?? "";
  const coreEnd = Math.max(leading.length, value.length - trailing.length);
  return { leading, core: value.slice(leading.length, coreEnd), trailing: value.slice(coreEnd) };
}
function localizeTextNode(node: Text, targetLocale: string): void {
  if (!node.parentElement || shouldIgnoreElement(node.parentElement) || inRichMessage(node.parentElement)) return;
  const raw = node.nodeValue ?? "";
  const parts = splitWhitespace(raw);
  if (!parts.core) return;
  const priorOriginal = originalText.get(node);
  const currentEnglishMatch = findMatch("en", parts.core);
  const currentIsEnglish = !!currentEnglishMatch;
  const source = currentIsEnglish ? parts.core : (priorOriginal ?? parts.core);
  const englishMatch = currentIsEnglish
    ? currentEnglishMatch
    : priorOriginal !== undefined ? findMatch("en", priorOriginal) : null;
  const translated = translateRaw(parts.core, targetLocale, source, englishMatch);
  if (translated !== parts.core) {
    originalText.set(node, source);
    node.nodeValue = parts.leading + translated + parts.trailing;
  } else if (currentIsEnglish || priorOriginal !== undefined) {
    originalText.set(node, source);
  }
}
const TRANSLATABLE_ATTRIBUTES = ["title", "aria-label", "aria-description", "aria-roledescription", "placeholder", "alt", "data-tooltip"];
function localizeAttribute(element: Element, name: string, targetLocale: string): void {
  if (shouldIgnoreElement(element) || name === "data-l10n-rich") return;

  // Explicit attribute keys take precedence over reverse matching.
  const keyedAttributes = element.getAttribute("data-l10n-attr-keys");
  if (keyedAttributes) {
    try {
      const mapping = JSON.parse(keyedAttributes) as Record<string, unknown>;
      const key = mapping[name];
      if (typeof key === "string" && key) {
        element.setAttribute(name, translateKey(key, {}, targetLocale));
        return;
      }
    } catch {
      // Ignore malformed metadata and preserve the legacy localization path.
    }
  }

  const raw = element.getAttribute(name);
  if (raw === null || !raw.trim()) return;
  let byName = originalAttributes.get(element);
  if (!byName) { byName = new Map<string, string>(); originalAttributes.set(element, byName); }
  const prior = byName.get(name);
  const currentIsEnglish = !!findMatch("en", raw);
  const source = currentIsEnglish ? raw : (prior ?? raw);
  const translated = translateRaw(raw, targetLocale, source);
  if (translated !== raw) {
    byName.set(name, source);
    element.setAttribute(name, translated);
  } else if (currentIsEnglish || prior !== undefined) {
    byName.set(name, source);
  }
}
function localizeSubtree(node: Node, targetLocale: string): void {
  if (node.nodeType === Node.TEXT_NODE) { localizeTextNode(node as Text, targetLocale); return; }
  if (!(node instanceof Element) || shouldIgnoreElement(node)) return;

  // Explicit keys are resolved directly, never inferred from translated text.
  const localizationKey = node.getAttribute("data-l10n-key");
  if (localizationKey) {
    node.textContent = translateKey(localizationKey, {}, targetLocale);
    return;
  }

  for (const name of TRANSLATABLE_ATTRIBUTES) localizeAttribute(node, name, targetLocale);
  if (node instanceof HTMLElement && node.hasAttribute("data-l10n-rich")) {
    renderRichMessage(node, targetLocale);
    return;
  }
  for (const child of Array.from(node.childNodes)) localizeSubtree(child, targetLocale);
}
function renderRichMessage(element: HTMLElement, targetLocale: string): void {
  const key = element.getAttribute("data-l10n-rich");
  if (!key || richRenderedLocale.get(element) === targetLocale) return;
  const template = catalogs.get(targetLocale)?.messages[key] ?? english!.messages[key];
  if (typeof template !== "string") return;
  let slots = richOriginalSlots.get(element);
  if (!slots) {
    slots = new Map<string, HTMLElement>();
    for (const slot of Array.from(element.querySelectorAll<HTMLElement>("[data-l10n-slot]"))) {
      const name = slot.getAttribute("data-l10n-slot");
      if (name && !slots.has(name)) slots.set(name, slot.cloneNode(true) as HTMLElement);
    }
    richOriginalSlots.set(element, slots);
  }
  const fragment = document.createDocumentFragment();
  const token = /\{([A-Za-z][A-Za-z0-9_]*)\}/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = token.exec(template)) !== null) {
    if (match.index > cursor) fragment.appendChild(document.createTextNode(template.slice(cursor, match.index)));
    const slot = slots.get(match[1]);
    if (slot) {
      const clone = slot.cloneNode(true) as HTMLElement;
      localizeSubtree(clone, targetLocale);
      fragment.appendChild(clone);
    } else {
      fragment.appendChild(document.createTextNode(match[0]));
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < template.length) fragment.appendChild(document.createTextNode(template.slice(cursor)));
  element.replaceChildren(fragment);
  richRenderedLocale.set(element, targetLocale);
}
function localizeDocument(targetLocale: string): void {
  const languageSelect = document.getElementById("app-language") as HTMLSelectElement | null;
  if (languageSelect && languageSelect.options.length) languageSelect.value = targetLocale;
  document.documentElement.lang = targetLocale;
  document.documentElement.dir = catalogs.get(targetLocale)?._meta.direction ?? "ltr";
  if (document.body) for (const child of Array.from(document.body.childNodes)) localizeSubtree(child, targetLocale);
  // Include head text, notably the document title.
  if (document.head) for (const child of Array.from(document.head.childNodes)) localizeSubtree(child, targetLocale);
}
function detectSystemLocale(): string {
  const candidates = [...(Array.isArray(navigator.languages) ? navigator.languages : []), navigator.language || ""];
  for (const raw of candidates) {
    const tag = String(raw).trim().replace(/_/g, "-").toLowerCase();
    if (!tag) continue;
    if (tag === "zh" || tag.startsWith("zh-")) {
      if (/(?:^|-)hant(?:-|$)/.test(tag) || /^zh-(?:tw|hk|mo)(?:-|$)/.test(tag)) return catalogs.has("zh-TW") ? "zh-TW" : "en";
      if (/(?:^|-)hans(?:-|$)/.test(tag) || /^zh-(?:cn|sg)(?:-|$)/.test(tag) || tag === "zh") return catalogs.has("zh-CN") ? "zh-CN" : "en";
      // Unknown Chinese region variants prefer Simplified Chinese instead of falling straight to English.
      return catalogs.has("zh-CN") ? "zh-CN" : "en";
    }
    const exact = [...catalogs.keys()].find((code) => code.toLowerCase() === tag);
    if (exact) return exact;
    const base = tag.split("-")[0];
    const normalized = ({ fil: "tl", nb: "no", nn: "no" } as Record<string, string>)[base] || base;
    const match = [...catalogs.keys()].find((code) => code.toLowerCase() === normalized);
    if (match) return match;
  }
  return "en";
}
function populateLanguageSelect(): void {
  const select = document.getElementById("app-language") as HTMLSelectElement | null;
  if (!select) return;
  select.replaceChildren();
  const ordered = LANGUAGE_ORDER.filter((code) => catalogs.has(code));
  for (const code of ordered) {
    const catalog = catalogs.get(code)!;
    const option = document.createElement("option");
    option.value = code;
    option.textContent = catalog._meta.languageName;
    select.appendChild(option);
  }
}
export function initializeLocalization(): string {
  if (initialized) return activeLocale;
  initialized = true;
  populateLanguageSelect();
  let saved = "";
  try { saved = localStorage.getItem(PREFERENCE_KEY) || ""; } catch { /* Browser storage may be unavailable. */ }
  activeLocale = catalogs.has(saved) ? saved : detectSystemLocale();
  localizeDocument(activeLocale);
  const observerOptions: MutationObserverInit = {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: [...TRANSLATABLE_ATTRIBUTES, "data-l10n-rich", "data-l10n-key"],
  };
  if (typeof MutationObserver !== "undefined" && document.documentElement) {
    observer = new MutationObserver((records) => {
      // Ignore mutations caused by localization itself.
      observer?.disconnect();
      try {
        for (const record of records) {
          if (record.type === "characterData" && record.target instanceof Text) {
            localizeTextNode(record.target, activeLocale);
          } else if (record.type === "childList") {
            for (const node of Array.from(record.addedNodes)) {
              localizeSubtree(node, activeLocale);
            }
          } else if (record.type === "attributes" && record.target instanceof Element) {
            const name = record.attributeName;
            if (name === "data-l10n-rich" && record.target instanceof HTMLElement) {
              richRenderedLocale.delete(record.target);
              renderRichMessage(record.target, activeLocale);
            } else if (name === "data-l10n-key") {
              localizeSubtree(record.target, activeLocale);
            } else if (name && TRANSLATABLE_ATTRIBUTES.includes(name)) {
              localizeAttribute(record.target, name, activeLocale);
            }
          }
        }
      } finally {
        if (document.documentElement) {
          observer?.observe(document.documentElement, observerOptions);
        }
      }
    });
    observer.observe(document.documentElement, observerOptions);
  }
  return activeLocale;
}
export function setActiveLocale(locale: string, persist = false): string {
  const normalized = catalogs.has(locale) ? locale : "en";
  if (!initialized) initializeLocalization();
  activeLocale = normalized;
  if (observer) {
    // A full locale pass rewrites many observed nodes. Suspend observation so
    // those writes cannot feed back into repeated translation matching.
    observer.disconnect();
    try {
      localizeDocument(normalized);
    } finally {
      observer.observe(document.documentElement, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: [...TRANSLATABLE_ATTRIBUTES, "data-l10n-rich", "data-l10n-key"],
      });
    }
  } else {
    localizeDocument(normalized);
  }
  if (persist) {
    try { localStorage.setItem(PREFERENCE_KEY, normalized); } catch { /* Preference is optional. */ }
  }
  const languageSelect = document.getElementById("app-language") as HTMLSelectElement | null;
  if (languageSelect) languageSelect.value = normalized;
  window.dispatchEvent(new CustomEvent("rdt-connect-language-changed", { detail: { locale: normalized } }));
  return normalized;
}
/**
 * Render dynamic UI from a stable catalog key.
 * Prefer this over translating previously rendered text.
 */
export function setLocalizedText(
  element: HTMLElement,
  key: string,
  params: Record<string, string | number> = {},
): void {
  element.dataset.l10nKey = key;
  element.textContent = translateKey(key, params, activeLocale);
}

/** Render a translatable attribute from a stable catalog key. */
export function setLocalizedAttribute(
  element: HTMLElement,
  attribute: string,
  key: string,
  params: Record<string, string | number> = {},
): void {
  const allowed = ["title", "aria-label", "aria-description",
    "aria-roledescription", "placeholder", "alt", "data-tooltip"];
  if (!allowed.includes(attribute)) {
    throw new Error(`Unsupported localized attribute: ${attribute}`);
  }

  let keys = element.dataset.l10nAttrKeys;
  const mapping: Record<string, string> = keys ? JSON.parse(keys) : {};
  mapping[attribute] = key;
  element.dataset.l10nAttrKeys = JSON.stringify(mapping);
  element.setAttribute(attribute, translateKey(key, params, activeLocale));
}

export function getActiveLocale(): string { return activeLocale; }
export function getLocaleChoices(): Array<{ code: string; languageName: string }> {
  return LANGUAGE_ORDER.filter((code) => catalogs.has(code)).map((code) => ({ code, languageName: catalogs.get(code)!._meta.languageName }));
}
export function translateKey(key: string, params: Record<string, string | number> = {}, locale = activeLocale): string {
  const template = catalogs.get(locale)?.messages[key] ?? english!.messages[key] ?? key;
  return template.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (whole, name: string) => name in params ? String(params[name]) : whole);
}
