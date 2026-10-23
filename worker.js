

/**

 * RDT Voice v0.2-DEV.6 - EasyWhisper worker (GitHub / Regular Version)

 * PolyForm Noncommercial License 1.0.0 - Copyright (c) 2026 RolanDorisTech

 * Source: https://github.com/RolanDorisTech/rdt-voice

 * Commercial licensing: see COMMERCIAL_LICENSE.md or contact woknewsbusiness@gmail.com

 *

 * This file is part of RDT Voice project code licensed under PolyForm Noncommercial 1.0.0.

 * Third-party: @huggingface/transformers (Apache 2.0) - https://github.com/huggingface/transformers.js

 * Whisper models retain their own licenses (OpenAI / Hugging Face publishers).

 * Offline-first: check local cache first, only fetch HF if not cached.

 */

/* RDT Voice EasyWhisper worker - GITHUB / REGULAR VERSION

   Offline-first: check local cache first, only fetch HF if not cached. */

const TF_DIST = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.1/dist/";

// Transformers.js 3.7.1 uses ONNX Runtime Web 1.22.0-dev.20250409-89f8206ba4.

// Keep the WASM loader pointed at the ONNX Runtime binaries, not the Transformers.js package.

const ORT_WASM_DIST = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0-dev.20250409-89f8206ba4/dist/";

import { pipeline, env } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.1/dist/transformers.min.js";

env.backends.onnx.wasm.wasmPaths = ORT_WASM_DIST;

env.allowRemoteModels = true;

env.useBrowserCache = true;

env.useWasmCache = true;

try { env.backends.onnx.logLevel = "verbose"; env.backends.onnx.debug = true; env.backends.onnx.logSeverityLevel = 0; } catch {}

// Debug logging is off unless the page sets self.RDT_DEBUG (kept out of the public console).
const RDT_DEBUG = !!self.RDT_DEBUG;


// Browser Cache Storage coordination. Multiple tabs can share this origin's
// transformers-cache. Serialize reads/writes for the same exact model URL using
// Web Locks when available, preventing one tab from observing an entry while
// another tab is replacing it. Older browsers fall back to a worker-local queue.
const rdtCacheLocalQueues = new Map();
const rdtCacheCoordinationInfo = {
  strategy: "same-key-web-locks-with-local-queue-fallback",
  webLocksAvailable: !!(typeof navigator !== "undefined" && navigator.locks && typeof navigator.locks.request === "function"),
  actionCount: 0,
  lastAction: null
};
let rdtLastOnnxArtifactCacheCheck = null;
let rdtDeepHashLargeOnNextLegacyRetry = false;
// Keep the current load request's manifest file metadata available to all pipeline rebuilds.
// Worker jobs are serialized; this lets a WASM fallback/retry reuse the exact pinned file list.
let rdtActiveExpectedModelFiles = [];

function rdtCacheKeyUrl(key) {
  try { if (key && typeof key.url === "string") return key.url; } catch {}
  try { return new URL(String(key), self.location && self.location.href || "https://rdt.invalid/").href; }
  catch { return String(key).slice(0, 512); }
}
function rdtCacheKeyLabel(key) {
  try {
    const u = new URL(rdtCacheKeyUrl(key));
    const parts = u.pathname.split("/").filter(Boolean);
    return String(parts[parts.length - 1] || "cache-entry").slice(0, 120);
  } catch { return "cache-entry"; }
}
async function rdtWithCacheEntryLock(key, action, callback) {
  const url = rdtCacheKeyUrl(key);
  const label = rdtCacheKeyLabel(key);
  const lockName = "rdt-voice-easywhisper-cache:" + url;
  const started = Date.now();
  const run = async (mode) => {
    const waitMs = Math.max(0, Date.now() - started);
    try { return await callback(); }
    finally {
      rdtCacheCoordinationInfo.actionCount += 1;
      rdtCacheCoordinationInfo.lastAction = { action, mode, file: label, waitMs };
    }
  };
  let locks = null;
  try { locks = typeof navigator !== "undefined" ? navigator.locks : null; } catch {}
  if (locks && typeof locks.request === "function") {
    rdtCacheCoordinationInfo.webLocksAvailable = true;
    return locks.request(lockName, { mode: "exclusive" }, () => run("web-locks"));
  }
  rdtCacheCoordinationInfo.webLocksAvailable = false;
  const previous = rdtCacheLocalQueues.get(url) || Promise.resolve();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const tail = previous.catch(() => {}).then(() => gate);
  rdtCacheLocalQueues.set(url, tail);
  await previous.catch(() => {});
  try { return await run("local-worker-queue"); }
  finally {
    release();
    if (rdtCacheLocalQueues.get(url) === tail) rdtCacheLocalQueues.delete(url);
  }
}

// Cache Storage entries saved from streamed relay responses may not retain a
// Content-Length header. Re-attach the stored Blob's size for Transformers.js.
try {
  const RDT_CACHE_NAME = "transformers-cache";
  env.useCustomCache = true;
  env.customCache = {
    async match(key) {
      return rdtWithCacheEntryLock(key, "match", async () => {
        const cache = await caches.open(RDT_CACHE_NAME);
        const r = await cache.match(key);
        if (!r || r.headers.get("content-length") || !r.ok) return r;
        const blob = await r.blob();
        const headers = new Headers(r.headers);
        headers.set("content-length", String(blob.size));
        return new Response(blob, { status: r.status, statusText: r.statusText, headers });
      });
    },
    async put(key, response) {
      return rdtWithCacheEntryLock(key, "put", async () => {
        const cache = await caches.open(RDT_CACHE_NAME);
        await cache.put(key, response);
      });
    }
  };
} catch { env.useCustomCache = false; }

// Cold-download CORS relay. The browser still uses the original Hugging Face URL
// as the cache key; only the network fetch is routed through our fixed-origin relay.
// This preserves existing Cache Storage entries and offline-first behavior.
const RDT_HF_PROXY_ORIGIN = "https://rdt-voice-hf-proxy.rolandoristech.workers.dev";
const RDT_NATIVE_FETCH = globalThis.fetch.bind(globalThis);
const RDT_ALLOWED_HF_PATH = /^\/(onnx-community\/whisper-(?:tiny|base|small|large-v3-turbo))\/resolve\/([0-9a-f]{40})\/([A-Za-z0-9_.\/-]+)$/i;

function rdtHfProxyUrl(requestUrl) {
  let u;
  try { u = new URL(requestUrl); } catch { return null; }
  if (u.origin !== "https://huggingface.co") return null;
  const m = u.pathname.match(RDT_ALLOWED_HF_PATH);
  if (!m) return null;
  const pathParts = m[3].split("/");
  if (pathParts.some(part => !part || part === "." || part === "..")) return null;
  return RDT_HF_PROXY_ORIGIN + "/hf" + u.pathname + u.search;
}


async function rdtResponseLooksLikeHtml(response) {
  if (!response) return false;
  const contentType = String(
    response.headers && response.headers.get("content-type") || ""
  );
  if (/text\/html|application\/xhtml\+xml/i.test(contentType)) return true;
  try {
    const sample = (await response.clone().text()).slice(0, 512)
      .replace(/^\uFEFF/, "").trimStart();
    return /^<(?:!doctype\s+html\b|html\b|head\b|body\b)/i.test(sample);
  } catch {
    return false;
  }
}

async function rdtHfAwareFetch(input, init) {
  let req;
  try {
    req = input instanceof Request ? new Request(input, init) : new Request(input, init);
  } catch {
    return RDT_NATIVE_FETCH(input, init);
  }
  const proxyUrl = rdtHfProxyUrl(req.url);
  if (!proxyUrl) {
    return RDT_NATIVE_FETCH(req);
  }
  // Rebuild the request at the relay URL while preserving Range/conditional headers.
  // The Transformers.js cache still uses the original Hugging Face URL outside fetch.
  const proxiedRequest = new Request(proxyUrl, req);
  const __resp = await RDT_NATIVE_FETCH(proxiedRequest);
  const __rdtMetadataPath = new URL(req.url).pathname;
  if (__resp && __resp.ok && /\.json$/i.test(__rdtMetadataPath) &&
      await rdtResponseLooksLikeHtml(__resp)) {
    let __rdtDirectReason = "no usable response";
    try {
      const __rdtDirect = await RDT_NATIVE_FETCH(req.clone());
      if (__rdtDirect && __rdtDirect.ok &&
          !(await rdtResponseLooksLikeHtml(__rdtDirect))) return __rdtDirect;
      __rdtDirectReason = __rdtDirect
        ? (__rdtDirect.ok
          ? "direct URL also returned HTML"
          : "direct URL returned HTTP " + __rdtDirect.status)
        : "direct URL returned no response";
    } catch (__rdtDirectError) {
      __rdtDirectReason = String(
        __rdtDirectError && __rdtDirectError.message || __rdtDirectError
      ).slice(0, 180);
    }
    throw new Error("Model relay returned HTML instead of JSON for " +
      __rdtMetadataPath + " (HTTP " + __resp.status +
      "). Direct Hugging Face metadata retry failed: " + __rdtDirectReason +
      ". Check the relay's /hf passthrough for this pinned model metadata file.");
  }

  if (__resp.status === 403) {
    let detail = "";
    try { detail = (await __resp.clone().text()).slice(0, 180).trim(); } catch {}
    throw new Error("Model relay returned HTTP 403 (forbidden). The deployed relay may not allow this site's origin or the requested pinned model file." + (detail ? " Details: " + detail : ""));
  }
  if (__resp.status === 404) {
    let missingFile = "";
    try {
      const u = new URL(req.url);
      const m = u.pathname.match(/\/resolve\/[0-9a-f]{40}\/(.+)$/i);
      missingFile = m ? m[1] : u.pathname;
    } catch {}
    throw new Error("Model relay returned HTTP 404 (model file not found" + (missingFile ? ": " + missingFile : "") + "). Check the pinned model revision and relay configuration.");
  }
  return __resp;
}

// Transformers.js 3.7.x primarily uses the worker-global fetch. Set env.fetch too
// for builds that consult the environment override; both paths share one wrapper.
self.fetch = rdtHfAwareFetch;
env.fetch = rdtHfAwareFetch;

// ARM32 compatibility: this browser-worker code cannot read Android's native ABI directly.
// Use the actual WebAssembly SIMD feature test, not a guessed bitness, to select the runtime.
// Preserve Transformers.js 3.7.1 model and Whisper support on both runtime paths.
// For browsers without WebAssembly SIMD, inject the separate ORT Web 1.17.3 WASM-only ESM namespace before
// loading an isolated v3.7.1 module copy. This avoids pairing scalar WASM with SIMD/JSEP JavaScript glue.
// The selected model ID and revision stay pinned. Affected Android Tiny/WASM profiles use the pinned INT8 pair; other dtype choices remain unchanged.
const RDT_COMPAT_TRANSFORMERS_URL = new URL("./rdt-easywhisper-compat-sequential-r71.min.js", import.meta.url).href;
const RDT_COMPAT_ORT_WASM_MODULE_URL = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/esm/ort.wasm.min.js";
const RDT_COMPAT_ORT_WASM_BINARY = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort-wasm.wasm";
const RDT_ORT_GLOBAL_SYMBOL = Symbol.for("onnxruntime");
let rdtActivePipelineFactory = pipeline;
let rdtActiveRuntimeEnv = env;
let rdtActiveRuntimeKind = "runtime-not-selected-yet";
let rdtActiveRuntimeSelectionReason = "awaiting-device-and-feature-selection";
let rdtCompatOrtRuntimeModule = null;
let rdtCompatOrtRuntimeModulePromise = null;
let rdtCompatTransformersModulePromise = null;
let rdtCompatOrtRuntimeImporter = () => import(RDT_COMPAT_ORT_WASM_MODULE_URL);
let rdtCompatTransformersImporter = () => import(RDT_COMPAT_TRANSFORMERS_URL);

async function rdtImportWasmOnlyCompatTransformers() {
  if (!rdtCompatOrtRuntimeModulePromise) {
    rdtCompatOrtRuntimeModulePromise = Promise.resolve().then(() => rdtCompatOrtRuntimeImporter()).catch((err) => {
      rdtCompatOrtRuntimeModulePromise = null;
      throw err;
    });
  }
  const ortWasmOnly = await rdtCompatOrtRuntimeModulePromise;
  if (!ortWasmOnly || !ortWasmOnly.InferenceSession || typeof ortWasmOnly.InferenceSession.create !== "function" || !ortWasmOnly.Tensor || !ortWasmOnly.env || !ortWasmOnly.env.wasm) {
    throw new Error("version-matched-ort-wasm-only-module-shape-invalid");
  }
  const previousDescriptor = Object.getOwnPropertyDescriptor(globalThis, RDT_ORT_GLOBAL_SYMBOL);
  try {
    Object.defineProperty(globalThis, RDT_ORT_GLOBAL_SYMBOL, {value: ortWasmOnly, writable: true, configurable: true, enumerable: false});
    const transformers = await rdtCompatTransformersImporter();
    if (!transformers || typeof transformers.pipeline !== "function" || !transformers.env || transformers.env.backends?.onnx !== ortWasmOnly.env) {
      throw new Error("transformers-v3-did-not-adopt-the-wasm-only-onnx-runtime");
    }
    rdtCompatOrtRuntimeModule = ortWasmOnly;
    return transformers;
  } finally {
    if (previousDescriptor) Object.defineProperty(globalThis, RDT_ORT_GLOBAL_SYMBOL, previousDescriptor);
    else delete globalThis[RDT_ORT_GLOBAL_SYMBOL];
  }
}
const rdtSharedTransformersCache = env.customCache || null;
const rdtLegacyCompatibilityAttemptedModels = new Set();

function rdtRuntimePlanForDevice(device, simdSupported, forceLegacyNoSimd = false) {
  if (forceLegacyNoSimd) return "legacy-non-simd";
  if (device === "wasm" && simdSupported !== true) return "legacy-non-simd";
  return "modern";
}

function rdtIsLegacyNonSimdRuntime() {
  return rdtActiveRuntimeKind === "huggingface-transformers-3.7.1-ort-web-1.17.3-wasm-only-non-simd";
}

function rdtBrowserArchitectureHint() {
  const nav = typeof navigator !== "undefined" ? navigator : null;
  if (!nav) return "unknown";
  const ua = String(nav.userAgent || "");
  const platform = String(nav.platform || "");
  const reported = platform + " " + ua;
  if (/Android/i.test(ua) && /(?:armv7l|armv7|armeabi-v7a)/i.test(reported) && !/(?:arm64|aarch64)/i.test(reported)) return "android-arm32-likely";
  if (/Android/i.test(ua) && /(?:arm64|aarch64)/i.test(reported)) return "android-arm64-likely";
  if (/Android/i.test(ua) && rdtWasmSimdSupported() === false) return "android-legacy-wasm-architecture-unknown";
  return "unknown-or-not-exposed";
}

function rdtRuntimeDiagnosticInfo() {
  const legacy = rdtIsLegacyNonSimdRuntime();
  const selected = rdtActiveRuntimeKind !== "runtime-not-selected-yet";
  return {
    runtimeKind: rdtActiveRuntimeKind,
    transformersPackage: legacy ? "@huggingface/transformers@3.7.1+rdt-r60-device-registry-patch" : selected ? "@huggingface/transformers@3.7.1" : null,
    transformersRuntimePatch: legacy ? "injected-official-onnxruntime-web-wasm-device-registry-r60-plus-r71-sequential-tiny-low-memory" : null,
    onnxRuntimePackage: legacy ? "onnxruntime-web@1.17.3/wasm-only" : selected ? "onnxruntime-web@1.22.0-dev.20250409-89f8206ba4" : null,
    wasmSIMDSupported: rdtWasmSimdSupported(),
    architectureHint: rdtBrowserArchitectureHint(),
    nativeAndroidABIAvailableToBrowser: false,
    selectionReason: rdtActiveRuntimeSelectionReason,
    nonSimdWasmExpected: legacy ? true : selected ? false : null,
    nonSimdWasmPathStrategy: legacy ? "onnxruntime-web-1.17.3-wasm-only-esm-loader-plus-ort-wasm.wasm" : null,
    wasmOnlyOnnxRuntimeInjected: legacy ? !!(rdtCompatOrtRuntimeModule && rdtActiveRuntimeEnv && rdtActiveRuntimeEnv.backends && rdtActiveRuntimeEnv.backends.onnx === rdtCompatOrtRuntimeModule.env) : null
  };
}

async function rdtPrepareRuntimeForDevice(device, forceLegacyNoSimd = false, reasonOverride = null, modelId = null) {
  const simdSupported = rdtWasmSimdSupported();
  // Device memory selects the Tiny weight files, not the runtime implementation.
  // Other SIMD-capable browsers get the modern runtime first; only an actual failure
  // may trigger the existing bounded compatibility retry.
  const arm32TinyPrimaryCompatibility = device === "wasm" &&
    simdSupported === true &&
    String(modelId || "").toLowerCase() === "onnx-community/whisper-tiny" &&
    rdtBrowserArchitectureHint() === "android-arm32-likely" &&
    rdtIsLegacyArmAndroidTinyWasmDevice(modelId);
  const useLegacy = arm32TinyPrimaryCompatibility || rdtRuntimePlanForDevice(device, simdSupported, forceLegacyNoSimd) === "legacy-non-simd";
  if (!useLegacy) {
    rdtActivePipelineFactory = pipeline;
    rdtActiveRuntimeEnv = env;
    rdtActiveRuntimeKind = "huggingface-transformers-3.7.1-ort-web-1.22.0-dev-simd";
    rdtActiveRuntimeSelectionReason = device === "webgpu" ? "webgpu-primary-runtime" : "wasm-simd-feature-available";
    return rdtActiveRuntimeEnv;
  }

  try {
    if (!rdtCompatTransformersModulePromise) {
      rdtCompatTransformersModulePromise = rdtImportWasmOnlyCompatTransformers().catch((err) => {
        rdtCompatTransformersModulePromise = null;
        throw err;
      });
    }
    const legacy = await rdtCompatTransformersModulePromise;
    if (!legacy || typeof legacy.pipeline !== "function" || !legacy.env) throw new Error("legacy-transformers-module-shape-invalid");
    const legacyOnnx = legacy.env.backends && legacy.env.backends.onnx;
    try { if (legacyOnnx) { legacyOnnx.logLevel = "verbose"; legacyOnnx.debug = true; legacyOnnx.logSeverityLevel = 0; } } catch {}
    const legacyWasm = legacyOnnx && legacyOnnx.wasm;
    if (!legacyWasm || typeof legacyWasm !== "object") throw new Error("legacy-onnx-wasm-config-unavailable");

    // The ORT WASM-only ESM bundle is injected before an isolated Transformers.js 3.7.1 module copy is evaluated.
    // Unlike the normal WebGPU/JSEP-enabled bundle, its factory is built for plain WASM and pairs with
    // the scalar ort-wasm.wasm artifact. Mapping a SIMD/JSEP bundle's filename to this scalar binary
    // is not sufficient and caused Firefox ARM32's earlier "WebAssembly SIMD is not supported" error.
    if (!rdtCompatOrtRuntimeModule || legacyOnnx !== rdtCompatOrtRuntimeModule.env) {
      throw new Error("wasm-only-ort-runtime-not-adopted-by-transformers-v3");
    }
    legacyWasm.wasmPaths = { "ort-wasm.wasm": RDT_COMPAT_ORT_WASM_BINARY };
    legacyWasm.simd = false;
    legacyWasm.numThreads = 1;
    if (legacyWasm.simd !== false || legacyWasm.numThreads !== 1 || !legacyWasm.wasmPaths || legacyWasm.wasmPaths["ort-wasm.wasm"] !== RDT_COMPAT_ORT_WASM_BINARY) {
      throw new Error("non-simd-wasm-only-runtime-config-not-applied");
    }
    legacy.env.allowRemoteModels = true;
    legacy.env.allowLocalModels = true;
    legacy.env.useBrowserCache = true;
    legacy.env.useWasmCache = true;
    legacy.env.fetch = rdtHfAwareFetch;
    if (rdtSharedTransformersCache) {
      legacy.env.useCustomCache = true;
      legacy.env.customCache = rdtSharedTransformersCache;
    }

    rdtActivePipelineFactory = legacy.pipeline;
    rdtActiveRuntimeEnv = legacy.env;
    rdtActiveRuntimeKind = "huggingface-transformers-3.7.1-ort-web-1.17.3-wasm-only-non-simd";
    rdtActiveRuntimeSelectionReason = reasonOverride || (arm32TinyPrimaryCompatibility
      ? "legacy-arm32-tiny-primary-runtime"
      : forceLegacyNoSimd
      ? "legacy-arm32-inference-compatibility-retry"
      : simdSupported !== true
        ? (simdSupported === false ? "wasm-simd-feature-unavailable" : "wasm-simd-feature-test-unavailable")
        : "legacy-runtime-forced");
    return rdtActiveRuntimeEnv;
  } catch (err) {
    const detail = String(err && err.message !== undefined ? err.message : err).slice(0, 220);
    throw new Error("The version-matched WASM-only ONNX Runtime Web 1.17.3 loader or isolated Transformers.js 3.7.1 compatibility runtime could not initialize. No incompatible SIMD binary was forced. Try RDT Connect Local Server if the browser cannot run this model. Technical detail: " + detail);
  }
}

const WORKER_VERSION = "2026-10-22-v38-tiny-sequential-compat";

console.info("[RDT worker] version", WORKER_VERSION);

let transcriber    = null;

let currentModelId = null;

let currentDevice  = null;

let lastLoad       = null;

// Structured, privacy-conscious diagnostic state. Never retain audio samples,
// decoded audio buffers, transcript text, authentication values, or complete URLs.
let rdtActiveOperation = null;
let rdtLastOperation = null;
let rdtLastFailure = null;
let rdtLastProgress = null;
let rdtModelCacheSummary = null;
let rdtObservedRuntimeError = null;
let rdtObservedModelInputs = null;

// Transformers.js already writes useful ONNX errors and tensor summaries to the
// worker console. Capture a metadata-only mirror so the developer report can join
// that evidence to the worker stage without retaining tensor data or transcript text.
function rdtSummarizeModelInputs(inputs) {
  try {
    if (!inputs || typeof inputs !== "object") return null;
    return Object.entries(inputs).slice(0, 80).map(([name, value]) => {
      if (!value || typeof value !== "object") return { name: String(name).slice(0, 120), valueType: typeof value };
      let dims = null;
      try { if (Array.isArray(value.dims)) dims = value.dims.slice(0, 8).map(n => Number(n)); } catch {}
      let dataType = null;
      let dataLength = null;
      try {
        const data = value.data;
        if (data && data.constructor && typeof data.length === "number") {
          dataType = String(data.constructor.name).slice(0, 80);
          dataLength = Number.isFinite(Number(data.length)) ? Number(data.length) : null;
        }
      } catch {}
      return {
        name: String(name).slice(0, 120),
        containerType: value.constructor && value.constructor.name ? String(value.constructor.name).slice(0, 80) : "object",
        tensorType: typeof value.type === "string" ? value.type : null,
        dims,
        size: Number.isFinite(Number(value.size)) ? Number(value.size) : null,
        location: typeof value.location === "string" ? value.location : null,
        dataType,
        dataLength
      };
    });
  } catch { return null; }
}

const rdtOriginalConsoleError = typeof console !== "undefined" && typeof console.error === "function" ? console.error.bind(console) : null;
try {
  if (rdtOriginalConsoleError) console.error = (...args) => {
    try {
      const headline = args.length && (typeof args[0] === "string" ? args[0] : String(args[0]));
      if (/An error occurred during model execution/i.test(headline || "")) {
        rdtObservedRuntimeError = { at: new Date().toISOString(), message: String(headline).slice(0, 2400) };
      }
      if (/Inputs given to model/i.test(headline || "")) {
        rdtObservedModelInputs = { at: new Date().toISOString(), inputs: rdtSummarizeModelInputs(args[1]) };
      }
    } catch {}
    return rdtOriginalConsoleError(...args);
  };
} catch {}

function rdtSetOperationStage(stage, details) {
  if (!rdtActiveOperation) return;
  rdtActiveOperation.stage = String(stage || "unknown");
  rdtActiveOperation.stageAt = new Date().toISOString();
  if (details && typeof details === "object") {
    // Only allow explicitly supplied scalar operational metadata.
    for (const [key, value] of Object.entries(details)) {
      if (value === null || ["string", "number", "boolean"].includes(typeof value)) {
        rdtActiveOperation.details[key] = value;
      }
    }
  }
}

function rdtBeginOperation(id, type, payload) {
  if (type === "diagnostics") return;
  const now = Date.now();
  const safePayload = payload && typeof payload === "object" ? payload : {};
  rdtActiveOperation = {
    id: String(id || ""),
    type: String(type || "unknown"),
    status: "running",
    stage: type === "load" ? "load-request-received" : type === "run" ? "run-request-received" : "request-received",
    startedAt: new Date(now).toISOString(),
    startedAtMs: now,
    stageAt: new Date(now).toISOString(),
    details: {}
  };
  if (type === "load") {
    rdtSetOperationStage("load-request-received", {
      modelId: safePayload.modelId || null,
      requestedDevice: safePayload.device || "webgpu",
      variant: safePayload.variant || null,
      revision: safePayload.revision || null,
      expectedFileCount: Array.isArray(safePayload.expected) ? safePayload.expected.length : null
    });
  } else if (type === "run") {
    const audio = safePayload.audio;
    rdtSetOperationStage("run-request-received", {
      selectedLanguage: typeof safePayload.language === "string" ? safePayload.language : "auto",
      audioInputType: audio && audio.constructor ? audio.constructor.name : typeof audio,
      audioSampleCount: audio && Number.isFinite(audio.length) ? Number(audio.length) : null
    });
  }
  rdtLastProgress = null;
}

function rdtCompleteOperation(id, status) {
  if (!rdtActiveOperation || rdtActiveOperation.id !== String(id || "")) return;
  const now = Date.now();
  rdtActiveOperation.status = String(status || "complete");
  rdtActiveOperation.completedAt = new Date(now).toISOString();
  rdtActiveOperation.elapsedMs = Math.max(0, now - Number(rdtActiveOperation.startedAtMs || now));
  delete rdtActiveOperation.startedAtMs;
  rdtLastOperation = { ...rdtActiveOperation, details: { ...(rdtActiveOperation.details || {}) } };
  rdtActiveOperation = null;
}

function rdtWasmSimdSupported() {
  try {
    if (typeof WebAssembly === "undefined" || typeof WebAssembly.validate !== "function") return null;
    // Valid minimal SIMD module (v128.const / drop).
    return WebAssembly.validate(new Uint8Array([
      0x00,0x61,0x73,0x6d,0x01,0x00,0x00,0x00,
      0x01,0x04,0x01,0x60,0x00,0x00,
      0x03,0x02,0x01,0x00,
      0x0a,0x09,0x01,0x07,0x00,0x41,0x00,0xfd,0x0f,0x1a,0x0b
    ]));
  } catch { return null; }
}

function rdtSafeErrorDetails(err, depth = 0) {
  const type = err === null ? "null" : typeof err;
  const out = { type, message: String(err && err.message !== undefined ? err.message : err).slice(0, 2400) };
  if (err && typeof err === "object") {
    if (err.name != null) out.name = String(err.name).slice(0, 160);
    if (err.stack != null) out.stack = String(err.stack).slice(0, 7000);
    if (/^\d+$/.test(out.message)) {
      const n = Number(out.message);
      out.numericValue = Number.isSafeInteger(n) ? n : null;
      out.numericHex = Number.isSafeInteger(n) && n <= 0xffffffff ? "0x" + (n >>> 0).toString(16) : null;
    }
    for (const key of ["code", "errno", "status", "statusCode", "causeCode"]) {
      try { if (err[key] !== undefined && ["string", "number", "boolean"].includes(typeof err[key])) out[key] = err[key]; } catch {}
    }
    if (depth < 2 && err.cause !== undefined && err.cause !== err) out.cause = rdtSafeErrorDetails(err.cause, depth + 1);
  } else if (type === "number") {
    out.numericValue = err;
    out.numericHex = Number.isFinite(err) && Number.isInteger(err) ? "0x" + (err >>> 0).toString(16) : null;
  } else if (type === "string" && /^\d+$/.test(err)) {
    const n = Number(err);
    out.numericValue = Number.isSafeInteger(n) ? n : null;
    out.numericHex = Number.isSafeInteger(n) && n <= 0xffffffff ? "0x" + (n >>> 0).toString(16) : null;
  }
  return out;
}

function rdtDiagnosticSnapshot(includeFailure = true) {
  const nav = typeof navigator !== "undefined" ? navigator : null;
  const active = rdtActiveOperation ? { ...rdtActiveOperation, details: { ...(rdtActiveOperation.details || {}) } } : null;
  const last = rdtLastOperation ? { ...rdtLastOperation, details: { ...(rdtLastOperation.details || {}) } } : null;
  return {
    schema: "rdt-easywhisper-worker-diagnostic-v1",
    capturedAt: new Date().toISOString(),
    workerVersion: WORKER_VERSION,
    runtime: rdtRuntimeDiagnosticInfo(),
    environment: {
      userAgent: nav && nav.userAgent ? String(nav.userAgent).slice(0, 500) : null,
      platform: nav && nav.platform ? String(nav.platform).slice(0, 120) : null,
      language: nav && nav.language ? String(nav.language).slice(0, 40) : null,
      deviceMemoryGiB: nav && Number.isFinite(Number(nav.deviceMemory)) ? Number(nav.deviceMemory) : null,
      hardwareConcurrency: nav && Number.isFinite(Number(nav.hardwareConcurrency)) ? Number(nav.hardwareConcurrency) : null,
      isSecureContext: typeof self.isSecureContext === "boolean" ? self.isSecureContext : null,
      crossOriginIsolated: typeof self.crossOriginIsolated === "boolean" ? self.crossOriginIsolated : null,
      sharedArrayBufferAvailable: typeof SharedArrayBuffer !== "undefined",
      wasmSIMD: rdtWasmSimdSupported(),
      architectureHint: rdtBrowserArchitectureHint(),
      nativeAndroidABIAvailableToBrowser: false,
      runtimeKind: rdtActiveRuntimeKind,
      webgpuApiAvailable: !!(nav && nav.gpu),
      webgpuAdapterProbe: webGPUAdapterAvailability === null ? "not-tested" : (webGPUAdapterAvailability ? "available" : "no-usable-adapter"),
      webgpuAdapterProbeReason: webGPUAdapterProbeReason,
      cacheStorageAvailable: typeof caches !== "undefined"
    },
    model: {
      loadedModelId: currentModelId,
      effectiveDevice: currentDevice,
      lastLoad: lastLoad ? {
        modelId: lastLoad.modelId || null,
        revision: lastLoad.revision || null,
        requestedDevice: lastLoad.requestedDevice || lastLoad.device || null,
        effectiveDevice: lastLoad.effectiveDevice || lastLoad.device || null,
        variant: lastLoad.variant || null,
        dtype: lastLoad.dtype || null,
        wasmExecutionProfile: lastLoad.wasmExecutionProfile || rdtCurrentWasmExecutionProfile,
        webgpuPowerPreference: lastLoad.webgpuPowerPreference || null,
        gpuSelectionStrategy: lastLoad.gpuSelectionStrategy || "high-performance-then-low-power-then-wasm",
        runtimeKind: lastLoad.runtimeKind || null,
        transformersPackage: lastLoad.transformersPackage || null,
        onnxRuntimePackage: lastLoad.onnxRuntimePackage || null,
        runtimeSelectionReason: lastLoad.runtimeSelectionReason || null,
        wasmSIMDSupported: typeof lastLoad.wasmSIMDSupported === "boolean" ? lastLoad.wasmSIMDSupported : null
      } : null,
      cache: rdtModelCacheSummary ? { ...rdtModelCacheSummary } : null,
      wasmPreferredRevisionCount: wasmPreferredModels.size,
      wasmExecutionProfile: rdtCurrentWasmExecutionProfile,
      webgpuSelectionStrategy: "high-performance-then-low-power-then-wasm",
      runtimeKind: rdtActiveRuntimeKind,
      transformersPackage: rdtRuntimeDiagnosticInfo().transformersPackage,
      onnxRuntimePackage: rdtRuntimeDiagnosticInfo().onnxRuntimePackage,
      runtimeSelectionReason: rdtActiveRuntimeSelectionReason,
      architectureHint: rdtBrowserArchitectureHint(),
      nativeAndroidABIAvailableToBrowser: false,
      webgpuPowerPreference: rdtWebGPUSelectedPowerPreference,
      webgpuAdapterInfo: rdtWebGPUAdapterInfo ? { ...rdtWebGPUAdapterInfo } : null,
      webgpuAdapterAttempts: rdtWebGPUAdapterAttempts.map(a => ({ ...a })),
      webgpuFailedPreferences: Array.from(rdtWebGPUFailedPreferences)
    },
    runtimeCapture: {
      lastObservedExecutionError: rdtObservedRuntimeError,
      lastObservedModelInputSignatures: rdtObservedModelInputs,
      browserCacheCoordination: { ...rdtCacheCoordinationInfo, lastAction: rdtCacheCoordinationInfo.lastAction ? { ...rdtCacheCoordinationInfo.lastAction } : null },
      onnxArtifactCacheCheck: rdtLastOnnxArtifactCacheCheck ? JSON.parse(JSON.stringify(rdtLastOnnxArtifactCacheCheck)) : null
    },
    operation: active || last,
    activeOperation: active,
    lastOperation: last,
    lastProgress: rdtLastProgress ? { ...rdtLastProgress } : null,
    lastFailure: includeFailure && rdtLastFailure ? rdtLastFailure : null
  };
}

function rdtRecordFailure(err, id, type) {
  const diagnosticId = "EW-" + Date.now().toString(36).toUpperCase() + "-" + Math.random().toString(36).slice(2, 7).toUpperCase();
  const diag = {
    diagnosticId,
    operationId: String(id || ""),
    failedRequestType: String(type || "unknown"),
    ...rdtDiagnosticSnapshot(false),
    error: rdtSafeErrorDetails(err)
  };
  rdtLastFailure = diag;
  console.error("[RDT worker] structured diagnostic", diag);
  return diag;
}

// A real WebGPU exception switches this model revision to WASM for this worker
// session. The next page/worker session can retry WebGPU; warnings alone never
// change this set because ONNX Runtime may legitimately place some ops on CPU.
const wasmPreferredModels = new Set();
// Tiny revisions on affected legacy Android devices are assigned the conservative profile
// before the first pipeline is built. Runtime-discovered faults are remembered as well, so
// the worker never repeatedly builds a second heavy pipeline after a failed run.
const rdtWasmCompatibilityPreferredModels = new Set();
let rdtCurrentWasmExecutionProfile = "not-wasm";
// WebGPU preference order: high-performance first (normally the browser's discrete/high-throughput choice),
// then low-power (often an integrated GPU), and only then WASM/CPU. WebGPU does not expose a standard API to
// enumerate every physical adapter, so these are browser selection profiles, not a list of every discrete GPU.
let webGPUAdapterAvailability = null;
let webGPUAdapterProbeReason = "not-tested";
let rdtWebGPUSelectedPowerPreference = null;
let rdtWebGPUAdapterInfo = null;
const rdtWebGPUAdapterAttempts = [];
const rdtWebGPUAttemptedPreferences = new Set();
const rdtWebGPUFailedPreferences = new Set();
let rdtLastWebGPUFallbackError = null;
const RDT_WEBGPU_PROBE_TIMEOUT_MS = 4500;
async function rdtAwaitGPUResult(promise, label, destroyLateValue=false) {
  let timer = null;
  let timedOut = false;
  const work = Promise.resolve(promise).then(value => {
    if (timedOut && destroyLateValue && value && typeof value.destroy === "function") {
      try { value.destroy(); } catch {}
    }
    return value;
  });
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      timedOut = true;
      reject(new Error(label + " timed out"));
    }, RDT_WEBGPU_PROBE_TIMEOUT_MS);
  });
  try { return await Promise.race([work, timeout]); }
  finally { if (timer !== null) clearTimeout(timer); }
}
function rdtSafeGPUAdapterInfo(adapter) {
  let info = null;
  try { info = adapter && adapter.info ? adapter.info : null; } catch {}
  const safeText = value => typeof value === "string" && value.trim() ? value.trim().slice(0, 100) : null;
  let fallback = null;
  try { if (typeof adapter.isFallbackAdapter === "boolean") fallback = adapter.isFallbackAdapter; } catch {}
  if (fallback === null) { try { if (info && typeof info.isFallbackAdapter === "boolean") fallback = info.isFallbackAdapter; } catch {} }
  return {
    vendor: safeText(info && info.vendor),
    architecture: safeText(info && info.architecture),
    device: safeText(info && info.device),
    description: safeText(info && info.description),
    isFallbackAdapter: fallback
  };
}
function rdtGpuAttemptSummary(preference, outcome, details = {}) {
  const summary = { at: new Date().toISOString(), powerPreference: preference, outcome: String(outcome).slice(0, 80) };
  if (details && details.reason) summary.reason = String(details.reason).slice(0, 140);
  if (details && details.adapterInfo) summary.adapterInfo = { ...details.adapterInfo };
  return summary;
}
async function rdtSelectWebGPUAdapter(powerPreference) {
  const preference = powerPreference === "low-power" ? "low-power" : "high-performance";
  const onnxEnv = env && env.backends && env.backends.onnx;
  const onnxWebGPU = onnxEnv && onnxEnv.webgpu;
  if (rdtWebGPUSelectedPowerPreference === preference && !rdtWebGPUFailedPreferences.has(preference) && onnxWebGPU && onnxWebGPU.adapter) {
    return true;
  }
  if (rdtWebGPUAttemptedPreferences.has(preference)) return false;
  rdtWebGPUAttemptedPreferences.add(preference);
  if (typeof navigator === "undefined" || !navigator.gpu || typeof navigator.gpu.requestAdapter !== "function") {
    rdtWebGPUAdapterAttempts.push(rdtGpuAttemptSummary(preference, "unavailable", { reason: "webgpu-api-unavailable" }));
    webGPUAdapterProbeReason = "webgpu-api-unavailable";
    return false;
  }
  let adapter = null;
  try {
    adapter = await rdtAwaitGPUResult(navigator.gpu.requestAdapter({ powerPreference: preference }), "WebGPU " + preference + " adapter request");
  } catch (err) {
    const reason = /timed out/i.test(String(err && err.message || err)) ? "adapter-request-timeout" : "adapter-request-error";
    rdtWebGPUAdapterAttempts.push(rdtGpuAttemptSummary(preference, "unavailable", { reason }));
    webGPUAdapterProbeReason = reason;
    return false;
  }
  if (!adapter) {
    rdtWebGPUAdapterAttempts.push(rdtGpuAttemptSummary(preference, "unavailable", { reason: "adapter-not-available" }));
    webGPUAdapterProbeReason = "adapter-not-available";
    return false;
  }
  const adapterInfo = rdtSafeGPUAdapterInfo(adapter);
  if (adapterInfo.isFallbackAdapter === true) {
    // Do not select a known software/fallback adapter ahead of the other hardware preference.
    rdtWebGPUAdapterAttempts.push(rdtGpuAttemptSummary(preference, "software-fallback-skipped", { reason: "known-fallback-adapter", adapterInfo }));
    webGPUAdapterProbeReason = "known-fallback-adapter";
    return false;
  }
  if (typeof adapter.requestDevice !== "function") {
    rdtWebGPUAdapterAttempts.push(rdtGpuAttemptSummary(preference, "unavailable", { reason: "adapter-cannot-request-device", adapterInfo }));
    webGPUAdapterProbeReason = "adapter-cannot-request-device";
    return false;
  }
  let device = null;
  try {
    device = await rdtAwaitGPUResult(adapter.requestDevice(), "WebGPU " + preference + " device request", true);
    if (!device) throw new Error("device-request-returned-empty");
  } catch (err) {
    const reason = /timed out/i.test(String(err && err.message || err)) ? "device-request-timeout" : "device-request-error";
    rdtWebGPUAdapterAttempts.push(rdtGpuAttemptSummary(preference, "unavailable", { reason, adapterInfo }));
    webGPUAdapterProbeReason = reason;
    return false;
  } finally {
    try { if (device && typeof device.destroy === "function") device.destroy(); } catch {}
  }
  try {
    if (!onnxEnv || typeof onnxEnv !== "object") throw new Error("onnx-webgpu-config-unavailable");
    // ONNX Runtime Web 1.22 supports binding the exact adapter before the first WebGPU session.
    // This avoids probing one adapter and then letting the runtime silently choose another.
    if (!onnxEnv.webgpu || typeof onnxEnv.webgpu !== "object") onnxEnv.webgpu = {};
    onnxEnv.webgpu.powerPreference = preference;
    onnxEnv.webgpu.adapter = adapter;
    if (onnxEnv.webgpu.adapter !== adapter) throw new Error("adapter-binding-failed");
  } catch (err) {
    const reason = String(err && err.message || err || "onnx-webgpu-config-unavailable").slice(0, 140);
    rdtWebGPUAdapterAttempts.push(rdtGpuAttemptSummary(preference, "binding-failed", { reason, adapterInfo }));
    webGPUAdapterProbeReason = "onnx-webgpu-adapter-binding-failed";
    return false;
  }
  rdtWebGPUSelectedPowerPreference = preference;
  rdtWebGPUAdapterInfo = adapterInfo;
  webGPUAdapterAvailability = true;
  webGPUAdapterProbeReason = preference === "high-performance" ? "selected-high-performance-adapter" : "selected-low-power-adapter";
  rdtWebGPUAdapterAttempts.push(rdtGpuAttemptSummary(preference, "selected", { adapterInfo }));
  return true;
}
async function rdtHasUsableWebGPUAdapter() {
  if (rdtWebGPUSelectedPowerPreference && !rdtWebGPUFailedPreferences.has(rdtWebGPUSelectedPowerPreference)) return true;
  if (await rdtSelectWebGPUAdapter("high-performance")) return true;
  if (await rdtSelectWebGPUAdapter("low-power")) return true;
  webGPUAdapterAvailability = false;
  webGPUAdapterProbeReason = "no-usable-adapter-after-high-performance-and-low-power";
  return false;
}
function modelRevisionKey(modelId, revision) { return String(modelId || "") + "@" + String(revision || ""); }

function rdtIsRemoteFetchFailure(err) {
  const s = String(err && err.message || err || "");
  return /failed to fetch|networkerror|network request failed|fetch failed|load failed|cors policy|access-control-allow-origin|net::err_|model relay returned HTTP (?:403|404)|HTTP 40[0-9]/i.test(s);
}
function rdtIsMemoryPressureFailure(err) {
  const s = String(err && err.message || err || "");
  return /out of memory|memory allocation|allocation failed|could not allocate|memory limit|arraybuffer allocation/i.test(s);
}


function rdtIsArm32AndroidTinyWasmDevice(modelId) {
  const nav = typeof navigator !== "undefined" ? navigator : null;
  if (!nav) return false;
  const ua = String(nav.userAgent || "");
  const platform = String(nav.platform || "");
  const arch = platform + " " + ua;
  const android = /Android\s+(?:[0-9]|1[0-2])(?:[.;\s)]|$)/i.test(ua);
  const armv7 = /armv7l|armeabi-v7a/i.test(arch) && !/arm64|aarch64/i.test(arch);
  const tiny = /(?:^|\/)whisper-tiny(?:$|-)/i.test(String(modelId || ""));
  return !!(tiny && android && armv7);
}

function rdtIsLegacyArmAndroidTinyWasmDevice(modelId) {
  const nav = typeof navigator !== "undefined" ? navigator : null;
  if (!nav || !rdtIsArm32AndroidTinyWasmDevice(modelId)) return false;
  const memoryGiB = Number(nav.deviceMemory);
  const noSharedMemory = typeof SharedArrayBuffer === "undefined" || !(typeof self !== "undefined" && self.crossOriginIsolated === true);
  return !!(memoryGiB > 0 && memoryGiB <= 2 && noSharedMemory);
}

function rdtIsLegacyArmAndroidTinyWasmFault(modelId, err) {
  if (!rdtIsArm32AndroidTinyWasmDevice(modelId)) return false;
  const errorText = String(err && err.message !== undefined ? err.message : err);
  const rawWasmFault = (typeof err === "number" && Number.isFinite(err)) ||
    (typeof WebAssembly !== "undefined" && typeof WebAssembly.RuntimeError === "function" && err instanceof WebAssembly.RuntimeError) ||
    /memory access out of bounds|wasm.*(?:trap|abort|unreachable)/i.test(errorText) ||
    /an error occurred during model execution:\s*[\"']?\d+[\"']?/i.test(errorText);
  return !!rawWasmFault;
}
function rdtShouldUseAndroidTinyUint8(modelId) {
  const nav = typeof navigator !== "undefined" ? navigator : null;
  if (!nav || !/Android/i.test(String(nav.userAgent || ""))) return false;
  if (!/^onnx-community\/whisper-tiny$/i.test(String(modelId || ""))) return false;
  // The reported Firefox profile cannot create a Q8 session without SIMD.
  // The LG ARM32/2-GiB profile has SIMD but fails during Q8 inference; use the
  // pinned INT8 pair only for that specific low-memory Android architecture.
  return rdtWasmSimdSupported() !== true || rdtIsLegacyArmAndroidTinyWasmDevice(modelId);
}

function pickDtype(device, modelId, variant) {

  // INT8 is scoped to affected Android Tiny/WASM profiles. Modern Android Tiny
  // and other WASM models retain Q8; non-Android Tiny keeps its existing FP32
  // selection so this targeted fix does not change Apple/Windows behavior.
  // WebGPU precision choices below are intentionally unchanged.
  if (device !== "webgpu") {
    if (device === "wasm" && rdtShouldUseAndroidTinyUint8(modelId)) return "uint8";
    if (device === "wasm" && /^onnx-community\/whisper-tiny$/i.test(String(modelId || "")) && !(typeof navigator !== "undefined" && /Android/i.test(String(navigator.userAgent || "")))) return "fp32";
    return "q8";
  }

  // Use the q4 Tiny files present in the pinned manifest. q4f16 is not published
  // at this revision; requesting those paths caused a model-relay HTTP 404.

  if (variant === "mobile-lite" && /(?:^|\/)whisper-tiny(?:$|-)/i.test(modelId || "")) {

    return { encoder_model: "q4", decoder_model_merged: "q4" };

  }

  if (variant === "mobile-standard" && /(?:^|\/)whisper-tiny(?:$|-)/i.test(modelId || "")) {

    return { encoder_model: "fp16", decoder_model_merged: "q4" };

  }

  if (/whisper-large-v3-turbo/i.test(modelId || "")) {

    return { encoder_model: "fp16", decoder_model_merged: "q4" };

  }

  return { encoder_model: "fp32", decoder_model_merged: "q4" };

}

function sessionOptionsForDevice(device, wasmExecutionProfile = "standard") {
  // Only surface ONNX Runtime errors (placement notices are expected and harmless).
  const base = sessionOptionsForDeviceInner(device, wasmExecutionProfile);
  return { ...base, logSeverityLevel: 3 };
}
function sessionOptionsForDeviceInner(device, wasmExecutionProfile = "standard") {

  if (device === "wasm") {

    return {

      enableMemPattern: false,

      enableCpuMemArena: false,

      executionMode: "sequential",

      // Older 32-bit Android WASM runtimes get one retry at BASIC optimization
      // if the normal profile throws a raw WASM numeric/runtime failure.
      graphOptimizationLevel: wasmExecutionProfile === "legacy-android-basic" ? "basic" : "all"

    };

  }

  return { enableMemPattern: false };

}

async function rdtDisposeActivePipelineForRetry() {
  const activePipeline = transcriber;
  // Detach first so no later request can reuse a failed session. If disposal is
  // unsupported or fails, the caller must not allocate a second session.
  transcriber = null;
  currentModelId = null;
  currentDevice = null;
  rdtCurrentWasmExecutionProfile = "not-wasm";
  if (!activePipeline) return;
  const disposer = typeof activePipeline.dispose === "function"
    ? () => activePipeline.dispose()
    : activePipeline.model && typeof activePipeline.model.dispose === "function"
      ? () => activePipeline.model.dispose()
      : null;
  if (!disposer) throw new Error("active-onnx-pipeline-dispose-unavailable; refusing a second compatibility session");
  let timer;
  try {
    await Promise.race([
      Promise.resolve().then(disposer),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("active-onnx-pipeline-dispose-timeout; refusing a second compatibility session")), 5000); })
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function buildPipeline(id, modelId, revision, device, variant, wasmExecutionProfile = "standard", runtimeOverride = null) {

  await rdtPrepareRuntimeForDevice(device, runtimeOverride === "legacy-non-simd", runtimeOverride === "legacy-non-simd" ? "legacy-arm32-inference-compatibility-retry" : null, modelId);
  const legacyRuntime = rdtIsLegacyNonSimdRuntime();
  const opts = {

    session_options: sessionOptionsForDevice(device, wasmExecutionProfile),

    progress_callback: (p) => reply({

      id,

      type: "progress",

      phase: p && p.status,

      file: p && p.file,

      loaded: p && p.loaded,

      total: p && p.total,

      progress: p && p.progress

    })

  };

  if (legacyRuntime) {
    // Keep Whisper support from Transformers.js v3. dtypeSuffix and pickDtype select
    // the pinned INT8 pair only for affected Android Tiny/WASM profiles.
    // The older v2 fallback failed on this model with "Unsupported model type: whisper".
    opts.dtype = pickDtype("wasm", modelId, variant);
    opts.device = "wasm";
  } else {
    opts.dtype = pickDtype(device, modelId, variant);
    opts.device = device;
  }

  opts.revision = revision;
  if (lastLoad) {
    const runtimeInfo = rdtRuntimeDiagnosticInfo();
    lastLoad.runtimeKind = runtimeInfo.runtimeKind;
    lastLoad.transformersPackage = runtimeInfo.transformersPackage;
    lastLoad.onnxRuntimePackage = runtimeInfo.onnxRuntimePackage;
    lastLoad.runtimeSelectionReason = runtimeInfo.selectionReason;
    lastLoad.wasmSIMDSupported = runtimeInfo.wasmSIMDSupported;
    lastLoad.architectureHint = runtimeInfo.architectureHint;
    lastLoad.nativeAndroidABIAvailableToBrowser = false;
  }
  // On legacy WASM only, make sure each selected ONNX artifact that is absent
  // from Cache Storage is fully downloaded, size-checked, and committed under
  // its original Hugging Face cache key before ONNX Runtime opens a session.
  // This closes the missing-file/concurrent-download gap without re-reading or
  // re-downloading already-present files on every load. Modern GPU paths bypass it.
  if (legacyRuntime && device === "wasm") {
    await rdtEnsureLegacyWasmArtifactsCached(id, modelId, revision, variant, rdtActiveExpectedModelFiles);
    reply({ id, type: "progress", phase: "model-file-prefetch-complete", progress: 100,
      note: "Selected ONNX files were checked. Initializing the inference session; the model is not ready until this step succeeds." });
  }
  transcriber = await rdtActivePipelineFactory("automatic-speech-recognition", modelId, opts);
  currentDevice = device;
  rdtCurrentWasmExecutionProfile = device === "wasm" ? (legacyRuntime ? "legacy-non-simd" : (wasmExecutionProfile === "legacy-android-basic" ? "legacy-android-basic" : "standard")) : "not-wasm";
  currentModelId = modelId;

}

async function rdtTryLowerPowerGpuPipeline(id, modelId, revision, variant) {
  if (rdtWebGPUSelectedPowerPreference !== "high-performance") return { attempted: false, ok: false, reason: "high-performance-profile-not-active" };
  rdtWebGPUFailedPreferences.add("high-performance");
  if (!await rdtSelectWebGPUAdapter("low-power")) {
    return { attempted: true, ok: false, reason: "low-power-adapter-unavailable" };
  }
  reply({ id, type: "progress", phase: "fallback", fallbackReason: "webgpu-low-power-retry", note: "The high-performance GPU path failed. Trying the browser's lower-power WebGPU adapter before CPU/WASM." });
  transcriber = null; currentModelId = null; currentDevice = null;
  try {
    await buildPipeline(id, modelId, revision, "webgpu", variant);
    if (lastLoad) {
      lastLoad.variant = variant;
      lastLoad.dtype = pickDtype("webgpu", modelId, variant);
      lastLoad.effectiveDevice = "webgpu";
      lastLoad.webgpuPowerPreference = "low-power";
      lastLoad.gpuSelectionStrategy = "high-performance-then-low-power-then-wasm";
    }
    rdtSetOperationStage("gpu-fallback-session-ready", { effectiveDevice: "webgpu", webgpuPowerPreference: "low-power" });
    return { attempted: true, ok: true };
  } catch (error) {
    rdtWebGPUFailedPreferences.add("low-power");
    rdtLastWebGPUFallbackError = String(error && error.message !== undefined ? error.message : error).slice(0, 220);
    rdtSetOperationStage("gpu-fallback-session-failed", { effectiveDevice: "webgpu", webgpuPowerPreference: "low-power", fallbackError: rdtLastWebGPUFallbackError });
    transcriber = null; currentModelId = null; currentDevice = null;
    return { attempted: true, ok: false, error, reason: "low-power-session-failed" };
  }
}

function reply(msg) {
  if (msg && msg.type === "progress") {
    const safe = { at: new Date().toISOString(), phase: String(msg.phase || "progress") };
    if (Number.isFinite(Number(msg.progress))) safe.progressPercent = Math.max(0, Math.min(100, Number(msg.progress)));
    if (Number.isFinite(Number(msg.batchIndex))) safe.batchIndex = Number(msg.batchIndex);
    if (Number.isFinite(Number(msg.batchTotal))) safe.batchTotal = Number(msg.batchTotal);
    rdtLastProgress = safe;
    if (rdtActiveOperation && rdtActiveOperation.id === String(msg.id || "")) {
      rdtActiveOperation.stage = safe.phase;
      rdtActiveOperation.stageAt = safe.at;
    }
  }
  self.postMessage(msg);
}

let __opQueue = Promise.resolve();

function enqueue(fn){

  const next = __opQueue.then(fn, fn);

  __opQueue = next.catch(()=>{});

  return next;

}

self.onmessage = (ev) => {

  const { id, type, payload } = ev.data || {};
  // Diagnostic snapshots bypass the inference queue and contain metadata only.
  // They can respond between worker event-loop tasks; they cannot interrupt synchronous WASM computation.
  if (type === "diagnostics") {
    reply({ id, type: "diagnostics", diagnostic: rdtDiagnosticSnapshot() });
    return;
  }
  const run = async () => {

    if (type === "load" || type === "run") rdtBeginOperation(id, type, payload || {});
    try {

      if (type === "load")         await handleLoad(id, payload || {});

      else if (type === "run")     await handleRun(id, payload || {});

      else if (type === "cache-status") {
        const p = payload || {};
        try {
          const modelId = String(p.modelId || "");
          const revision = String(p.revision || "");
          if (!modelId || !/^[0-9a-f]{40}$/i.test(revision)) throw new Error("pinned-model-revision-required");
          const device = p.device || "wasm";
          const variant = p.variant || null;
          const files = expectedFilesForVariant(modelId, device, variant, p.expected);
          const inspection = await inspectModelCache(modelId, revision, files);
          const manifestFiles = Array.isArray(files) ? files.filter(f => f && f.path) : [];
          const metadataListed = manifestFiles.some(f => !String(f.path).startsWith("onnx/"));
          const expectedFileCount = manifestFiles.length + (metadataListed ? 0 : REQUIRED_META.length);
          const missing = Array.isArray(inspection.missing) ? inspection.missing : [];
          reply({ id, type: "cache-status", cacheStatus: {
            modelId, revision, device, variant,
            state: String(inspection.state || "unknown"),
            expectedFileCount,
            presentFileCount: Math.max(0, expectedFileCount - missing.length),
            missingFileCount: missing.length,
            fullyCached: inspection.state === "full",
            missing: missing.slice(0, 12),
            checkedAt: new Date().toISOString(),
            readOnly: true
          }});
        } catch (error) {
          reply({ id, type: "cache-status", cacheStatus: {
            modelId: String(p.modelId || "").slice(0, 120), state: "unknown", fullyCached: false,
            checkedAt: new Date().toISOString(), readOnly: true,
            error: String(error && error.message || error).slice(0, 180)
          }});
        }
      }

      else if (type === "dispose") {

        transcriber = null;

        currentModelId = null;

        currentDevice = null;

        lastLoad = null;
        wasmPreferredModels.clear();

        reply({ id, type: "disposed" });

      } else reply({ id, type: "error", error: "unknown message type: " + type });

    } catch (err) {

      if (type === "load" || type === "run") { rdtSetOperationStage("operation-failed"); if (rdtActiveOperation) rdtActiveOperation.status = "failed"; }
      const diagnostic = rdtRecordFailure(err, id, type);
      if (type === "load" || type === "run") rdtCompleteOperation(id, "failed");
      const rawMessage = (err && err.message !== undefined) ? String(err.message) : String(err);
      const errorMessage = /^\d+$/.test(rawMessage.trim())
        ? "The inference runtime returned an opaque numeric error (" + rawMessage + "). Open Developer Options → EasyWhisper Diagnostics and copy report " + diagnostic.diagnosticId + "."
        : rawMessage;
      reply({ id, type: "error", error: errorMessage, diagnostic });
      return;

    }

    if (type === "load" || type === "run") rdtCompleteOperation(id, "succeeded");
  };

  enqueue(run);

};

function cacheUrlFor(modelId, revision, path) {

  return "https://huggingface.co/" + modelId + "/resolve/" + revision + "/" + path;

}

async function cacheHasPath(cache, modelId, revision, path) {

  const exactUrl = cacheUrlFor(modelId, revision, path);

  let resp = await cache.match(exactUrl);

  if (resp) return true;

  const needle = "/resolve/" + revision + "/" + path;

  const keys = await cache.keys();

  return keys.some(req => req.url.indexOf(needle) !== -1);

}

// --- OFFLINE-FIRST CHECK ---

// With a manifest, "full" means every expected file is present in the browser cache.

// Without one, we conservatively report only a metadata hit; we do not pretend the

// model is fully cached when we cannot prove it.

const REQUIRED_META = ["config.json", "tokenizer.json", "tokenizer_config.json", "preprocessor_config.json"];

async function inspectModelCache(modelId, revision, expected) {

  try {

    if (!self.caches) return { state: "unavailable", checked: 0, missing: [] };

    const cache = await caches.open("transformers-cache");

    if (Array.isArray(expected) && expected.length) {

      const files = expected.filter(f => f && f.path);

      if (!files.length) return { state: "unknown", checked: 0, missing: [] };

      let checked = 0;

      const missing = [];

      for (const f of files) {

        if (await cacheHasPath(cache, modelId, revision, f.path)) checked++;

        else missing.push(f.path);

      }

      // Even if manifest only listed ONNX files, we must also ensure critical metadata

      // is cached to truly be offline. If manifest omitted them, we check them here

      // and downgrade full -> partial with explicit missing list.

      const hasMetaInExpected = files.some(f => !String(f.path).startsWith("onnx/"));

      if (!hasMetaInExpected) {

        for (const meta of REQUIRED_META) {

          if (!(await cacheHasPath(cache, modelId, revision, meta))) {

            missing.push(meta + " (required but not listed in manifest)");

          }

        }

      }

      return {

        state: missing.length === 0 ? "full" : (checked > 0 ? "partial" : "miss"),

        checked,

        missing

      };

    }

    const keys = await cache.keys();

    const base = "/" + modelId + "/resolve/" + revision + "/";

    const metadataHit = keys.some(r => r.url.includes(base + "config.json"));

    const tokenizerHit = keys.some(r => r.url.includes(base + "tokenizer.json"));

    return {

      state: metadataHit && tokenizerHit ? "metadata" : "miss",

      checked: Number(metadataHit) + Number(tokenizerHit),

      missing: []

    };

  } catch {

    return { state: "unavailable", checked: 0, missing: [] };

  }

}

function dtypeSuffix(name) {

  if (name === "q4f16") return "_q4f16.onnx";

  if (name === "fp16") return "_fp16.onnx";

  if (name === "fp32") return ".onnx";

  if (name === "uint8") return "_uint8.onnx";

  if (name === "int8") return "_int8.onnx";

  if (name === "q4") return "_q4.onnx";

  return "_quantized.onnx";

}

function expectedFilesForVariant(modelId, device, variant, expected) {

  if (!Array.isArray(expected) || !expected.length) return expected;

  const dtype = pickDtype(device, modelId, variant);

  const encoder = typeof dtype === "string" ? dtype : dtype.encoder_model;

  const decoder = typeof dtype === "string" ? dtype : dtype.decoder_model_merged;

  const encPath = "onnx/encoder_model" + dtypeSuffix(encoder);

  const decPath = "onnx/decoder_model_merged" + dtypeSuffix(decoder);

  // The manifest lists all precision variants. For cache-first we must check:

  // - ALL non-ONNX files (config, tokenizer, preprocessor) are always required

  // - Only the two ONNX files chosen for this device/variant determine cache completeness

  return expected.filter(f => {

    if (!f || !f.path) return false;

    const p = String(f.path);

    if (!p.startsWith("onnx/")) return true;

    return p === encPath || p === decPath;

  });

}

async function handleLoad(id, payload) {

  const modelId  = payload.modelId;

  const revision = payload.revision;

  const requestedDevice = payload.device || "webgpu";

  const variant = payload.variant || null;

  const expected = payload.expected;
  rdtActiveExpectedModelFiles = Array.isArray(expected) ? expected : [];
  rdtLastOnnxArtifactCacheCheck = null;
  rdtCacheCoordinationInfo.actionCount = 0;
  rdtCacheCoordinationInfo.lastAction = null;
  const modelKey = modelRevisionKey(modelId, revision);
  rdtSetOperationStage("validate-model-request", { modelId: modelId || null, requestedDevice, variant: variant || null, revision: revision || null });
  const rememberedWasm = requestedDevice === "webgpu" && wasmPreferredModels.has(modelKey);
  let effectiveDevice = rememberedWasm ? "wasm" : requestedDevice;

  if (!modelId) throw new Error("load: missing modelId");
  if (rememberedWasm) {
    reply({ id, type: "progress", phase: "fallback", fallbackReason: "previous-webgpu-model-failure", note: "WebGPU previously failed for this model in this worker session. Using CPU/WASM for this attempt; large models may be substantially slower." });
  }

  if (!/^[0-9a-f]{40}$/i.test(String(revision || ""))) {

    throw new Error("load: pinned 40-hex revision required");

  }

  // Do this before cache/variant selection: a GPU-less VM needs the WASM model
  // files, not the WebGPU precision pair. Do not try WebGPU first just because
  // navigator.gpu exists; its requestAdapter() can still return null.
  if (effectiveDevice === "webgpu") {
    const adapterAvailable = await rdtHasUsableWebGPUAdapter();
    if (!adapterAvailable) {
      wasmPreferredModels.add(modelKey);
      effectiveDevice = "wasm";
      reply({ id, type: "progress", phase: "fallback", fallbackReason: webGPUAdapterProbeReason, note: "WebGPU is unavailable (" + webGPUAdapterProbeReason + "). Using CPU/WASM instead; large models may be substantially slower in this browser or virtual machine." });
    }
  }

  const cacheExpected = expectedFilesForVariant(modelId, effectiveDevice, variant, expected);

  rdtSetOperationStage("inspect-model-cache", { effectiveDevice, expectedFileCount: Array.isArray(cacheExpected) ? cacheExpected.length : null });
  const cache = await inspectModelCache(modelId, revision, cacheExpected);
  const cached = cache.state === "full";

  const online = typeof navigator === "undefined" ? true : navigator.onLine !== false;
  rdtModelCacheSummary = {
    state: String(cache.state || "unknown"),
    expectedFileCount: Array.isArray(cacheExpected) ? cacheExpected.length : null,
    presentFileCount: Number.isFinite(Number(cache.checked)) ? Number(cache.checked) : null,
    missingFileCount: Array.isArray(cache.missing) ? cache.missing.length : null,
    fullyCached: cached,
    online
  };
  rdtSetOperationStage("model-cache-inspected", { cacheState: rdtModelCacheSummary.state, fullyCached: cached, missingFileCount: rdtModelCacheSummary.missingFileCount, online });

  if (cache.state === "full") {

    reply({ id, type: "progress", phase: "cache-hit", note: "Found the complete model in local cache; loading offline", missing: [] });

  } else if (!online) {

    const missingList = cache.missing && cache.missing.length ? " Missing: " + cache.missing.slice(0, 12).join(", ") + (cache.missing.length > 12 ? " …+" + (cache.missing.length - 12) + " more" : "") : "";

    throw new Error(

      (cache.state === "partial"

        ? "This model is only partially cached. Connect once to finish the download." + missingList

        : "This model is not fully cached. Connect once to download it before using EasyWhisper offline." + missingList)

    );

  } else if (cache.state === "partial") {

    const missingList = cache.missing && cache.missing.length ? " Missing: " + cache.missing.slice(0, 12).join(", ") + (cache.missing.length > 12 ? " …+" + (cache.missing.length - 12) + " more" : "") : "";

    reply({ id, type: "progress", phase: "cache-partial", note: "Model partially cached (" + cache.checked + "/" + (cacheExpected ? cacheExpected.length : "?") + "). Fetching remaining files." + missingList, missing: cache.missing, checked: cache.checked });

  } else if (cache.state === "metadata") {

    reply({ id, type: "progress", phase: "cache-partial", note: "Model metadata is cached; checking remaining files", missing: [] });

  } else if (cache.state === "miss") {

    const missingList = cache.missing && cache.missing.length ? " Missing: " + cache.missing.slice(0, 12).join(", ") : "";

    reply({ id, type: "progress", phase: "cache-miss", note: "No complete local copy; downloading from Hugging Face." + (missingList ? missingList : ""), missing: cache.missing });

  } else {

    reply({ id, type: "progress", phase: "cache-miss", note: "Cache unavailable; downloading from Hugging Face", missing: [] });

  }

  if (transcriber && currentModelId === modelId && (currentDevice === requestedDevice || (rememberedWasm && currentDevice === "wasm"))) {

    reply({ id, type: "ready", modelId, device: currentDevice, cached: true, cacheState: "memory" });

    return;

  }

  transcriber = null;

  currentModelId = null;

  currentDevice = null;

  lastLoad = {
    modelId, revision, variant,
    device: requestedDevice,
    requestedDevice,
    effectiveDevice,
    dtype: pickDtype(effectiveDevice, modelId, variant),
    webgpuPowerPreference: effectiveDevice === "webgpu" ? rdtWebGPUSelectedPowerPreference : null,
    gpuSelectionStrategy: "high-performance-then-low-power-then-wasm",
    startedAt: new Date().toISOString()
  };
  rdtSetOperationStage("initialize-inference-pipeline", { requestedDevice, effectiveDevice, variant: variant || null, webgpuPowerPreference: lastLoad.webgpuPowerPreference, dtype: typeof lastLoad.dtype === "string" ? lastLoad.dtype : JSON.stringify(lastLoad.dtype) });

  const hasGpu = effectiveDevice === "webgpu" && typeof navigator !== "undefined" && !!navigator.gpu;
  // No WebGPU API is a capability result, not a failure: remember WASM for the
  // current model revision instead of repeatedly taking the same ineffective path.
  if (requestedDevice === "webgpu" && !hasGpu) wasmPreferredModels.add(modelKey);

  const initialDevice = hasGpu ? "webgpu" : "wasm";
  const runtimeEnv = await rdtPrepareRuntimeForDevice(initialDevice, false, null, modelId);
  if (initialDevice === "wasm" && rdtIsLegacyNonSimdRuntime()) {
    reply({ id, type: "progress", phase: "runtime-compatibility", fallbackReason: "wasm-simd-unavailable", note: "This browser does not support WebAssembly SIMD. Using the version-matched non-SIMD compatibility runtime for the pinned Whisper model." });
  }

  // Cache-first: if fully cached, forbid remote fetch during pipeline construction
  // to avoid unnecessary HEAD/GET to huggingface.co. Restore after.
  // Must keep allowLocalModels true, otherwise "both local and remote disabled" error.
  const prevAllowRemote = runtimeEnv.allowRemoteModels;
  const prevAllowLocal = runtimeEnv.allowLocalModels;
  let restoreRemoteAfter = false;
  if (cached) {
    runtimeEnv.allowRemoteModels = false;
    runtimeEnv.allowLocalModels = true;
    restoreRemoteAfter = true;
  }

  // Start with the normal profile on SIMD-capable Android, including the low-memory
  // ARM32 phone. Runtime fallback is evidence-driven and remains limited to one retry.
  // Browsers without SIMD already selected the scalar compatibility runtime above.
  const initialWasmProfile = initialDevice === "wasm" && rdtIsLegacyNonSimdRuntime()
    ? "legacy-android-basic" : "standard";
  if (lastLoad) lastLoad.webgpuPowerPreference = initialDevice === "webgpu" ? rdtWebGPUSelectedPowerPreference : null;
  // Record the attempted profile before pipeline construction so failed loads still identify it.
  lastLoad.wasmExecutionProfile = rdtIsLegacyNonSimdRuntime() ? "legacy-non-simd" : initialWasmProfile;

  try {

    try {

      await buildPipeline(id, modelId, revision, initialDevice, variant, initialWasmProfile);
      lastLoad.wasmExecutionProfile = rdtIsLegacyNonSimdRuntime() ? "legacy-non-simd" : initialWasmProfile;

    } finally {

      if (restoreRemoteAfter) {

        runtimeEnv.allowRemoteModels = prevAllowRemote;

        runtimeEnv.allowLocalModels = prevAllowLocal;

        restoreRemoteAfter = false;

      }

    }

  } catch (err) {

    if (restoreRemoteAfter) {

      runtimeEnv.allowRemoteModels = prevAllowRemote;

      runtimeEnv.allowLocalModels = prevAllowLocal;

    }

    const loadErrorMessage = String(err && err.message || err || "");
    let cacheIntegrityRecovered = false;
    // If a mobile Tiny variant is missing on the relay, try the published standard
    // Tiny WebGPU pair before declaring a download failure. Other 403/404/network
    // errors still fail promptly with their diagnostic details.
    const mobileVariant404 = hasGpu && variant === "mobile-lite" && /Model relay returned HTTP 404/i.test(loadErrorMessage);
    if (rdtIsRemoteFetchFailure(err) && !mobileVariant404) {
      throw new Error("EasyWhisper could not download this model. Your recording has not been transcribed yet. Check your connection and try again, or choose a model already downloaded on this device. If the problem continues, copy the technical details for support. Details: " + loadErrorMessage.slice(0,240));
    }
    if (rdtIsMemoryPressureFailure(err)) {
      throw new Error("WebGPU reported memory pressure while loading this model. Another GPU session was not allocated. Try a smaller model or free GPU memory first. Details: " + loadErrorMessage.slice(0,180));
    }

    const isOnnxProtobufFailure = /protobuf parsing failed/i.test(loadErrorMessage);
    if (!hasGpu && initialDevice === "wasm" && isOnnxProtobufFailure) {
      const previousArtifactCheck = rdtLastOnnxArtifactCacheCheck;
      // This is the error path, so perform one bounded-memory-impact full digest
      // of any present large selected model artifact. It distinguishes a valid-size
      // but corrupt decoder from an ONNX/runtime incompatibility.
      const deepHashLargeOnFailure = modelId === "onnx-community/whisper-tiny" && rdtIsLegacyNonSimdRuntime();
      rdtLastOnnxArtifactCacheCheck = await rdtInspectOnnxArtifactCache(modelId, revision, initialDevice, variant, expected, true, deepHashLargeOnFailure);
      rdtLastOnnxArtifactCacheCheck.failureAudit = {
        capturedAt: rdtLastOnnxArtifactCacheCheck.capturedAt,
        deepHashLargeOnFailure,
        strategy: rdtLastOnnxArtifactCacheCheck.strategy,
        allSelectedArtifactsMatchPinnedSha256: Array.isArray(rdtLastOnnxArtifactCacheCheck.files) && rdtLastOnnxArtifactCacheCheck.files.length > 0 && rdtLastOnnxArtifactCacheCheck.files.every(f => f.present === true && f.sizeMatches === true && f.sha256Checked === true && f.sha256Matches === true),
        files: (rdtLastOnnxArtifactCacheCheck.files || []).map(f => ({ path: f.path, present: f.present, sizeMatches: f.sizeMatches, sha256Checked: f.sha256Checked, sha256Matches: f.sha256Matches, cacheEntryEvicted: f.cacheEntryEvicted }))
      };
      if (rdtLastOnnxArtifactCacheCheck.failureAudit.allSelectedArtifactsMatchPinnedSha256) {
        rdtLastOnnxArtifactCacheCheck.failureAudit.interpretation = "Selected ONNX files match their pinned size and SHA-256 values; the protobuf error is not explained by a size/hash mismatch. Runtime/parser/model compatibility remains unresolved.";
      }
      if (previousArtifactCheck && previousArtifactCheck.preflight) {
        rdtLastOnnxArtifactCacheCheck.preflight = {
          ...previousArtifactCheck.preflight,
          failureAuditPerformed: true,
          failureAuditDeepHashLarge: deepHashLargeOnFailure,
          failureAuditAt: rdtLastOnnxArtifactCacheCheck.capturedAt
        };
        rdtLastOnnxArtifactCacheCheck.prefetchAttempts = previousArtifactCheck.prefetchAttempts || [];
        if (previousArtifactCheck.cacheStatusAfterPrefetch) rdtLastOnnxArtifactCacheCheck.cacheStatusAfterPrefetch = previousArtifactCheck.cacheStatusAfterPrefetch;
      }
      if (Array.isArray(rdtLastOnnxArtifactCacheCheck.evictedFiles) && rdtLastOnnxArtifactCacheCheck.evictedFiles.length) {
        rdtDeepHashLargeOnNextLegacyRetry = deepHashLargeOnFailure && rdtLastOnnxArtifactCacheCheck.evictedFiles.some(path => {
          const item = (rdtLastOnnxArtifactCacheCheck.files || []).find(f => f.path === path);
          return !!(item && Number(item.expectedBytes) > 12 * 1024 * 1024);
        });
        reply({ id, type: "progress", phase: "cache-integrity-recovery", fallbackReason: "invalid-cached-onnx-artifact", note: "A cached Whisper model file failed its expected-size or SHA-256 check. Removed only the invalid model file and retrying the load once." });
        rdtSetOperationStage("cache-integrity-recovery", { invalidCachedArtifactCount: rdtLastOnnxArtifactCacheCheck.evictedFiles.length });
        transcriber = null; currentModelId = null; currentDevice = null;
        const failureAudit = JSON.parse(JSON.stringify(rdtLastOnnxArtifactCacheCheck));
        try {
          await buildPipeline(id, modelId, revision, initialDevice, variant, initialWasmProfile);
          if (lastLoad) lastLoad.wasmExecutionProfile = rdtIsLegacyNonSimdRuntime() ? "legacy-non-simd" : initialWasmProfile;
          rdtLastOnnxArtifactCacheCheck.repairAudit = failureAudit;
          rdtLastOnnxArtifactCacheCheck.retryAttempted = true;
          rdtLastOnnxArtifactCacheCheck.retrySucceeded = true;
          cacheIntegrityRecovered = true;
        } catch (retryErr) {
          rdtLastOnnxArtifactCacheCheck.repairAudit = failureAudit;
          rdtLastOnnxArtifactCacheCheck.retryAttempted = true;
          rdtLastOnnxArtifactCacheCheck.retrySucceeded = false;
          rdtLastOnnxArtifactCacheCheck.retryError = String(retryErr && retryErr.message || retryErr).slice(0, 500);
          throw retryErr;
        }
      }
    }
    if (!cacheIntegrityRecovered && !hasGpu) throw err;

    if (!cacheIntegrityRecovered && variant === "mobile-lite") {

      reply({ id, type: "progress", phase: "fallback", note: "Light WebGPU variant unavailable; trying standard WebGPU" });

      transcriber = null; currentModelId = null; currentDevice = null;

      try {

        await buildPipeline(id, modelId, revision, "webgpu", "mobile-standard");
        lastLoad = { ...lastLoad, variant: "mobile-standard", dtype: pickDtype("webgpu", modelId, "mobile-standard"), effectiveDevice: "webgpu", webgpuPowerPreference: rdtWebGPUSelectedPowerPreference };

      } catch (gpuErr) {

        let lowerPowerRetry = { attempted: false, ok: false };
        if (!rdtIsRemoteFetchFailure(gpuErr)) lowerPowerRetry = await rdtTryLowerPowerGpuPipeline(id, modelId, revision, "mobile-standard");
        if (!lowerPowerRetry.ok) {
          wasmPreferredModels.add(modelKey);
          reply({ id, type: "progress", phase: "fallback", note: "Both Tiny WebGPU variants and the lower-power GPU fallback are exhausted or unavailable; switching to CPU/WASM: " + String(lowerPowerRetry.error && lowerPowerRetry.error.message || gpuErr && gpuErr.message || gpuErr).slice(0, 180) });
          transcriber = null; currentModelId = null; currentDevice = null;
          await buildPipeline(id, modelId, revision, "wasm", null, rdtWasmCompatibilityPreferredModels.has(modelKey) ? "legacy-android-basic" : "standard");
          lastLoad = { ...lastLoad, device: "wasm", effectiveDevice: "wasm", variant: null, dtype: pickDtype("wasm", modelId, null), webgpuPowerPreference: null };
        }

      }

    } else if (!cacheIntegrityRecovered) {

      const lowerPowerRetry = await rdtTryLowerPowerGpuPipeline(id, modelId, revision, variant);
      if (!lowerPowerRetry.ok) {
        wasmPreferredModels.add(modelKey);
        reply({ id, type: "progress", phase: "fallback", note: "High-performance and lower-power WebGPU attempts are exhausted or unavailable; switching to CPU/WASM for this session: " + String(lowerPowerRetry.error && lowerPowerRetry.error.message || err && err.message || err).slice(0, 180) });
        transcriber = null; currentModelId = null; currentDevice = null;
        await buildPipeline(id, modelId, revision, "wasm", null, rdtWasmCompatibilityPreferredModels.has(modelKey) ? "legacy-android-basic" : "standard");
        lastLoad = { ...lastLoad, device: "wasm", effectiveDevice: "wasm", variant: null, dtype: pickDtype("wasm", modelId, null), webgpuPowerPreference: null };
      }

    }

  }

  let verify = { checked: 0, skipped: 0, failed: [] };

  rdtSetOperationStage("verify-model-artifacts", { expectedFileCount: Array.isArray(expected) ? expected.length : 0 });
  if (expected && expected.length) {

    try {
      verify = await verifySmallFiles(modelId, revision, expected);
    } catch (e) {

      reply({ id, type: "progress", phase: "verify-error", note: String(e && e.message || e) });

    }

    if (verify.failed.length) {

      transcriber = null;

      currentModelId = null;

      currentDevice = null;

      throw new Error("Model integrity check failed: " + verify.failed.join(", "));

    }

  }

  rdtSetOperationStage("model-ready", { effectiveDevice: currentDevice, modelId: currentModelId, fullyCached: cached, verifiedFileCount: verify.checked, skippedVerificationCount: verify.skipped });
  reply({

    id,

    type: "ready",

    modelId,

    device: currentDevice,

    cached,

    verified: verify.checked,

    skipped: verify.skipped,

    cacheState: cached ? "disk-full" : (cache.state || "unknown"),

    missing: cached ? [] : (cache.missing || [])

  });

}

async function rdtCountCachedResponseBytes(response, expectedBytes, onProgress) {
  const clone = response.clone();
  if (clone.body && typeof clone.body.getReader === "function") {
    const reader = clone.body.getReader();
    let total = 0;
    let lastProgressAt = 0;
    const emitProgress = (force = false) => {
      if (typeof onProgress !== "function") return;
      const now = Date.now();
      if (!force && now - lastProgressAt < 300) return;
      lastProgressAt = now;
      try { onProgress(total); } catch {}
    };
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        total += part.value ? part.value.byteLength : 0;
        emitProgress(false);
        // A size mismatch is already proven; stop early instead of reading an
        // unexpectedly huge/corrupt cache entry into memory.
        if (Number.isFinite(Number(expectedBytes)) && Number(expectedBytes) >= 0 && total > Number(expectedBytes)) {
          emitProgress(true);
          try { await reader.cancel(); } catch {}
          return total;
        }
      }
      emitProgress(true);
      return total;
    } finally { try { reader.releaseLock(); } catch {} }
  }
  const blob = await clone.blob();
  if (typeof onProgress === "function") { try { onProgress(blob.size); } catch {} }
  return blob.size;
}

async function rdtInspectOnnxArtifactCache(modelId, revision, device, variant, expected, evictInvalid, deepHashLarge = false) {
  const report = {
    capturedAt: new Date().toISOString(),
    modelId: String(modelId || "").slice(0, 120),
    revision: /^[0-9a-f]{40}$/i.test(String(revision || "")) ? String(revision) : null,
    strategy: deepHashLarge
      ? "selected-onnx-file-size-and-sha256-check-including-large-file"
      : "selected-onnx-file-size-and-small-file-sha256-check",
    deepHashLargeFilesChecked: !!deepHashLarge,
    inspectionPurpose: evictInvalid ? "integrity-audit-and-selective-repair" : "read-only-cache-inspection",
    files: [],
    evictedFiles: []
  };
  try {
    if (!self.caches) { report.error = "cache-storage-unavailable"; return report; }
    const cache = await caches.open("transformers-cache");
    const selected = expectedFilesForVariant(modelId, device, variant, expected)
      .filter(f => f && typeof f.path === "string" && f.path.startsWith("onnx/"));
    for (const f of selected) {
      const exactUrl = cacheUrlFor(modelId, revision, f.path);
      // Inspect and, if needed, evict under the same key lock used by Cache.match/put.
      // This prevents another tab from replacing a valid entry between the check and delete.
      const item = await rdtWithCacheEntryLock(exactUrl, evictInvalid ? "inspect-and-repair" : "inspect", async () => {
        const keys = await cache.keys();
        let matchRequest = exactUrl;
        let response = await cache.match(exactUrl);
        if (!response) {
          const alternative = keys.find(req => req && typeof req.url === "string" && req.url.indexOf("/resolve/" + revision + "/" + f.path) !== -1);
          if (alternative) { matchRequest = alternative; response = await cache.match(alternative); }
        }
        const result = {
          path: f.path,
          present: !!response,
          expectedBytes: Number.isFinite(Number(f.size)) ? Number(f.size) : null,
          actualBytes: null,
          sizeMatches: null,
          sha256Checked: false,
          sha256Matches: null,
          cacheEntryEvicted: false
        };
        if (!response) return result;
        try {
          result.actualBytes = await rdtCountCachedResponseBytes(response, result.expectedBytes);
          if (result.expectedBytes !== null) result.sizeMatches = result.actualBytes === result.expectedBytes;
        } catch (e) {
          result.readError = String(e && e.message || e).slice(0, 180);
        }
        // Hash only small ONNX artifacts to keep peak memory bounded on low-RAM devices.
        if (result.sizeMatches === true && result.expectedBytes !== null && (result.expectedBytes <= 12 * 1024 * 1024 || deepHashLarge) && typeof f.sha256 === "string" && typeof crypto !== "undefined" && crypto.subtle) {
          try {
            const bytes = await response.clone().arrayBuffer();
            const digest = await crypto.subtle.digest("SHA-256", bytes);
            const hex = Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, "0")).join("");
            result.sha256Checked = true;
            result.sha256Matches = hex.toLowerCase() === f.sha256.toLowerCase();
          } catch (e) {
            result.sha256Error = String(e && e.message || e).slice(0, 180);
          }
        }
        const invalid = result.sizeMatches === false || result.sha256Matches === false;
        if (invalid && evictInvalid) {
          try {
            await cache.delete(matchRequest);
            if (matchRequest !== exactUrl) await cache.delete(exactUrl);
            result.cacheEntryEvicted = true;
          } catch (e) {
            result.evictError = String(e && e.message || e).slice(0, 180);
          }
        }
        return result;
      });
      if (item.cacheEntryEvicted) report.evictedFiles.push(f.path);
      report.files.push(item);
    }
  } catch (e) {
    report.error = String(e && e.message || e).slice(0, 240);
  }
  return report;
}

async function rdtSha256ResponseHex(response) {
  const bytes = await response.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, "0")).join("");
}

async function rdtPrefetchMissingOnnxArtifact(id, modelId, revision, file, deepHashLarge, report) {
  const exactUrl = cacheUrlFor(modelId, revision, file.path);
  const expectedBytes = Number.isFinite(Number(file.size)) ? Number(file.size) : null;
  return rdtWithCacheEntryLock(exactUrl, "prefetch-missing-onnx", async () => {
    const cache = await caches.open("transformers-cache");
    // Re-check after acquiring the same per-key cross-tab lock. If another tab
    // finished downloading while we waited, use its complete cache entry.
    const existing = await cache.match(exactUrl);
    if (existing) {
      const row = {
        path: file.path, present: true, expectedBytes, actualBytes: null,
        sizeMatches: null, sha256Checked: false, sha256Matches: null,
        cacheEntryEvicted: false, cacheSource: "existing-exact-cache-key",
        downloadedBeforePipeline: false
      };
      report.files.push(row);
      return row;
    }

    const row = {
      path: file.path, present: false, expectedBytes, actualBytes: null,
      sizeMatches: null, sha256Checked: false, sha256Matches: null,
      cacheEntryEvicted: false, cacheSource: "missing-before-prefetch",
      downloadedBeforePipeline: false, attempts: []
    };
    if (expectedBytes === null || expectedBytes <= 0) {
      row.error = "pinned-expected-byte-size-unavailable";
      report.files.push(row);
      throw new Error("EasyWhisper cannot safely prefetch " + file.path + ": pinned expected byte size is unavailable.");
    }

    let lastError = null;
    for (let attempt = 1; attempt <= 2; attempt++) {
      let putPromise = null;
      try {
        reply({
          id, type: "progress", phase: "model-file-preflight",
          file: file.path, loaded: 0, total: expectedBytes, progress: 0,
          note: "Preparing the missing Whisper model file before opening the WASM session."
        });
        const response = await rdtHfAwareFetch(exactUrl, { cache: "no-store" });
        if (!response || !response.ok || response.status !== 200) {
          throw new Error("unexpected-model-file-http-status:" + (response ? response.status : "no-response"));
        }
        const countPromise = rdtCountCachedResponseBytes(response.clone(), expectedBytes, loaded => {
          reply({
            id, type: "progress", phase: "model-file-prefetch-progress",
            file: file.path, loaded, total: expectedBytes,
            progress: expectedBytes > 0 ? Math.min(100, loaded / expectedBytes * 100) : null,
            note: "Downloading and validating the missing ONNX file before session creation."
          });
        });
        const shouldHash = !!(file.sha256 && typeof crypto !== "undefined" && crypto.subtle &&
          (expectedBytes <= 12 * 1024 * 1024 || deepHashLarge));
        const hashPromise = shouldHash ? rdtSha256ResponseHex(response.clone()) : Promise.resolve(null);
        // Cache.put and byte counting run concurrently to avoid accumulating the
        // whole 30.7 MB decoder in a second memory buffer. Readers are blocked by
        // the same lock until the complete size/hash checks have finished.
        putPromise = cache.put(exactUrl, response.clone());
        const [actualBytes, hashHex] = await Promise.all([countPromise, hashPromise, putPromise]).then(([actual, hash]) => [actual, hash]);
        row.actualBytes = actualBytes;
        row.sizeMatches = actualBytes === expectedBytes;
        if (shouldHash) {
          row.sha256Checked = true;
          row.sha256Matches = String(hashHex).toLowerCase() === String(file.sha256).toLowerCase();
        }
        if (row.sizeMatches !== true) {
          await cache.delete(exactUrl);
          throw new Error("download-size-mismatch:expected=" + expectedBytes + ":actual=" + actualBytes);
        }
        if (row.sha256Checked && row.sha256Matches !== true) {
          await cache.delete(exactUrl);
          throw new Error("download-sha256-mismatch:" + file.path);
        }
        row.present = true;
        row.cacheEntryEvicted = false;
        row.cacheSource = "downloaded-and-verified-before-pipeline";
        row.downloadedBeforePipeline = true;
        row.attempts.push({ attempt, status: "verified", actualBytes });
        row.attemptCount = attempt;
        report.prefetchAttempts.push({ path: file.path, attempt, status: "verified", actualBytes });
        report.files.push(row);
        return row;
      } catch (error) {
        lastError = String(error && error.message || error).slice(0, 300);
        try { if (putPromise) await putPromise; } catch {}
        try { await cache.delete(exactUrl); } catch {}
        row.attempts.push({ attempt, status: "failed", error: lastError });
        report.prefetchAttempts.push({ path: file.path, attempt, status: "failed", error: lastError });
        if (attempt < 2) await new Promise(resolve => setTimeout(resolve, 300));
      }
    }
    row.error = lastError || "download-failed";
    row.present = false;
    report.files.push(row);
    throw new Error("EasyWhisper could not download and verify " + file.path + " after two attempts. " + row.error);
  });
}

async function rdtRefreshModelCacheSummary(modelId, revision, device, variant, expected, source) {
  const cacheExpected = expectedFilesForVariant(modelId, device, variant, expected);
  const inspection = await inspectModelCache(modelId, revision, cacheExpected);
  const files = Array.isArray(cacheExpected) ? cacheExpected.filter(f => f && f.path) : [];
  const metadataListed = files.some(f => !String(f.path).startsWith("onnx/"));
  const requiredMetadataCount = metadataListed ? 0 : REQUIRED_META.length;
  const expectedFileCount = files.length + requiredMetadataCount;
  const missingFileCount = Array.isArray(inspection.missing) ? inspection.missing.length : null;
  const presentFileCount = missingFileCount === null ? null : Math.max(0, expectedFileCount - missingFileCount);
  const summary = {
    state: String(inspection.state || "unknown"),
    expectedFileCount,
    presentFileCount,
    missingFileCount,
    fullyCached: inspection.state === "full",
    online: typeof navigator === "undefined" ? true : navigator.onLine !== false,
    checkedAt: new Date().toISOString(),
    source: String(source || "cache-inspection").slice(0, 80),
    missing: Array.isArray(inspection.missing) ? inspection.missing.slice(0, 12) : []
  };
  rdtModelCacheSummary = summary;
  return summary;
}

async function rdtEnsureLegacyWasmArtifactsCached(id, modelId, revision, variant, expected) {
  const report = {
    capturedAt: new Date().toISOString(),
    modelId: String(modelId || "").slice(0, 120),
    revision: /^[0-9a-f]{40}$/i.test(String(revision || "")) ? String(revision) : null,
    strategy: "legacy-wasm-prefetch-missing-selected-onnx-before-session",
    files: [], evictedFiles: [], prefetchAttempts: [],
    preflight: { started: true, completed: false, deepHashLarge: !!rdtDeepHashLargeOnNextLegacyRetry }
  };
  rdtLastOnnxArtifactCacheCheck = report;
  try {
    if (!self.caches || typeof caches.open !== "function") {
      report.preflight.skipped = "cache-storage-unavailable";
      report.preflight.completed = true;
      return report;
    }
    const selected = expectedFilesForVariant(modelId, "wasm", variant, expected)
      .filter(f => f && typeof f.path === "string" && f.path.startsWith("onnx/") && Number.isFinite(Number(f.size)));
    if (!selected.length) {
      report.preflight.skipped = "no-pinned-onnx-artifact-list";
      report.preflight.completed = true;
      return report;
    }
    const deepHashLarge = !!rdtDeepHashLargeOnNextLegacyRetry;
    rdtDeepHashLargeOnNextLegacyRetry = false;
    report.preflight.deepHashLarge = deepHashLarge;
    const results = await Promise.allSettled(selected.map(file => rdtPrefetchMissingOnnxArtifact(id, modelId, revision, file, deepHashLarge, report)));
    const failed = results.find(result => result.status === "rejected");
    if (failed) throw failed.reason;
    report.preflight.completed = true;
    report.preflight.filesFetched = report.prefetchAttempts.filter(x => x.status === "verified").map(x => x.path).filter((v, i, a) => a.indexOf(v) === i);
    report.cacheStatusAfterPrefetch = await rdtRefreshModelCacheSummary(modelId, revision, "wasm", variant, expected, "post-legacy-wasm-prefetch");
    return report;
  } catch (error) {
    report.preflight.completed = false;
    report.preflight.error = String(error && error.message || error).slice(0, 500);
    try { report.cacheStatusAfterPrefetch = await rdtRefreshModelCacheSummary(modelId, revision, "wasm", variant, expected, "failed-legacy-wasm-prefetch"); } catch {}
    throw error;
  }
}

async function verifySmallFiles(modelId, revision, expected) {

  const MAX = 10 * 1024 * 1024;

  const cache = self.caches ? await caches.open("transformers-cache") : null;

  if (!cache) return { checked: 0, skipped: expected.length, failed: [] };

  let checked = 0;

  let skipped = 0;

  const failed = [];

  for (const f of expected) {

    if (!f || !f.sha256 || !f.path) continue;

    if (f.size > MAX) { skipped++; continue; }

    let resp = await cache.match(cacheUrlFor(modelId, revision, f.path));

    if (!resp) {

      const all = await cache.keys();

      const hit = all.find(req => req.url.indexOf("/resolve/" + revision + "/" + f.path) !== -1);

      if (hit) resp = await cache.match(hit);

    }

    if (!resp) { skipped++; continue; }

    const buf = await resp.clone().arrayBuffer();

    const hashBuf = await crypto.subtle.digest("SHA-256", buf);

    const hex = Array.from(new Uint8Array(hashBuf))

      .map(b => b.toString(16).padStart(2, "0")).join("");

    if (hex.toLowerCase() === String(f.sha256).toLowerCase()) checked++;

    else failed.push(f.path);

  }

  return { checked, skipped, failed };

}

function normalizeAudio(payloadAudio) {

  if (payloadAudio instanceof Float32Array) return payloadAudio;
  // Keep long captures compact in transit and memory. Convert each inference
  // window to Float32 before it reaches Transformers.js, which expects Float32.
  if (payloadAudio instanceof Int16Array) return payloadAudio;

  if (payloadAudio instanceof ArrayBuffer) return new Float32Array(payloadAudio);

  if (ArrayBuffer.isView(payloadAudio)) {
    if (payloadAudio.BYTES_PER_ELEMENT === 2) {
      const n = Math.floor(payloadAudio.byteLength / 2);
      return new Int16Array(payloadAudio.buffer, payloadAudio.byteOffset, n);
    }
    return new Float32Array(payloadAudio.buffer, payloadAudio.byteOffset, Math.floor(payloadAudio.byteLength / 4));
  }

  if (Array.isArray(payloadAudio)) return Float32Array.from(payloadAudio);

  throw new Error("run: missing audio Float32Array or Int16Array");

}

function toFloat32Audio(audio) {
  if (!(audio instanceof Int16Array)) return audio;
  const out = new Float32Array(audio.length);
  for (let i = 0; i < audio.length; i++) out[i] = audio[i] / 32768.0;
  return out;
}

function inferenceWindow(audio, start, end) {
  const slice = audio.subarray(start, end);
  return slice instanceof Int16Array ? toFloat32Audio(slice) : slice;
}

function mergeTextPieces(pieces) {

  let out = "";

  for (const raw of pieces) {

    const next = String(raw || "").trim();

    if (!next) continue;

    if (!out) { out = next; continue; }

    // Improved overlap removal: handles both spaced languages and CJK (no spaces).

    // For CJK, we compare character overlap, not word overlap.

    const isCjk = /[\u4e00-\u9fff]/.test(out.slice(-20) + next.slice(0,20));

    if (isCjk) {

      const a = out.replace(/\s+/g, "");

      const b = next.replace(/\s+/g, "");

      let overlap = 0;

      const max = Math.min(80, a.length, b.length);

      for (let n = max; n >= 6; n--) {

        if (a.slice(-n) === b.slice(0, n)) { overlap = n; break; }

      }

      if (overlap > 0) {

        // Remove overlap from next, preserving original spacing handling

        let trimmed = next;

        let removed = 0;

        while (removed < overlap && trimmed.length) {

          if (/\s/.test(trimmed[0])) { trimmed = trimmed.slice(1); continue; }

          trimmed = trimmed.slice(1);

          removed++;

        }

        out += trimmed.replace(/^\s+/, "");

      } else {

        out += next;

      }

    } else {

      const a = out.toLocaleLowerCase();

      const b = next.toLocaleLowerCase();

      let overlap = 0;

      const max = Math.min(140, a.length, b.length);

      for (let n = max; n >= 12; n--) {

        if (a.slice(-n).replace(/\s+/g, " ") === b.slice(0, n).replace(/\s+/g, " ")) { overlap = n; break; }

      }

      out += overlap ? next.slice(overlap).replace(/^\s+/, " ") : " " + next;

    }

  }

  return out.trim();

}

async function runBatchedInference(id, audio, opts) {

  const BATCH_SAMPLES = 60 * 16000;

  const OVERLAP_SAMPLES = 2 * 16000;

  const total = Math.max(1, Math.ceil(audio.length / BATCH_SAMPLES));

  const textPieces = [];

  const allChunks = [];

  let elapsedMs = 0;

  let allHaveChunks = true;

  reply({ id, type: "progress", phase: "inference-start", progress: 0, batchIndex: 0, batchTotal: total, note: total > 1 ? `Processing ${total} audio batches` : "Transcription in progress" });

  for (let i = 0; i < total; i++) {

    const coreStart = i * BATCH_SAMPLES;

    const inputStart = Math.max(0, coreStart - (i > 0 ? OVERLAP_SAMPLES : 0));

    const coreEnd = Math.min(audio.length, coreStart + BATCH_SAMPLES);

    const inputEnd = Math.min(audio.length, coreEnd + (i < total - 1 ? OVERLAP_SAMPLES : 0));

    const input = inferenceWindow(audio, inputStart, inputEnd);

    const started = performance.now();
    rdtSetOperationStage("onnx-inference-call", {
      batchIndex: i + 1,
      batchTotal: total,
      windowDurationSeconds: Math.round((input.length / 16000) * 1000) / 1000,
      effectiveDevice: currentDevice,
      modelId: currentModelId,
      resolvedLanguage: opts && opts.language ? opts.language : "auto"
    });

    const result = await transcriber(input, opts);

    elapsedMs += performance.now() - started;

    const chunks = (result && Array.isArray(result.chunks)) ? result.chunks : [];

    if (!chunks.length) allHaveChunks = false;

    if (chunks.length) {

      for (const c of chunks) {

        const ts = Array.isArray(c.timestamp) ? c.timestamp : [null, null];

        // Drop chunks wholly inside the overlap at the start of later batches.

        // Exception: keep chunk if it has no end timestamp (final chunk) to avoid dropping last word.

        if (i > 0 && ts[1] != null && ts[1] <= (coreStart - inputStart) / 16000) continue;

        const start = ts[0] == null ? null : ts[0] + inputStart / 16000;

        const end = ts[1] == null ? null : ts[1] + inputStart / 16000;

        allChunks.push({ text: c.text || "", timestamp: [start, end] });

      }

    }

    textPieces.push((result && result.text) || "");

    reply({

      id, type: "progress", phase: "inference-progress",

      progress: Math.min(99, Math.round(((i + 1) / total) * 100)),

      batchIndex: i + 1, batchTotal: total,

      note: total > 1 ? `Batch ${i + 1} of ${total}` : "Transcription in progress"

    });

  }

  // Preserve pipeline's formatted full text when single batch; for multi-batch stitch from chunk text

  // where available to avoid replaying overlap. If chunks exist, prefer them to avoid missing final words

  // that mergeTextPieces might drop.

  let text;

  if (total === 1) {

    text = String(textPieces[0] || "");

    // If chunks provide more coverage (e.g., final word only in chunks), use chunk-joined text when longer

    if (allHaveChunks && allChunks.length) {

      const chunkText = allChunks.map(c => c.text || "").join("").trim();

      // Heuristic: if chunkText is longer or pipeline text is empty, prefer chunkText to avoid omitted final words

      if (chunkText && chunkText.length > text.length) text = chunkText;

    }

  } else {

    text = (allHaveChunks && allChunks.length ? allChunks.map(c => c.text || "").join("").trim() : mergeTextPieces(textPieces));

  }

  return { text, chunks: allChunks, elapsedMs, durationSec: audio.length / 16000 };

}

async function runBatchedInferenceAuto(id, audio, baseOpts, detectedLangRef) {

  // For auto (mixed Chinese+English), detect language per 30s window and transcribe with that language

  // This allows code-switching: Chinese 30s chunk -> chinese, English chunk -> english

  const BATCH_SAMPLES = 30 * 16000; // smaller batch for better language switching

  const OVERLAP_SAMPLES = 2 * 16000;

  const total = Math.max(1, Math.ceil(audio.length / BATCH_SAMPLES));

  const textPieces = [];

  const allChunks = [];

  let elapsedMs = 0;

  let allHaveChunks = true;

  let detectedLanguages = [];
  let lastDetectedBatchLang = null;

  reply({ id, type: "progress", phase: "inference-start", progress: 0, batchIndex: 0, batchTotal: total, note: total > 1 ? `Auto-detect mixed: ${total} batches` : "Transcription in progress (auto)" });

  for (let i = 0; i < total; i++) {

    const coreStart = i * BATCH_SAMPLES;

    const inputStart = Math.max(0, coreStart - (i > 0 ? OVERLAP_SAMPLES : 0));

    const coreEnd = Math.min(audio.length, coreStart + BATCH_SAMPLES);

    const inputEnd = Math.min(audio.length, coreEnd + (i < total - 1 ? OVERLAP_SAMPLES : 0));

    const input = inferenceWindow(audio, inputStart, inputEnd);

    // Detect language for this batch (use first 15s of batch for detection to be fast)

    const detectSlice = input.length > 15 * 16000 ? input.subarray(0, 15 * 16000) : input;

    let batchLang = null;

    try { batchLang = await detectAudioLanguage(detectSlice, id); } catch (e) {}
    // If the first half of a segment is silent/noisy, try a separate later window once.
    if (!batchLang && input.length > 15 * 16000) {
      try { batchLang = await detectAudioLanguage(input.subarray(input.length - 15 * 16000), id); } catch (e) {}
    }
    if (!batchLang && lastDetectedBatchLang) {
      batchLang = lastDetectedBatchLang;
      reply({ id, type: "progress", phase: "language-detection-reused", note: `Could not identify batch ${i+1}; continuing with the last detected language (${batchLang}).`, batchIndex: i+1 });
    }
    if (!batchLang) {
      throw new Error("EasyWhisper couldn't identify the spoken language in the first audio segment. It did not force English. Choose the spoken language in Settings and retry, or reload the model and try Auto Detect again.");
    }
    lastDetectedBatchLang = batchLang;

    // Sanitize: strip < > | wrappers

    batchLang = String(batchLang).replace(/<\|/g, "").replace(/\|>/g, "").replace(/[<|>]/g, "").replace(/\|/g, "").trim().toLowerCase();

    if (batchLang === "zh" || batchLang === "zh-cn" || batchLang === "zh-tw") batchLang = "chinese";

    if (batchLang === "en") batchLang = "english";

    detectedLanguages.push(batchLang);

    const opts = { ...baseOpts, language: batchLang };

    reply({ id, type: "progress", phase: "detecting-language", note: `Batch ${i+1}/${total} detected as ${batchLang}`, language: batchLang, batchIndex: i+1 });

    const started = performance.now();

    const result = await transcriber(input, opts);

    elapsedMs += performance.now() - started;

    const chunks = (result && Array.isArray(result.chunks)) ? result.chunks : [];

    if (!chunks.length) allHaveChunks = false;

    if (chunks.length) {

      for (const c of chunks) {

        const ts = Array.isArray(c.timestamp) ? c.timestamp : [null, null];

        if (i > 0 && ts[1] != null && ts[1] <= (coreStart - inputStart) / 16000) continue;

        const start = ts[0] == null ? null : ts[0] + inputStart / 16000;

        const end = ts[1] == null ? null : ts[1] + inputStart / 16000;

        allChunks.push({ text: c.text || "", timestamp: [start, end], language: batchLang });

      }

    }

    textPieces.push((result && result.text) || "");

    reply({

      id, type: "progress", phase: "inference-progress",

      progress: Math.min(99, Math.round(((i + 1) / total) * 100)),

      batchIndex: i + 1, batchTotal: total,

      note: `Batch ${i + 1} of ${total} (${batchLang})`

    });

  }

  let text;

  if (total === 1) {

    text = String(textPieces[0] || "");

    if (allHaveChunks && allChunks.length) {

      const chunkText = allChunks.map(c => c.text || "").join("").trim();

      if (chunkText && chunkText.length > text.length) text = chunkText;

    }

  } else {

    text = (allHaveChunks && allChunks.length ? allChunks.map(c => c.text || "").join("").trim() : mergeTextPieces(textPieces));

  }

  if (detectedLangRef) detectedLangRef.value = detectedLanguages.join(",");

  return { text, chunks: allChunks, elapsedMs, durationSec: audio.length / 16000, detectedLanguages };

}





function rdtCollectTokenIds(value, out) {
  if (value == null || !out) return;
  try {
    if (value && typeof value.tolist === "function") {
      const listed = value.tolist();
      if (listed !== value) { rdtCollectTokenIds(listed, out); return; }
    }
  } catch (e) {}
  if (Array.isArray(value)) {
    for (const item of value) rdtCollectTokenIds(item, out);
  } else if (
    (typeof value === "number" || typeof value === "bigint") &&
    Number.isFinite(Number(value))
  ) {
    // Transformers.js 3.7.1 returns int64 token tensors backed by BigInt64Array.
    // Tensor.tolist() preserves BigInt elements, so normalize IDs before mapping.
    out.push(Number(value));
  }
}

async function rdtDecodeWhisperLanguageToken(tokenId) {
  const tokenizer = transcriber && (transcriber.tokenizer || transcriber.processor && transcriber.processor.tokenizer);
  if (!tokenizer) return null;
  const parse = value => {
    const m = String(value || "").match(/<\|([a-z]{2,3}(?:-[a-z0-9]+)?)\|>/i);
    return m ? m[1].toLowerCase() : null;
  };
  try {
    if (typeof tokenizer.decode === "function") {
      const decoded = await tokenizer.decode([Number(tokenId)], { skip_special_tokens: false });
      const lang = parse(decoded);
      if (lang) return lang;
    }
  } catch (e) {}
  try {
    const convert = tokenizer.convert_ids_to_tokens || tokenizer.convertIdsToTokens;
    if (typeof convert === "function") {
      const token = await convert.call(tokenizer, [Number(tokenId)]);
      const lang = parse(Array.isArray(token) ? token[0] : token);
      if (lang) return lang;
    }
  } catch (e) {}
  return null;
}

function rdtCollapseCjkRepeatedRuns(value) {
  const source = String(value || "");
  return source.replace(/[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]{12,}/g, run => {
    const chars = Array.from(run);
    const out = [];
    let i = 0, collapsedAny = false;
    while (i < chars.length) {
      let chosenLen = 0, chosenRuns = 0;
      const maxLen = Math.min(12, Math.floor((chars.length - i) / 4));
      for (let n = 2; n <= maxLen; n++) {
        let reps = 1;
        while (i + (reps + 1) * n <= chars.length) {
          let same = true;
          for (let k = 0; k < n; k++) {
            if (chars[i + k] !== chars[i + reps * n + k]) { same = false; break; }
          }
          if (!same) break;
          reps++;
        }
        if (reps >= 4 && n * reps >= 12) { chosenLen = n; chosenRuns = reps; break; }
      }
      if (chosenLen) { out.push(chars.slice(i, i + chosenLen).join("")); i += chosenLen * chosenRuns; collapsedAny = true; }
      else {
        // Also catch noisy loop variants such as 我们的我们我们的我们 on long Han runs.
        // Require 8+ copies, short gaps, and a 24-character span to avoid touching normal text.
        let nearLen = 0, nearEnd = 0;
        for (let n = 2; n <= Math.min(4, chars.length - i); n++) {
          let reps = 1, lastEnd = i + n, pos = i + n;
          while (pos + n <= chars.length) {
            let found = -1;
            const stop = Math.min(chars.length - n, pos + 2);
            for (let j = pos; j <= stop; j++) {
              let same = true;
              for (let k = 0; k < n; k++) if (chars[i + k] !== chars[j + k]) { same = false; break; }
              if (same) { found = j; break; }
            }
            if (found < 0) break;
            reps++; lastEnd = found + n; pos = lastEnd;
          }
          if (reps >= 8 && lastEnd - i >= 24) { nearLen = n; nearEnd = lastEnd; break; }
        }
        if (nearLen) { out.push(chars.slice(i, i + nearLen).join("")); i = nearEnd; collapsedAny = true; }
        else out.push(chars[i++]);
      }
    }
    let result = out.join("");
    // Remove a short repeated tail fragment that can remain at the seam between loop shapes.
    if (collapsedAny) {
      const tailChars = Array.from(result);
      for (let n = 2; n <= 4 && tailChars.length >= n * 2; n++) {
        const tail = tailChars.slice(-n).join("");
        let duplicated = false;
        for (let j = Math.max(0, tailChars.length - n - 3); j <= tailChars.length - n * 2; j++) {
          if (tailChars.slice(j, j + n).join("") === tail && tailChars.length - n - (j + n) <= 2) { duplicated = true; break; }
        }
        if (duplicated) { result = tailChars.slice(0, -n).join(""); break; }
      }
    }
    return result;
  });
}

function rdtCleanRepeatedText(value) {
  // CJK speech may hallucinate long no-space loops; handle them before tokenization.
  let text = rdtCollapseCjkRepeatedRuns(String(value || ""));
  // Collapse only unmistakable loops: a word three or more times, or a multiword
  // phrase repeated three or more times with whitespace-only boundaries.
  text = text.replace(/\b([\p{L}\p{N}][\p{L}\p{N}'’_-]{2,})\b(?:\s+\1\b){2,}/giu, "$1");
  for (let pass = 0; pass < 8; pass++) {
    const tokens = Array.from(text.matchAll(/[\p{L}\p{N}]+(?:['’_-][\p{L}\p{N}]+)*/gu), m => ({
      raw: m[0], norm: m[0].toLocaleLowerCase(), start: m.index, end: m.index + m[0].length
    }));
    let changed = false;
    for (let i = 0; i < tokens.length && !changed; i++) {
      const maxN = Math.min(8, Math.floor((tokens.length - i) / 3));
      for (let n = maxN; n >= 2 && !changed; n--) {
        let repeats = 1;
        let blockStart = i + n;
        while (blockStart + n <= tokens.length) {
          let same = true;
          for (let j = 0; j < n; j++) {
            if (tokens[i + j].norm !== tokens[blockStart + j].norm) { same = false; break; }
            const prev = tokens[blockStart + j - 1];
            const cur = tokens[blockStart + j];
            if (j > 0 && !/^\s*$/.test(text.slice(prev.end, cur.start))) { same = false; break; }
          }
          const firstEnd = tokens[blockStart - 1].end;
          if (!same || !/^\s+$/.test(text.slice(tokens[blockStart - 1].end, tokens[blockStart].start))) break;
          repeats++;
          blockStart += n;
        }
        if (repeats >= 3) {
          const firstPhraseEnd = tokens[i + n - 1].end;
          const repeatedEnd = tokens[i + n * repeats - 1].end;
          text = text.slice(0, firstPhraseEnd) + text.slice(repeatedEnd);
          changed = true;
        }
      }
    }
    if (!changed) break;
  }
  // Collapse immediately repeated full sentences only when the sentence is substantial.
  const parts = text.split(/(?<=[.!?。！？])/u);
  if (parts.length > 1) {
    const out = [];
    for (const part of parts) {
      const norm = part.normalize("NFKC").replace(/\s+/g, " ").trim().toLocaleLowerCase();
      const prev = out.length ? out[out.length - 1] : "";
      const prevNorm = prev.normalize("NFKC").replace(/\s+/g, " ").trim().toLocaleLowerCase();
      const substantial = norm.length >= 12 || (/[一-鿿]/u.test(norm) && Array.from(norm).length >= 5);
      if (substantial && norm === prevNorm) continue;
      out.push(part);
    }
    text = out.join("");
  }
  return text.replace(/[ \t]{2,}/g, " ").trim();
}

// --- Auto language detection for Transformers.js v3 ---

// Transformers.js v3 has TODO in _retrieve_init_tokens that defaults to English when no language specified.

// Workaround from waxberry-dev/live-translate: run model.generate with only SOT token to get language token.

async function detectAudioLanguage(audio, runId) {

  try {

    if (!transcriber) {

      reply({ id: runId, type: "progress", phase: "detect-error", note: "detect: no transcriber" });

      return null;

    }

    const model = transcriber.model;

    if (!model) {

      reply({ id: runId, type: "progress", phase: "detect-error", note: "detect: no model" });

      return null;

    }

    // Processor can be under different properties depending on transformers.js version

    const processor = transcriber.processor || transcriber.feature_extractor || transcriber._feature_extractor || transcriber.processor_config;

    if (!processor) {

      reply({ id: runId, type: "progress", phase: "detect-error", note: "detect: no processor/feature_extractor found, keys: " + Object.keys(transcriber).join(",") });

      return null;

    }

    // Try to get input_features

    let input_features = null;

    try {

      const procOut = await processor(audio);

      input_features = procOut.input_features || procOut.input_values || procOut;

      // If it's a tensor-like object, keep as is

    } catch (pe) {

      // Fallback: try calling processor as function with sampling_rate

      try {

        const procOut2 = await processor(audio, { sampling_rate: 16000 });

        input_features = procOut2.input_features || procOut2;

      } catch (pe2) {

        reply({ id: runId, type: "progress", phase: "detect-error", note: "detect: processor failed: " + (pe && pe.message || pe) + " / " + (pe2 && pe2.message || pe2) });

        return null;

      }

    }

    const genConfig = model.generation_config || model.config || {};

    const sotId = genConfig.decoder_start_token_id ?? genConfig.bos_token_id ?? 50258;

    const langToId = genConfig.lang_to_id || genConfig.lang2id || {};

    reply({ id: runId, type: "progress", phase: "detect-info", note: "detect: sot=" + sotId + " lang_to_id keys=" + Object.keys(langToId).slice(0,5).join(",") + " audio_len=" + audio.length });

    let output = null;

    try {

      output = await model.generate({

        inputs: input_features,

        decoder_input_ids: [[sotId]],

        max_new_tokens: 1,

        do_sample: false,

      });

    } catch (ge) {

      // Try alternative param name: input_features instead of inputs

      try {

        output = await model.generate({

          input_features: input_features,

          decoder_input_ids: [[sotId]],

          max_new_tokens: 1,

          do_sample: false,

        });

      } catch (ge2) {

        reply({ id: runId, type: "progress", phase: "detect-error", note: "detect: generate failed: " + (ge && ge.message || ge) + " / alt: " + (ge2 && ge2.message || ge2) });

        return null;

      }

    }

    let langTokenId = null;

    try {
      const tokenIds = [];
      if (output && output.sequences != null) rdtCollectTokenIds(output.sequences, tokenIds);
      if (!tokenIds.length) rdtCollectTokenIds(output, tokenIds);
      const sotAt = tokenIds.findIndex(n => Number(n) === Number(sotId));
      const generated = sotAt >= 0 ? tokenIds.slice(sotAt + 1) : tokenIds.slice(-4);
      const idValues = new Set(Object.values(langToId).map(Number).filter(Number.isFinite));
      // Prefer a generated id recognized by the config map. If that map is missing
      // in this Transformers.js/model build, try the tokenizer's own special-token map.
      langTokenId = generated.find(n => idValues.has(Number(n))) ?? null;
      if (langTokenId == null && generated.length) langTokenId = generated[0];
      if (langTokenId == null && tokenIds.length > 1) langTokenId = tokenIds[tokenIds.length - 1];
    } catch (te) {
      reply({ id: runId, type: "progress", phase: "detect-error", note: "detect: token extraction failed: " + (te && te.message || te) });
    }

    if (langTokenId == null) {

      reply({ id: runId, type: "progress", phase: "detect-error", note: "detect: could not extract langTokenId from output: " + String(output).slice(0,200) });

      return null;

    }

    const idToLang = {};

    for (const [lang, id] of Object.entries(langToId)) {

      idToLang[id] = lang;

    }

    let detected = idToLang[langTokenId] || null;

    if (!detected) {
      // Older Transformers.js builds may omit lang_to_id from generation_config.
      // Decode the actual generated special token instead of guessing English.
      detected = await rdtDecodeWhisperLanguageToken(langTokenId);
    }

    if (detected) {

      // ATOMIC FIX: strip all Whisper wrappers comprehensively

      let raw = String(detected);

      // Remove <| |>, <|, |>, <, >, |

      raw = raw.replace(/<\|/g, "").replace(/\|>/g, "").replace(/[<|>]/g, "").replace(/\|/g, "").trim().toLowerCase();

      const shortToFull = {

        "zh": "chinese",

        "en": "english",

        "es": "spanish",

        "fr": "french",

        "de": "german",

        "it": "italian",

        "ja": "japanese",

        "ko": "korean",

        "pt": "portuguese",

        "ru": "russian",

        "tr": "turkish",

        "pl": "polish",

        "nl": "dutch",

        "ar": "arabic",

        "sv": "swedish",

        "zh-cn": "chinese",

        "zh-tw": "chinese"

      };

      if (shortToFull[raw]) raw = shortToFull[raw];

      detected = raw;

      reply({ id: runId, type: "progress", phase: "detect-success", note: "detect: token=" + langTokenId + " -> " + detected });

    } else {

      reply({ id: runId, type: "progress", phase: "detect-error", note: "detect: token " + langTokenId + " not in lang_to_id" });

    }

    return detected;

  } catch (e) {

    reply({ id: "auto-detect", type: "progress", phase: "detect-error", note: "Language detection failed: " + (e && e.stack || e.message || e) });

    return null;

  }

}

function sanitizeLangInput(s){

  if (s == null) return "";

  let v = String(s).trim().toLowerCase();

  // Strip Whisper token wrappers: <|en|>, <en>, |en|, <|zh|>, etc.

  // Remove all < > | characters, then extract first alpha chunk

  v = v.replace(/[<|>]/g, "").replace(/\|/g, "").trim();

  // Handle cases like "en", "english", "zh", "zh-cn", "auto"

  // If still contains non-alpha except hyphen, take first match

  const m = v.match(/[a-z]+(?:-[a-z]+)?/);

  return m ? m[0] : v;

}

async function rdtRunAudioWithActivePipeline(id, audio, opts, isAutoMixed) {
  if (isAutoMixed && audio.length > 15 * 16000) {
    const detectedRef = { value: null };
    const result = await runBatchedInferenceAuto(id, audio, opts, detectedRef);
    return { result, detectedLanguage: detectedRef.value };
  }
  return { result: await runBatchedInference(id, audio, opts), detectedLanguage: null };
}

async function handleRun(id, payload) {
  rdtSetOperationStage("validate-run-request", { loadedModelId: currentModelId, effectiveDevice: currentDevice });
  if (!transcriber) throw new Error("run: model not loaded; send load first");

  const audio = normalizeAudio(payload.audio);
  rdtSetOperationStage("audio-normalized", {
    sampleCount: audio.length,
    durationSeconds: Math.round((audio.length / 16000) * 1000) / 1000,
    selectedLanguage: typeof payload.language === "string" ? payload.language : "auto"
  });

  // Preserve "auto" explicitly: if payload.language is missing, undefined, or "auto", we trigger auto-detection.

  // Do NOT replace with navigator.language or "en" here. HTML server path does that, but EasyWhisper must not.

  const rawLang = payload.language;

  const rawSanitized = sanitizeLangInput(rawLang);

  const language = (rawSanitized === "" ) ? "auto" : rawSanitized;

  // RDT Voice is a transcription app: never silently translate speech into English.
  const task = "transcribe";

  if (!audio.length) throw new Error("run: audio is empty");

  for (let i = 0; i < audio.length; i += 8192) {

    if (!Number.isFinite(audio[i])) throw new Error("run: audio buffer contains invalid samples");

  }

  const durationSec = audio.length / 16000;

  // Root cause fix for 5-second Mandarin repeated English phrases on whisper-tiny:

  // Whisper's internal chunker pads short audio to 30s with silence. For tiny multilingual,

  // language detection on padded silence often mis-detects as English, then hallucinates

  // English loop. Fix: for short audio (<30s) disable internal chunking (chunk_length_s: 0)

  // so detection runs on actual audio only, not padded silence. For longer audio, keep 30s/5s.

  // Also avoid aggressive timestamp filtering that can drop final words.

  const isShort = durationSec < 30;

  const opts = {

    chunk_length_s: isShort ? 0 : 30,

    stride_length_s: isShort ? 0 : 5,

    return_timestamps: true,

    task,

    no_speech_threshold: 0.6,

    logprob_threshold: -1.0,

    compression_ratio_threshold: 2.4,

    condition_on_previous_text: false

  };

  // Auto language: explicitly set to null/undefined to trigger detection. Do NOT set to "en".

  // Explicit language: pass through (e.g., "zh", "chinese", "en").

  // Transformers.js accepts both short codes and full names; we preserve what HTML sends.

  // For Chinese, HTML sends "zh" – Whisper accepts "zh" and "chinese". Keep "zh".

  let detectedLanguage = null;

  const SHORT_TO_FULL = {

    "en": "english",

    "zh": "chinese",

    "es": "spanish",

    "fr": "french",

    "de": "german",

    "it": "italian",

    "pt": "portuguese",

    "nl": "dutch",

    "ru": "russian",

    "uk": "ukrainian",

    "pl": "polish",

    "cs": "czech",

    "sv": "swedish",

    "da": "danish",

    "fi": "finnish",

    "no": "norwegian",

    "ro": "romanian",

    "hu": "hungarian",

    "el": "greek",

    "tr": "turkish",

    "ar": "arabic",

    "he": "hebrew",

    "hi": "hindi",

    "ja": "japanese",

    "ko": "korean",

    "vi": "vietnamese",

    "th": "thai",

    "id": "indonesian",

    "ms": "malay",

    "tl": "tagalog",

    "lo": "lao",

    "zh-cn": "chinese",

    "zh-tw": "chinese"

  };

  if (language && language !== "auto") {

    // Explicit language: map short code (zh) -> full name (chinese) because transformers.js v3 expects full names

    // See error: Language "<zh>" is not supported. Must be one of: ["english","chinese",...]

    // ATOMIC FIX: ensure language is sanitized and mapped; fallback to english if unknown token still slips through

    let langForModel = sanitizeLangInput(language);

    if (SHORT_TO_FULL[langForModel]) langForModel = SHORT_TO_FULL[langForModel];

    // If after mapping it's still not in allowed list, and looks like a 2-letter code, map again; else keep

    // Final safety: if langForModel is still like "<en>" or unknown, default to english to avoid crash, but log

    const ALLOWED = ["english","chinese","german","spanish","russian","korean","french","japanese","portuguese","turkish","polish","catalan","dutch","arabic","swedish","italian","indonesian","hindi","ukrainian","vietnamese","thai","hebrew","greek","finnish","norwegian","danish","czech","romanian","hungarian","tagalog","malay","urdu","telugu","bengali","tamil","marathi","gujarati","malayalam","kannada","punjabi","assamese","odia","burmese","khmer","lao","nepali","sinhala","persian","pashto","kurdish","bosnian","serbian","croatian","slovenian","slovak","bulgarian","macedonian","albanian","maltese","icelandic","estonian","latvian","lithuanian","welsh","galician","basque","afrikaans","swahili","yoruba","hausa","amharic","somali","zulu","xhosa","shona","sesotho","lingala","kinyarwanda","luo","acholi","akan","bambara","baoulé","be","breton","catalan","corsican","dyula","dzongkha","ewe","faroese","fon","fulah","ga","ganda","guarani","haitian","igbo","kabyle","kongo","kwanyama","kyrgyz","luba","luganda","luo","malagasy","maltese","maori","mongolian","mossi","nuer","nyanja","oromo","sango","shona","somali","sotho","swati","tigrinya","tswana","umbundu","uzbek","wolof","xhosa","yoruba","zulu"];

    if (!ALLOWED.includes(langForModel)) {

      // Try to recover: if it's a short code we missed, try SHORT_TO_FULL again

      if (SHORT_TO_FULL[langForModel]) {

        langForModel = SHORT_TO_FULL[langForModel];

      } else if (langForModel.length === 2) {

        // unknown 2-letter, default english to avoid crash, but keep detection path for auto

        console.warn("[RDT worker] unknown lang code", langForModel, "defaulting to english");

        langForModel = "english";

      }

    }

    opts.language = langForModel;

  } else {

    if (audio.length > 15 * 16000) {
      // Long/mixed recordings are detected per 30-second segment below. Avoid a
      // redundant whole-recording preflight and do not invent English as a default.
      opts.language = null;
      reply({ id, type: "progress", phase: "detecting-language", note: "Auto-detecting spoken language in each audio segment..." });
    } else {
      const detectSlice = toFloat32Audio(audio.length > 30 * 16000 ? audio.subarray(0, 30 * 16000) : audio);
      reply({ id, type: "progress", phase: "detecting-language", note: "Auto-detecting spoken language..." });
      detectedLanguage = await detectAudioLanguage(detectSlice, id);
      if (detectedLanguage) {
        opts.language = detectedLanguage;
        reply({ id, type: "progress", phase: "language-detected", note: "Detected: " + detectedLanguage, language: detectedLanguage });
      } else {
        throw new Error("EasyWhisper couldn't identify the spoken language automatically. It did not force English or intentionally translate the speech. Select the spoken language in Settings and retry, or reload the model and try Auto Detect again.");
      }
    }

  }

  // FINAL SANITIZATION — transformers.js v3 expects full names like "chinese", not "<zh>" or "<en>"

  // This is the line you asked to verify in Console

  if (opts.language) {

    let clean = String(opts.language).replace(/<\|/g, "").replace(/\|>/g, "").replace(/[<|>]/g, "").replace(/\|/g, "").trim().toLowerCase();

    if (clean === "zh" || clean === "zh-cn" || clean === "zh-tw") clean = "chinese";

    if (clean === "en" || clean === "en-us" || clean === "en-gb") clean = "english";

    if (SHORT_TO_FULL[clean]) clean = SHORT_TO_FULL[clean];

    opts.language = clean;

    if (RDT_DEBUG) console.log("[RDT worker] final opts.language", opts.language, "from raw", language);

  }

  let result;

  const isAutoMixed = (language === "auto");
  rdtSetOperationStage("language-configured", { selectedLanguage: language, resolvedLanguage: opts.language || "auto", task, chunkLengthSeconds: opts.chunk_length_s, audioDurationSeconds: Math.round(durationSec * 1000) / 1000 });

  try {

    // For long Auto Detect recordings, preserve per-window language detection.
    const initialExecution = await rdtRunAudioWithActivePipeline(id, audio, opts, isAutoMixed);
    result = initialExecution.result;
    if (initialExecution.detectedLanguage) detectedLanguage = initialExecution.detectedLanguage;

  } catch (err) {

    // Do not create a second runtime/session after an inaccessible model resource or a
    // memory-pressure signal. These conditions are not evidence of an architecture mismatch.
    if (rdtIsRemoteFetchFailure(err)) {
      throw new Error("EasyWhisper couldn't access a required model resource while transcribing. The current job was not retried with another runtime because it needs the same resource. Try again when the connection is stable, or use Local Server. Technical details: " + String(err && err.message || err).slice(0,320));
    }
    if (rdtIsMemoryPressureFailure(err)) {
      throw new Error("EasyWhisper reported memory pressure during inference. Runtime compatibility retry was skipped to avoid a second high-memory attempt. Try a smaller model or a shorter recording. Details: " + String(err && err.message || err).slice(0,180));
    }

    let recoveredByLegacyAndroidRetry = false;

    // On the confirmed legacy ARM Android/Tiny profile, a raw WASM numeric execution failure
    // can indicate runtime/architecture incompatibility rather than a missing model file. Give
    // the pinned model one controlled retry through the non-SIMD-compatible ORT 1.17.3 runtime.
    // Never build this second runtime for a memory-pressure or remote-fetch failure (handled above).
    if (currentDevice === "wasm" && lastLoad && rdtIsLegacyArmAndroidTinyWasmFault(lastLoad.modelId, err) && !rdtIsLegacyNonSimdRuntime()) {
      const compatKey = modelRevisionKey(lastLoad.modelId, lastLoad.revision);
      const defaultRuntimeError = String(err && err.message !== undefined ? err.message : err).slice(0, 220);
      if (!rdtLegacyCompatibilityAttemptedModels.has(compatKey)) {
        rdtLegacyCompatibilityAttemptedModels.add(compatKey);
        const compatLoad = { ...lastLoad };
        reply({ id, type: "progress", phase: "fallback", fallbackReason: "legacy-arm32-runtime-compatibility", note: "The default WASM runtime failed on this older Android device. Retrying once with the version-matched compatibility runtime and the same pinned INT8 model files." });
        rdtSetOperationStage("legacy-arm32-runtime-retry", { modelId: compatLoad.modelId, effectiveDevice: "wasm", originalRuntimeError: defaultRuntimeError });
        await rdtDisposeActivePipelineForRetry();
        try {
          await buildPipeline(id, compatLoad.modelId, compatLoad.revision, "wasm", null, "legacy-android-basic", "legacy-non-simd");
          const compatRuntimeInfo = rdtRuntimeDiagnosticInfo();
          lastLoad = {
            ...compatLoad,
            device: "wasm",
            effectiveDevice: "wasm",
            variant: null,
            dtype: pickDtype("wasm", compatLoad.modelId, null),
            wasmExecutionProfile: rdtCurrentWasmExecutionProfile,
            runtimeFallbackReason: "legacy-arm32-runtime-compatibility",
            runtimeKind: compatRuntimeInfo.runtimeKind,
            transformersPackage: compatRuntimeInfo.transformersPackage,
            onnxRuntimePackage: compatRuntimeInfo.onnxRuntimePackage,
            runtimeSelectionReason: compatRuntimeInfo.selectionReason,
            wasmSIMDSupported: compatRuntimeInfo.wasmSIMDSupported,
            architectureHint: compatRuntimeInfo.architectureHint,
            nativeAndroidABIAvailableToBrowser: false
          };
          const compatExecution = await rdtRunAudioWithActivePipeline(id, audio, opts, isAutoMixed);
          result = compatExecution.result;
          if (compatExecution.detectedLanguage) detectedLanguage = compatExecution.detectedLanguage;
          recoveredByLegacyAndroidRetry = true;
          reply({ id, type: "progress", phase: "fallback", fallbackReason: "legacy-arm32-runtime-compatibility", note: "Tiny transcription recovered using the non-SIMD-compatible runtime." });
        } catch (compatErr) {
          const compatibilityError = String(compatErr && compatErr.message !== undefined ? compatErr.message : compatErr).slice(0, 260);
          try { await rdtDisposeActivePipelineForRetry(); }
          catch (disposeErr) { rdtSetOperationStage("legacy-arm32-runtime-retry-dispose-failed", { disposeError: String(disposeErr && disposeErr.message || disposeErr).slice(0, 180) }); }
          rdtSetOperationStage("legacy-arm32-runtime-retry-failed", { originalRuntimeError: defaultRuntimeError, compatibilityRuntimeError: compatibilityError });
          throw new Error("EasyWhisper Tiny failed with the default WASM runtime and with the version-matched non-SIMD compatibility runtime. Default runtime: " + defaultRuntimeError + "; compatibility runtime: " + compatibilityError + ". No second compatibility retry will be attempted in this worker session.");
        }
      }
    }

    if (!recoveredByLegacyAndroidRetry && currentDevice === "wasm" && lastLoad && rdtIsLegacyArmAndroidTinyWasmFault(lastLoad.modelId, err) && rdtIsLegacyNonSimdRuntime()) {
      throw new Error("EasyWhisper Tiny inference also failed in the non-SIMD-compatible legacy runtime. Runtime error: " + String(err && err.message !== undefined ? err.message : err).slice(0, 220) + ". The pinned model files were not changed.");
    }

    // Runtime WebGPU failures can occur after the model loaded. Retry the whole

    // request in WASM, with actual batch progress, only in that case.

    if (rdtIsRemoteFetchFailure(err)) {
      throw new Error("EasyWhisper couldn't access a required model resource while transcribing. The current job was not retried with WASM because it needs the same resource. Try again when the connection is stable, or use Local Server. Technical details: " + String(err && err.message || err).slice(0,320));
    }
    if (rdtIsMemoryPressureFailure(err)) {
      throw new Error("WebGPU reported memory pressure during inference. WASM retry was skipped to avoid a second high-memory attempt. Try a smaller model or a shorter recording. Details: " + String(err && err.message || err).slice(0,180));
    }
    if (/couldn't identify the spoken language/i.test(String(err && err.message || err))) throw err;

    // LG LM-X320 evidence: Android 9 / armv7l / 2 GiB / no WebGPU or SAB;
    // Tiny Q8 loads successfully but ONNX inference throws a raw numeric WASM error.
    // If the one-time runtime retry was already attempted, do not stack another session.
    if (!recoveredByLegacyAndroidRetry && currentDevice === "wasm" && lastLoad && rdtIsLegacyArmAndroidTinyWasmFault(lastLoad.modelId, err) && !rdtIsLegacyNonSimdRuntime()) {
      const tinyWasmKey = modelRevisionKey(lastLoad.modelId, lastLoad.revision);
      const firstError = String(err && err.message !== undefined ? err.message : err).slice(0, 180);
      if (!rdtWasmCompatibilityPreferredModels.has(tinyWasmKey)) {
        rdtWasmCompatibilityPreferredModels.add(tinyWasmKey);
        const retryLoad = { ...lastLoad };
        reply({ id, type: "progress", phase: "fallback", fallbackReason: "legacy-android-wasm-basic-optimization", note: "Tiny loaded, but this older Android WASM runtime failed during inference. Retrying once with the version-matched compatibility runtime; the pinned INT8 files are unchanged." });
        rdtSetOperationStage("wasm-android-compatibility-retry", { modelId: retryLoad.modelId, effectiveDevice: "wasm", graphOptimizationLevel: "basic", originalRuntimeError: firstError });
        await rdtDisposeActivePipelineForRetry();
        try {
          await buildPipeline(id, retryLoad.modelId, retryLoad.revision, "wasm", null, "legacy-android-basic");
          lastLoad = { ...retryLoad, device: "wasm", effectiveDevice: "wasm", variant: null, dtype: pickDtype("wasm", retryLoad.modelId, null), wasmExecutionProfile: rdtCurrentWasmExecutionProfile };
          rdtSetOperationStage("onnx-inference-compatibility-retry", { modelId: currentModelId, effectiveDevice: currentDevice, graphOptimizationLevel: "basic" });
          const retryExecution = await rdtRunAudioWithActivePipeline(id, audio, opts, isAutoMixed);
          result = retryExecution.result;
          if (retryExecution.detectedLanguage) detectedLanguage = retryExecution.detectedLanguage;
          recoveredByLegacyAndroidRetry = true;
          reply({ id, type: "progress", phase: "fallback", fallbackReason: "legacy-android-wasm-basic-optimization", note: "Tiny transcription recovered using the conservative Android WASM compatibility profile." });
        } catch (retryErr) {
          try { await rdtDisposeActivePipelineForRetry(); }
          catch (disposeErr) { rdtSetOperationStage("wasm-android-compatibility-retry-dispose-failed", { disposeError: String(disposeErr && disposeErr.message || disposeErr).slice(0, 180) }); }
          rdtSetOperationStage("wasm-android-compatibility-retry-failed", { modelId: retryLoad.modelId, effectiveDevice: currentDevice || "wasm", graphOptimizationLevel: "basic", originalRuntimeError: firstError, retryRuntimeError: String(retryErr && retryErr.message !== undefined ? retryErr.message : retryErr).slice(0, 180) });
          throw new Error("EasyWhisper Tiny inference failed on this older Android WASM profile, including a one-time retry with conservative ONNX graph optimization. Original runtime error: " + firstError + "; compatibility retry: " + String(retryErr && retryErr.message !== undefined ? retryErr.message : retryErr).slice(0, 180));
        }
      } else if (rdtCurrentWasmExecutionProfile === "legacy-android-basic") {
        throw new Error("EasyWhisper Tiny inference failed using the conservative Android WASM profile. Runtime error: " + firstError + ". No second pipeline was loaded; the selected model files remain unchanged.");
      }
    }

    if (!recoveredByLegacyAndroidRetry) {
      if (currentDevice !== "webgpu" || !lastLoad) throw err;

      const failedLoad = { ...lastLoad };
      const failedModelKey = modelRevisionKey(failedLoad.modelId, failedLoad.revision);
      const failedPreference = rdtWebGPUSelectedPowerPreference || failedLoad.webgpuPowerPreference || "high-performance";
      rdtWebGPUFailedPreferences.add(failedPreference);
      let recoveredByLowerPowerGpu = false;

      if (failedPreference === "high-performance") {
        const lowerPowerAdapterReady = await rdtSelectWebGPUAdapter("low-power");
        if (lowerPowerAdapterReady) {
          reply({ id, type: "progress", phase: "fallback", fallbackReason: "webgpu-low-power-retry", note: "High-performance WebGPU inference failed. Retrying this audio with the lower-power WebGPU adapter before CPU/WASM." });
          transcriber = null; currentModelId = null; currentDevice = null;
          try {
            await buildPipeline(id, failedLoad.modelId, failedLoad.revision, "webgpu", failedLoad.variant);
            lastLoad = { ...failedLoad, device: "webgpu", effectiveDevice: "webgpu", webgpuPowerPreference: "low-power", gpuSelectionStrategy: "high-performance-then-low-power-then-wasm" };
            const lowerPowerExecution = await rdtRunAudioWithActivePipeline(id, audio, opts, isAutoMixed);
            result = lowerPowerExecution.result;
            if (lowerPowerExecution.detectedLanguage) detectedLanguage = lowerPowerExecution.detectedLanguage;
            recoveredByLowerPowerGpu = true;
            reply({ id, type: "progress", phase: "fallback", fallbackReason: "webgpu-low-power-retry", note: "Transcription recovered using the lower-power WebGPU adapter." });
          } catch (lowGpuErr) {
            if (rdtIsRemoteFetchFailure(lowGpuErr)) {
              throw new Error("EasyWhisper couldn't access a required model resource during the lower-power GPU retry. The job was not retried with CPU/WASM because it needs the same resource. Technical details: " + String(lowGpuErr && lowGpuErr.message || lowGpuErr).slice(0, 240));
            }
            if (rdtIsMemoryPressureFailure(lowGpuErr)) {
              throw new Error("The lower-power WebGPU retry also reported memory pressure. CPU/WASM retry was skipped to avoid another allocation after a GPU memory failure. Try a smaller model or shorter recording. Details: " + String(lowGpuErr && lowGpuErr.message || lowGpuErr).slice(0, 180));
            }
            rdtWebGPUFailedPreferences.add("low-power");
            rdtLastWebGPUFallbackError = String(lowGpuErr && lowGpuErr.message !== undefined ? lowGpuErr.message : lowGpuErr).slice(0, 220);
            rdtSetOperationStage("low-power-webgpu-inference-failed", { webgpuPowerPreference: "low-power", fallbackError: rdtLastWebGPUFallbackError });
            transcriber = null; currentModelId = null; currentDevice = null;
          }
        }
      }

      if (!recoveredByLowerPowerGpu) {
        wasmPreferredModels.add(failedModelKey);
        reply({ id, type: "progress", phase: "fallback", note: "High-performance and lower-power WebGPU attempts are exhausted or unavailable; retrying this transcription in CPU/WASM for this model until the worker restarts." });
        transcriber = null; currentModelId = null; currentDevice = null;
        await buildPipeline(id, failedLoad.modelId, failedLoad.revision, "wasm", null, rdtWasmCompatibilityPreferredModels.has(failedModelKey) ? "legacy-android-basic" : "standard");
        lastLoad = { ...failedLoad, device: "wasm", effectiveDevice: "wasm", variant: null, dtype: pickDtype("wasm", failedLoad.modelId, null), wasmExecutionProfile: rdtCurrentWasmExecutionProfile, webgpuPowerPreference: null };
        const wasmExecution = await rdtRunAudioWithActivePipeline(id, audio, opts, isAutoMixed);
        result = wasmExecution.result;
        if (wasmExecution.detectedLanguage) detectedLanguage = wasmExecution.detectedLanguage;
      }
    }

  }

  const resultChunks = (result && result.chunks) || [];
  const cleanedChunks = resultChunks.map(c => ({ ...c, text: rdtCleanRepeatedText(c && c.text || "") }));
  const cleanedText = rdtCleanRepeatedText((result && result.text) || "");

  reply({

    id, type: "result",

    text: cleanedText,

    chunks: cleanedChunks,

    durationSec: audio.length / 16000,

    elapsedMs: result.elapsedMs || 0,

    device: currentDevice,

    modelId: currentModelId,

    language: language,

    detectedLanguage: detectedLanguage || opts.language || null

  });

}
