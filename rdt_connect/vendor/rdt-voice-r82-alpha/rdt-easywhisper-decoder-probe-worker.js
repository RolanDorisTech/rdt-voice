/* Test-only isolated ORT Web 1.17.3 decoder staging probe. No application code is imported. */
const RUNTIME_URL = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/esm/ort.wasm.min.js';
const WASM_URL = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort-wasm.wasm';
const MODEL_ID = 'onnx-community/whisper-tiny';
const REVISION = 'ff4177021cc41f7db950912b73ea4fdf7d01d8e7';
const MODEL_PATH = 'onnx/decoder_model_merged_uint8.onnx';
const MODEL_URL = `https://huggingface.co/${MODEL_ID}/resolve/${REVISION}/${MODEL_PATH}`;
const EXPECTED_BYTES = 30719271;
const EXPECTED_SHA256 = '8dca2dedbb3aaa64e840d60d9f58f12f5b6b259f62b6dcffe4b37a4d1edce808';
const growEvents = [];
let report = null;
const t0 = performance.now();
function send(type, payload = {}) { postMessage({ type, ...payload }); }
function stage(name, message, details = {}) {
  if (report) report.stages.push({ name, elapsedMs: Math.round(performance.now() - t0), ...details });
  send('stage', { stage: name, message });
}
function safeErr(err) {
  const out = {
    type: err === null ? 'null' : typeof err,
    name: err && err.name != null ? String(err.name).slice(0, 160) : null,
    message: String(err && err.message !== undefined ? err.message : err).slice(0, 2400),
    stack: err && err.stack ? String(err.stack).slice(0, 6000) : null
  };
  if (typeof err === 'number' || (typeof err === 'string' && /^\d+$/.test(err))) {
    const n = Number(err);
    if (Number.isSafeInteger(n)) { out.numericValue = n; out.numericHex = n <= 0xffffffff ? `0x${(n >>> 0).toString(16)}` : null; }
  }
  if (err && typeof err === 'object' && err.cause != null) out.cause = String(err.cause.message || err.cause).slice(0, 1000);
  return out;
}
function simdSupport() {
  try {
    if (!WebAssembly || typeof WebAssembly.validate !== 'function') return null;
    return WebAssembly.validate(new Uint8Array([
      0x00,0x61,0x73,0x6d,0x01,0x00,0x00,0x00,
      0x01,0x04,0x01,0x60,0x00,0x00,
      0x03,0x02,0x01,0x00,
      0x0a,0x09,0x01,0x07,0x00,0x41,0x00,0xfd,0x0f,0x1a,0x0b
    ]));
  } catch { return null; }
}
function safePerfMemory() {
  try {
    const m = performance.memory;
    if (!m) return null;
    return { usedJSHeapSize: m.usedJSHeapSize ?? null, totalJSHeapSize: m.totalJSHeapSize ?? null, jsHeapSizeLimit: m.jsHeapSizeLimit ?? null };
  } catch { return null; }
}
function installMemoryGrowTrace() {
  const meta = { attempted: false, installed: false, error: null, events: growEvents };
  try {
    const W = WebAssembly;
    const OriginalMemory = W.Memory;
    if (typeof OriginalMemory !== 'function') throw new Error('WebAssembly.Memory constructor unavailable');
    class TracedMemory extends OriginalMemory {
      constructor(descriptor) {
        super(descriptor);
        const memory = this;
        const originalGrow = memory.grow;
        try {
          Object.defineProperty(memory, 'grow', {
            configurable: true,
            writable: true,
            value: function (delta) {
              const beforeBytes = memory.buffer.byteLength;
              const event = { requestedPages: Number(delta), beforeBytes, beforePages: beforeBytes / 65536, startedElapsedMs: Math.round(performance.now() - t0) };
              try {
                const previousPages = originalGrow.call(memory, delta);
                event.status = 'success'; event.previousPagesReturned = previousPages;
                event.afterBytes = memory.buffer.byteLength; event.afterPages = event.afterBytes / 65536;
                event.elapsedMs = Math.round(performance.now() - t0);
                growEvents.push(event); send('trace', { event });
                return previousPages;
              } catch (err) {
                event.status = 'threw'; event.error = safeErr(err); event.afterBytes = memory.buffer.byteLength;
                event.afterPages = event.afterBytes / 65536; event.elapsedMs = Math.round(performance.now() - t0);
                growEvents.push(event); send('trace', { event });
                throw err;
              }
            }
          });
          meta.installed = true;
        } catch (err) { meta.error = `Unable to wrap an instance grow method: ${String(err?.message || err)}`; }
      }
    }
    Object.setPrototypeOf(TracedMemory, OriginalMemory);
    W.Memory = TracedMemory;
    meta.attempted = true;
    if (W.Memory !== TracedMemory) throw new Error('WebAssembly.Memory override did not persist');
  } catch (err) { meta.error = String(err?.message || err); }
  return meta;
}
function urlWithoutQuery(raw) {
  try { const u = new URL(raw); return u.origin + u.pathname; } catch { return String(raw || '').split('?')[0].slice(0, 500); }
}
async function sha256Hex(buffer) {
  try {
    if (!crypto?.subtle) return { status: 'unavailable', value: null };
    const digest = await crypto.subtle.digest('SHA-256', buffer);
    return { status: 'computed', value: [...new Uint8Array(digest)].map(x => x.toString(16).padStart(2, '0')).join('') };
  } catch (err) { return { status: 'failed', value: null, error: String(err?.message || err).slice(0, 200) }; }
}
async function run() {
  report = {
    schema: 'rdt-easywhisper-ort-decoder-probe-v1', capturedAt: new Date().toISOString(),
    environment: {
      userAgent: navigator.userAgent || null, platform: navigator.platform || null,
      deviceMemoryGiB: Number.isFinite(Number(navigator.deviceMemory)) ? Number(navigator.deviceMemory) : null,
      logicalProcessors: Number.isFinite(Number(navigator.hardwareConcurrency)) ? Number(navigator.hardwareConcurrency) : null,
      wasmSIMD: simdSupport(), webgpuApiAvailable: !!navigator.gpu,
      crossOriginIsolated: self.crossOriginIsolated === true,
      sharedArrayBufferAvailable: typeof SharedArrayBuffer === 'function', perfMemory: safePerfMemory()
    },
    runtime: { package: 'onnxruntime-web@1.17.3', moduleUrl: RUNTIME_URL, wasmUrl: WASM_URL, build: 'WASM-only non-SIMD ESM', simd: false, numThreads: 1 },
    model: { id: MODEL_ID, revision: REVISION, path: MODEL_PATH, requestedUrl: MODEL_URL, expectedBytes: EXPECTED_BYTES, expectedSha256: EXPECTED_SHA256 },
    memoryTrace: null, stages: [], result: { status: 'started' }
  };
  try {
    stage('instrument', 'Installing test-only WebAssembly.Memory.grow tracing.');
    report.memoryTrace = installMemoryGrowTrace();
    report.memoryTrace.note = 'Instrumentation may not intercept every implementation path. No events is not proof that no growth was attempted.';

    stage('runtime-import', 'Importing only ONNX Runtime Web 1.17.3 WASM-only module.');
    const ort = await import(RUNTIME_URL);
    if (!ort?.InferenceSession || typeof ort.InferenceSession.create !== 'function' || !ort.env?.wasm) {
      throw new Error('ORT module does not expose expected InferenceSession.create/env.wasm API');
    }
    ort.env.wasm.wasmPaths = { 'ort-wasm.wasm': WASM_URL };
    ort.env.wasm.simd = false;
    ort.env.wasm.numThreads = 1;
    report.runtime.moduleExports = Object.keys(ort).filter(k => ['InferenceSession', 'Tensor', 'env', 'registerBackend'].includes(k));
    report.runtime.effectiveSettings = { wasmPaths: ort.env.wasm.wasmPaths, simd: ort.env.wasm.simd, numThreads: ort.env.wasm.numThreads };

    stage('decoder-fetch', 'Fetching the pinned UINT8 decoder only; no encoder or audio is loaded.');
    const response = await fetch(MODEL_URL, { cache: 'force-cache', credentials: 'omit' });
    report.model.httpStatus = response.status;
    report.model.ok = response.ok;
    report.model.responseUrl = urlWithoutQuery(response.url);
    report.model.contentLengthHeader = response.headers.get('content-length');
    if (!response.ok) throw new Error(`Decoder fetch failed with HTTP ${response.status}`);
    const buffer = await response.arrayBuffer();
    report.model.actualBytes = buffer.byteLength;
    if (buffer.byteLength !== EXPECTED_BYTES) throw new Error(`Decoder byte length mismatch: got ${buffer.byteLength}, expected ${EXPECTED_BYTES}`);
    stage('decoder-hash', 'Computing SHA-256 before session creation.');
    report.model.sha256 = await sha256Hex(buffer);
    report.model.hashMatchesExpected = report.model.sha256.value ? report.model.sha256.value === EXPECTED_SHA256 : null;
    if (report.model.hashMatchesExpected === false) throw new Error('Decoder SHA-256 mismatch; refusing to create a session from unexpected bytes.');
    report.environment.perfMemoryBeforeSession = safePerfMemory();
    report.memoryTrace.growEventsBeforeSession = growEvents.length;

    stage('decoder-session-create', 'Calling InferenceSession.create() for the decoder only.');
    report.sessionOptions = { executionProviders: ['wasm'], graphOptimizationLevel: 'basic', executionMode: 'sequential', enableMemPattern: false, enableCpuMemArena: false };
    const session = await ort.InferenceSession.create(new Uint8Array(buffer), report.sessionOptions);
    report.environment.perfMemoryAfterSession = safePerfMemory();
    report.memoryTrace.growEventsAtSessionSuccess = growEvents.length;
    report.result = { status: 'decoder_session_created', message: 'Decoder-only session creation succeeded. This does not yet prove encoder initialization or transcription works.' };
    stage('decoder-session-success', 'PASS: decoder-only session created. This is not yet an end-to-end transcription test.');
    try { await session.release(); report.result.sessionReleased = true; }
    catch (err) { report.result.sessionReleased = false; report.result.releaseError = safeErr(err); }
  } catch (err) {
    report.environment.perfMemoryAtFailure = safePerfMemory();
    report.memoryTrace.growEventsAtFailure = growEvents.length;
    report.result = { status: 'decoder_session_failed', error: safeErr(err), interpretation: 'Use the last completed stage and recorded grow events. Do not infer a device-wide memory ceiling from this result alone.' };
    stage('failure', `FAIL: ${report.result.error.message}`);
  }
  report.elapsedMs = Math.round(performance.now() - t0);
  report.memoryTrace.events = growEvents;
  send('done', { report });
}
self.onmessage = (event) => {
  if (event.data?.type !== 'run') return;
  self.onmessage = null;
  run().catch(err => send('fatal', { error: safeErr(err) }));
};
