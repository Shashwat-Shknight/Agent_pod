/**
 * Microphone capture to 16 kHz mono 16-bit WAV.
 *
 * Browsers' MediaRecorder produces WebM/Ogg, which the server's speech engine (SpeechRecognition) cannot
 * read. Encoding WAV here keeps the microphone path working without any server-side converter (no ffmpeg).
 */

const TARGET_RATE = 16_000;
export const MAX_SECONDS = 15 * 60; // ~29 MB at 16 kHz would exceed the 25 MB upload cap, so stop at 15 min

export interface WavCapture {
  stop: () => Promise<Blob>;
  cancel: () => void;
  level: () => number;
  seconds: () => number;
}

export function microphoneSupported(): boolean {
  return Boolean(navigator.mediaDevices?.getUserMedia) && typeof window.AudioContext !== "undefined";
}

export async function startWavCapture(): Promise<WavCapture> {
  if (!microphoneSupported()) throw new Error("This browser cannot record from the microphone. Upload a WAV file or paste the conversation.");
  if (!window.isSecureContext) throw new Error("Microphone access needs https or http://localhost. Open the app via localhost.");
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  } catch (error) {
    const name = (error as DOMException)?.name;
    if (name === "NotAllowedError") throw new Error("Microphone permission was denied. Allow it in the browser address bar and try again.");
    if (name === "NotFoundError") throw new Error("No microphone was found on this computer.");
    throw new Error("The microphone could not be started.");
  }
  const context = new AudioContext();
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(4096, 1, 1);
  const chunks: Float32Array[] = [];
  let peak = 0;
  let samples = 0;
  processor.onaudioprocess = (event) => {
    const data = event.inputBuffer.getChannelData(0);
    chunks.push(new Float32Array(data));
    samples += data.length;
    let max = 0;
    for (let i = 0; i < data.length; i += 16) max = Math.max(max, Math.abs(data[i]));
    peak = max;
  };
  source.connect(processor);
  processor.connect(context.destination);

  const release = () => {
    processor.disconnect();
    source.disconnect();
    stream.getTracks().forEach((track) => track.stop());
    void context.close();
  };

  return {
    level: () => peak,
    seconds: () => samples / context.sampleRate,
    cancel: release,
    stop: async () => {
      const rate = context.sampleRate;
      release();
      return encodeWav(downsample(merge(chunks), rate, TARGET_RATE), TARGET_RATE);
    },
  };
}

function merge(chunks: Float32Array[]): Float32Array {
  const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

function downsample(input: Float32Array, from: number, to: number): Float32Array {
  if (from === to) return input;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(Math.floor((i + 1) * ratio), input.length);
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

export function encodeWav(samples: Float32Array, rate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const write = (offset: number, text: string) => [...text].forEach((ch, i) => view.setUint8(offset + i, ch.charCodeAt(0)));
  write(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  write(8, "WAVE");
  write(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}
