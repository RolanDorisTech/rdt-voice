// RDT Voice - microphone PCM capture (AudioWorklet). Posts aggregated mono Float32 blocks to the page to reduce long-recording message overhead.
class RdtPcmCapture extends AudioWorkletProcessor {
  constructor() { super(); this.size = 8192; this.buf = new Float32Array(this.size); this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      let o = 0;
      while (o < ch.length) {
        const c = Math.min(ch.length - o, this.size - this.n);
        this.buf.set(ch.subarray(o, o + c), this.n);
        this.n += c; o += c;
        if (this.n === this.size) { this.port.postMessage(this.buf.slice(0)); this.n = 0; }
      }
    }
    return true;
  }
}
registerProcessor("rdt-pcm-capture", RdtPcmCapture);
