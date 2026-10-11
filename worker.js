

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

try { env.backends.onnx.logLevel = "error"; env.backends.onnx.logSeverityLevel = 3; } catch {}

// Debug logging is off unless the page sets self.RDT_DEBUG (kept out of the public console).
const RDT_DEBUG = !!self.RDT_DEBUG;


// Cache wrapper: Cache Storage entries saved from a streamed relay response carry no
// Content-Length, which makes Transformers.js warn on every load. Re-attach the size
// (taken from the stored blob, no copy into JS memory) so progress and loads are exact.
try {
  const RDT_CACHE_NAME = "transformers-cache";
  env.useCustomCache = true;
  env.customCache = {
    async match(key) {
      const cache = await caches.open(RDT_CACHE_NAME);
      const r = await cache.match(key);
      if (!r || r.headers.get("content-length") || !r.ok) return r;
      const blob = await r.blob();
      const headers = new Headers(r.headers);
      headers.set("content-length", String(blob.size));
      return new Response(blob, { status: r.status, statusText: r.statusText, headers });
    },
    async put(key, response) {
      const cache = await caches.open(RDT_CACHE_NAME);
      await cache.put(key, response);
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

const WORKER_VERSION = "2026-10-11-v13-mobile-tiny-q4-variant-fix";

if (RDT_DEBUG) console.log("[RDT worker] version", WORKER_VERSION);

let transcriber    = null;

let currentModelId = null;

let currentDevice  = null;

let lastLoad       = null;
// A real WebGPU exception switches this model revision to WASM for this worker
// session. The next page/worker session can retry WebGPU; warnings alone never
// change this set because ONNX Runtime may legitimately place some ops on CPU.
const wasmPreferredModels = new Set();
function modelRevisionKey(modelId, revision) { return String(modelId || "") + "@" + String(revision || ""); }

function rdtIsRemoteFetchFailure(err) {
  const s = String(err && err.message || err || "");
  return /failed to fetch|networkerror|network request failed|fetch failed|load failed|cors policy|access-control-allow-origin|net::err_|model relay returned HTTP (?:403|404)|HTTP 40[0-9]/i.test(s);
}
function rdtIsMemoryPressureFailure(err) {
  const s = String(err && err.message || err || "");
  return /out of memory|memory allocation|allocation failed|could not allocate|memory limit|arraybuffer allocation/i.test(s);
}


function pickDtype(device, modelId, variant) {

  if (device !== "webgpu") return "q8";

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

function sessionOptionsForDevice(device) {
  // Only surface ONNX Runtime errors (placement notices are expected and harmless).
  const base = sessionOptionsForDeviceInner(device);
  return { ...base, logSeverityLevel: 3 };
}
function sessionOptionsForDeviceInner(device) {

  if (device === "wasm") {

    return {

      enableMemPattern: false,

      enableCpuMemArena: false,

      executionMode: "sequential",

      graphOptimizationLevel: "all"

    };

  }

  return { enableMemPattern: false };

}

async function buildPipeline(id, modelId, revision, device, variant) {

  const opts = {

    dtype: pickDtype(device, modelId, variant),

    device,

    session_options: sessionOptionsForDevice(device),

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

  opts.revision = revision;
  transcriber = await pipeline("automatic-speech-recognition", modelId, opts);
  currentDevice = device;

  currentModelId = modelId;

}

function reply(msg) { self.postMessage(msg); }

let __opQueue = Promise.resolve();

function enqueue(fn){

  const next = __opQueue.then(fn, fn);

  __opQueue = next.catch(()=>{});

  return next;

}

self.onmessage = (ev) => {

  const { id, type, payload } = ev.data || {};
  const run = async () => {

    try {

      if (type === "load")         await handleLoad(id, payload || {});

      else if (type === "run")     await handleRun(id, payload || {});

      else if (type === "dispose") {

        transcriber = null;

        currentModelId = null;

        currentDevice = null;

        lastLoad = null;
        wasmPreferredModels.clear();

        reply({ id, type: "disposed" });

      } else reply({ id, type: "error", error: "unknown message type: " + type });

    } catch (err) {

      reply({ id, type: "error", error: (err && err.message) ? err.message : String(err) });

    }

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
  const modelKey = modelRevisionKey(modelId, revision);
  const rememberedWasm = requestedDevice === "webgpu" && wasmPreferredModels.has(modelKey);
  const effectiveDevice = rememberedWasm ? "wasm" : requestedDevice;

  if (!modelId) throw new Error("load: missing modelId");

  if (!/^[0-9a-f]{40}$/i.test(String(revision || ""))) {

    throw new Error("load: pinned 40-hex revision required");

  }

  const cacheExpected = expectedFilesForVariant(modelId, effectiveDevice, variant, expected);

  const cache = await inspectModelCache(modelId, revision, cacheExpected);
  const cached = cache.state === "full";

  const online = typeof navigator === "undefined" ? true : navigator.onLine !== false;

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

  lastLoad = { modelId, revision, variant, device: requestedDevice };

  // Cache-first: if fully cached, forbid remote fetch during pipeline construction

  // to avoid unnecessary HEAD/GET to huggingface.co. Restore after.

  // Must keep allowLocalModels true, otherwise "both local and remote disabled" error.

  const prevAllowRemote = env.allowRemoteModels;

  const prevAllowLocal = env.allowLocalModels;

  let restoreRemoteAfter = false;

  if (cached) {

    env.allowRemoteModels = false;

    env.allowLocalModels = true;

    restoreRemoteAfter = true;

  }

  const hasGpu = effectiveDevice === "webgpu" && typeof navigator !== "undefined" && !!navigator.gpu;
  // No WebGPU API is a capability result, not a failure: remember WASM for the
  // current model revision instead of repeatedly taking the same ineffective path.
  if (requestedDevice === "webgpu" && !hasGpu) wasmPreferredModels.add(modelKey);

  try {

    try {

      await buildPipeline(id, modelId, revision, hasGpu ? "webgpu" : "wasm", variant);

    } finally {

      if (restoreRemoteAfter) {

        env.allowRemoteModels = prevAllowRemote;

        env.allowLocalModels = prevAllowLocal;

        restoreRemoteAfter = false;

      }

    }

  } catch (err) {

    if (restoreRemoteAfter) {

      env.allowRemoteModels = prevAllowRemote;

      env.allowLocalModels = prevAllowLocal;

    }

    const loadErrorMessage = String(err && err.message || err || "");
    // If a mobile Tiny variant is missing on the relay, try the published standard
    // Tiny WebGPU pair before declaring a download failure. Other 403/404/network
    // errors still fail promptly with their diagnostic details.
    const mobileVariant404 = hasGpu && variant === "mobile-lite" && /Model relay returned HTTP 404/i.test(loadErrorMessage);
    if (rdtIsRemoteFetchFailure(err) && !mobileVariant404) {
      throw new Error("EasyWhisper could not download this model. Your recording has not been transcribed yet. Check your connection and try again, or choose a model already downloaded on this device. If the problem continues, copy the technical details for support. Details: " + loadErrorMessage.slice(0,240));
    }

    if (!hasGpu) throw err;

    if (variant === "mobile-lite") {

      reply({ id, type: "progress", phase: "fallback", note: "Light WebGPU variant unavailable; trying standard WebGPU" });

      transcriber = null; currentModelId = null; currentDevice = null;

      try {

        await buildPipeline(id, modelId, revision, "webgpu", "mobile-standard");

      } catch (gpuErr) {

        wasmPreferredModels.add(modelKey);
        reply({ id, type: "progress", phase: "fallback", note: "Both WebGPU variants failed for this model; switching to WASM for this session: " + (gpuErr && gpuErr.message || gpuErr) });

        transcriber = null; currentModelId = null; currentDevice = null;

        await buildPipeline(id, modelId, revision, "wasm", null);

      }

    } else {

      wasmPreferredModels.add(modelKey);
      reply({ id, type: "progress", phase: "fallback", note: "WebGPU model load failed; switching to WASM for this session: " + (err && err.message || err) });

      transcriber = null; currentModelId = null; currentDevice = null;

      await buildPipeline(id, modelId, revision, "wasm", null);

    }

  }

  let verify = { checked: 0, skipped: 0, failed: [] };

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

async function handleRun(id, payload) {
  if (!transcriber) throw new Error("run: model not loaded; send load first");

  const audio = normalizeAudio(payload.audio);

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

  try {

    if (isAutoMixed && audio.length > 15 * 16000) {

      // For auto with longer audio (mixed Chinese/English), detect per 30s window

      const detectedRef = { value: null };

      result = await runBatchedInferenceAuto(id, audio, opts, detectedRef);

      detectedLanguage = detectedRef.value;

    } else {

      result = await runBatchedInference(id, audio, opts);

    }

  } catch (err) {

    // Runtime WebGPU failures can occur after the model loaded. Retry the whole

    // request in WASM, with actual batch progress, only in that case.

    if (rdtIsRemoteFetchFailure(err)) {
      throw new Error("EasyWhisper couldn't access a required model resource while transcribing. The current job was not retried with WASM because it needs the same resource. Try again when the connection is stable, or use Local Server. Technical details: " + String(err && err.message || err).slice(0,320));
    }
    if (rdtIsMemoryPressureFailure(err)) {
      throw new Error("WebGPU reported memory pressure during inference. WASM retry was skipped to avoid a second high-memory attempt. Try a smaller model or a shorter recording. Details: " + String(err && err.message || err).slice(0,180));
    }
    if (/couldn't identify the spoken language/i.test(String(err && err.message || err))) throw err;
    if (currentDevice !== "webgpu" || !lastLoad) throw err;

    const failedModelKey = modelRevisionKey(lastLoad.modelId, lastLoad.revision);
    wasmPreferredModels.add(failedModelKey);
    reply({ id, type: "progress", phase: "fallback", note: "A WebGPU inference error occurred; retrying this transcription once in WASM and remembering that choice for this model until the worker restarts." });

    transcriber = null; currentModelId = null; currentDevice = null;

    await buildPipeline(id, lastLoad.modelId, lastLoad.revision, "wasm", null);
    lastLoad = { ...lastLoad, device: "wasm", variant: null };

    if (isAutoMixed && audio.length > 15 * 16000) {

      const detectedRef = { value: null };

      result = await runBatchedInferenceAuto(id, audio, opts, detectedRef);

      detectedLanguage = detectedRef.value;

    } else {

      result = await runBatchedInference(id, audio, opts);

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
