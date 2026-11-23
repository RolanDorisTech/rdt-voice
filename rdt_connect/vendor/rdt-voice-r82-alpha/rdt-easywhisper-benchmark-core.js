/* RDT Voice EasyWhisper benchmark math and transcript scoring.
 * No audio, transcript, reference text, or persistent state is handled here.
 * Kept as a small UMD module so the browser app and Node regression tests use
 * the exact same metric implementation.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RDTEasyWhisperBenchmarkCore = api;
})(typeof self !== "undefined" ? self : (typeof globalThis !== "undefined" ? globalThis : this), function () {
  "use strict";

  const NORMALIZATION_PROFILE = "rdt-ew-nfc-lowercase-punctuation-space-v1";

  function finiteNumbers(values) {
    return (Array.isArray(values) ? values : [])
      .map(Number)
      .filter((value) => Number.isFinite(value) && value >= 0)
      .sort((a, b) => a - b);
  }

  function median(sortedValues) {
    const values = finiteNumbers(sortedValues);
    if (!values.length) return null;
    const middle = Math.floor(values.length / 2);
    return values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
  }

  function quantile(sortedValues, q) {
    const values = finiteNumbers(sortedValues);
    if (!values.length) return null;
    const clamped = Math.max(0, Math.min(1, Number(q)));
    const position = (values.length - 1) * clamped;
    const lower = Math.floor(position);
    const upper = Math.ceil(position);
    if (lower === upper) return values[lower];
    return values[lower] + (values[upper] - values[lower]) * (position - lower);
  }

  function summarizeSamples(values) {
    const sorted = finiteNumbers(values);
    if (!sorted.length) {
      return { sampleCount: 0, min: null, median: null, p90: null, max: null, mad: null, mean: null, coefficientOfVariation: null };
    }
    const mean = sorted.reduce((sum, value) => sum + value, 0) / sorted.length;
    const med = median(sorted);
    const deviations = sorted.map((value) => Math.abs(value - med));
    const variance = sorted.reduce((sum, value) => sum + ((value - mean) ** 2), 0) / sorted.length;
    const standardDeviation = Math.sqrt(variance);
    return {
      sampleCount: sorted.length,
      min: sorted[0],
      median: med,
      p90: quantile(sorted, 0.9),
      max: sorted[sorted.length - 1],
      mad: median(deviations),
      mean,
      coefficientOfVariation: mean > 0 ? standardDeviation / mean : null
    };
  }

  function downmixAndResampleAudioBuffer(audioBuffer, targetSampleRate = 16000) {
    if (!audioBuffer || !Number.isFinite(Number(audioBuffer.sampleRate)) ||
        !Number.isFinite(Number(audioBuffer.length)) || typeof audioBuffer.getChannelData !== "function") {
      throw new TypeError("A decoded AudioBuffer is required.");
    }
    const sourceRate = Number(audioBuffer.sampleRate);
    const sourceLength = Number(audioBuffer.length);
    const targetRate = Number(targetSampleRate);
    const channels = Number(audioBuffer.numberOfChannels) || 1;
    if (sourceRate <= 0 || sourceLength <= 0 || targetRate <= 0 || channels <= 0) {
      throw new RangeError("AudioBuffer sample rate, length, channels, and target rate must be positive.");
    }
    const outputLength = Math.max(1, Math.round(sourceLength * targetRate / sourceRate));
    const output = new Float32Array(outputLength);
    const channelData = [];
    for (let channel = 0; channel < channels; channel += 1) {
      const data = audioBuffer.getChannelData(channel);
      if (!data || data.length !== sourceLength) throw new Error("AudioBuffer channel data is incomplete.");
      channelData.push(data);
    }
    const ratio = sourceRate / targetRate;
    for (let i = 0; i < outputLength; i += 1) {
      const sourcePosition = Math.min(sourceLength - 1, i * ratio);
      const left = Math.floor(sourcePosition);
      const right = Math.min(sourceLength - 1, left + 1);
      const fraction = sourcePosition - left;
      let sample = 0;
      for (let channel = 0; channel < channels; channel += 1) {
        const data = channelData[channel];
        sample += data[left] + (data[right] - data[left]) * fraction;
      }
      output[i] = sample / channels;
    }
    return output;
  }

  function realtimeMetrics(inferenceMs, audioDurationSec) {
    const inference = Number(inferenceMs);
    const duration = Number(audioDurationSec);
    if (!Number.isFinite(inference) || inference <= 0 || !Number.isFinite(duration) || duration <= 0) {
      return { rtf: null, rtfx: null, audioSecondsPerWallSecond: null };
    }
    const rtf = inference / (duration * 1000);
    return {
      rtf,
      rtfx: 1 / rtf,
      audioSecondsPerWallSecond: duration / (inference / 1000)
    };
  }

  function normalizeTranscript(value, locale) {
    let text = String(value == null ? "" : value);
    try { text = text.normalize("NFC"); } catch (_) {}
    try { text = locale ? text.toLocaleLowerCase(locale) : text.toLowerCase(); }
    catch (_) { text = text.toLowerCase(); }
    try {
      text = text.replace(new RegExp("[\\p{P}\\p{S}]+", "gu"), " ");
    } catch (_) {
      text = text.replace(/[.,!?;:'"()\[\]{}<>，。！？；：、「」『』（）【】《》…—–-]+/g, " ");
    }
    return text.replace(/\s+/g, " ").trim();
  }

  function wordTokens(normalizedText, locale) {
    if (!normalizedText) return [];
    if (typeof Intl !== "undefined" && typeof Intl.Segmenter === "function") {
      try {
        const segmenter = new Intl.Segmenter(locale || "en", { granularity: "word" });
        return Array.from(segmenter.segment(normalizedText))
          .filter((part) => part.isWordLike)
          .map((part) => part.segment);
      } catch (_) {}
    }
    // A code-point fallback keeps CJK scoring useful on older browsers, while
    // whitespace-delimited languages retain their normal word boundaries.
    if (/[\u3400-\u9FFF\uF900-\uFAFF]/.test(normalizedText)) {
      return Array.from(normalizedText).filter((char) => !/\s/.test(char));
    }
    return normalizedText.split(/\s+/).filter(Boolean);
  }

  function editDistance(reference, hypothesis) {
    const a = Array.isArray(reference) ? reference : Array.from(String(reference || ""));
    const b = Array.isArray(hypothesis) ? hypothesis : Array.from(String(hypothesis || ""));
    if (!a.length) return b.length;
    if (!b.length) return a.length;
    let previous = new Array(b.length + 1);
    for (let j = 0; j <= b.length; j += 1) previous[j] = j;
    for (let i = 1; i <= a.length; i += 1) {
      const current = [i];
      for (let j = 1; j <= b.length; j += 1) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        current[j] = Math.min(
          current[j - 1] + 1,
          previous[j] + 1,
          previous[j - 1] + cost
        );
      }
      previous = current;
    }
    return previous[b.length];
  }

  function scoreTranscript(reference, hypothesis, locale) {
    const referenceText = normalizeTranscript(reference, locale);
    const hypothesisText = normalizeTranscript(hypothesis, locale);
    if (!referenceText) {
      return {
        status: "not-scored-no-reference",
        normalizationProfile: NORMALIZATION_PROFILE,
        wer: null,
        cer: null,
        referenceWordCount: 0,
        hypothesisWordCount: wordTokens(hypothesisText, locale).length,
        referenceCharacterCount: 0,
        hypothesisCharacterCount: Array.from(hypothesisText.replace(/\s/g, "")).length
      };
    }

    const referenceWords = wordTokens(referenceText, locale);
    const hypothesisWords = wordTokens(hypothesisText, locale);
    const referenceCharacters = Array.from(referenceText.replace(/\s/g, ""));
    const hypothesisCharacters = Array.from(hypothesisText.replace(/\s/g, ""));
    const wordErrors = editDistance(referenceWords, hypothesisWords);
    const characterErrors = editDistance(referenceCharacters, hypothesisCharacters);

    return {
      status: "scored",
      normalizationProfile: NORMALIZATION_PROFILE,
      wer: referenceWords.length ? wordErrors / referenceWords.length : null,
      cer: referenceCharacters.length ? characterErrors / referenceCharacters.length : null,
      wordErrors,
      characterErrors,
      referenceWordCount: referenceWords.length,
      hypothesisWordCount: hypothesisWords.length,
      referenceCharacterCount: referenceCharacters.length,
      hypothesisCharacterCount: hypothesisCharacters.length
    };
  }

  return Object.freeze({
    NORMALIZATION_PROFILE,
    median,
    quantile,
    summarizeSamples,
    downmixAndResampleAudioBuffer,
    realtimeMetrics,
    normalizeTranscript,
    wordTokens,
    editDistance,
    scoreTranscript
  });
});
