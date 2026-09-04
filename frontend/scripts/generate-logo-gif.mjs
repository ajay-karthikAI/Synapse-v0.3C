import fs from "node:fs/promises";
import path from "node:path";
import sharp from "sharp";

const width = 200;
const height = 200;
const frames = 30;
const frameDelay = 60;
const output = path.resolve("public/synapse-logo.gif");
const points = "8,100 65,100 78,100 92,36 108,164 122,100 138,100 192,100";

const palette = [];
for (let r = 0; r < 6; r += 1) {
  for (let g = 0; g < 6; g += 1) {
    for (let b = 0; b < 6; b += 1) {
      palette.push([Math.round((r * 255) / 5), Math.round((g * 255) / 5), Math.round((b * 255) / 5)]);
    }
  }
}
const transparentIndex = 216;
while (palette.length < 256) palette.push([0, 0, 0]);

function svg(progress) {
  const offset = Math.max(0, 600 * (1 - Math.min(1, progress)));
  const opacity = progress < 0.72 ? 0.48 + (progress / 0.72) * 0.52 : 1;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 200 200">
    <defs>
      <linearGradient id="stroke" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" stop-color="#39248F"/><stop offset="36%" stop-color="#5B3FD6"/>
        <stop offset="90%" stop-color="#8B78E8"/><stop offset="94%" stop-color="#FFFFFF"/><stop offset="100%" stop-color="#FFFFFF"/>
      </linearGradient>
      <linearGradient id="highlight" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" stop-color="#FFFFFF" stop-opacity="0"/><stop offset="88%" stop-color="#FFFFFF" stop-opacity="0"/>
        <stop offset="94%" stop-color="#FFFFFF" stop-opacity="1"/><stop offset="100%" stop-color="#FFFFFF" stop-opacity="1"/>
      </linearGradient>
      <filter id="glow" x="-24%" y="-44%" width="148%" height="188%" color-interpolation-filters="sRGB">
        <feGaussianBlur in="SourceGraphic" stdDeviation="4" result="wide"/><feGaussianBlur in="SourceGraphic" stdDeviation="1.4" result="tight"/>
        <feMerge><feMergeNode in="wide"/><feMergeNode in="wide"/><feMergeNode in="tight"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="bloom" x="-300%" y="-300%" width="700%" height="700%" color-interpolation-filters="sRGB">
        <feGaussianBlur in="SourceGraphic" stdDeviation="7" result="wide"/><feGaussianBlur in="SourceGraphic" stdDeviation="2.6" result="tight"/>
        <feMerge><feMergeNode in="wide"/><feMergeNode in="wide"/><feMergeNode in="tight"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
    </defs>
    <polyline points="${points}" fill="none" stroke="url(#stroke)" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" pathLength="600" stroke-dasharray="600" stroke-dashoffset="${offset}" opacity="${opacity}" filter="url(#glow)"/>
    <polyline points="${points}" fill="none" stroke="url(#highlight)" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" pathLength="600" stroke-dasharray="600" stroke-dashoffset="${offset}" opacity="${opacity}" filter="url(#bloom)"/>
  </svg>`;
}

function lzw(indices) {
  const clear = 1 << 8;
  const end = clear + 1;
  // Emit each pixel as a one-item phrase, resetting the dictionary between
  // pixels. It is deliberately simple and produces a larger but universally
  // decodable GIF, which is appropriate for this small logo asset.
  const codes = [];
  for (const index of indices) codes.push(clear, index);
  codes.push(end);
  const bytes = [];
  let buffer = 0;
  let bits = 0;
  for (const value of codes) {
    buffer |= value << bits; bits += 9;
    while (bits >= 8) { bytes.push(buffer & 255); buffer >>= 8; bits -= 8; }
  }
  if (bits) bytes.push(buffer & 255);
  return Buffer.from(bytes);
}

function subBlocks(data) {
  const chunks = [];
  for (let i = 0; i < data.length; i += 255) {
    const chunk = data.subarray(i, i + 255);
    chunks.push(Buffer.from([chunk.length]), chunk);
  }
  chunks.push(Buffer.from([0]));
  return Buffer.concat(chunks);
}

function frame(raw, progress) {
  const indices = new Uint8Array(width * height);
  for (let i = 0, p = 0; i < raw.length; i += 4, p += 1) {
    if (raw[i + 3] < 16) indices[p] = transparentIndex;
    else indices[p] = Math.min(215, Math.round(raw[i] / 51) * 36 + Math.round(raw[i + 1] / 51) * 6 + Math.round(raw[i + 2] / 51));
  }
  const gce = Buffer.from([0x21, 0xf9, 4, 1, Math.round(frameDelay / 10) & 255, Math.round(frameDelay / 10) >> 8, transparentIndex, 0]);
  const descriptor = Buffer.from([0x2c, 0, 0, 0, 0, width & 255, width >> 8, height & 255, height >> 8, 0]);
  return Buffer.concat([gce, descriptor, Buffer.from([8]), subBlocks(lzw(indices))]);
}

const rendered = [];
for (let i = 0; i < frames; i += 1) {
  const progress = Math.min(1, Math.max(0, (i * frameDelay - 180) / 1200));
  rendered.push(await sharp(Buffer.from(svg(progress))).ensureAlpha().raw().toBuffer());
}

await fs.mkdir(path.dirname(output), { recursive: true });
const header = Buffer.from("GIF89a", "ascii");
const logicalScreen = Buffer.from([width & 255, width >> 8, height & 255, height >> 8, 0xf7, 0, transparentIndex]);
const globalTable = Buffer.from(palette.flat());
const loop = Buffer.from([0x21, 0xff, 11, ...Buffer.from("NETSCAPE2.0", "ascii"), 3, 1, 0, 0, 0]);
const trailer = Buffer.from([0x3b]);
await fs.writeFile(output, Buffer.concat([header, logicalScreen, globalTable, loop, ...rendered.map((raw, i) => frame(raw, i / (frames - 1))), trailer]));
// eslint-disable-next-line no-console -- a build script's only output channel
console.log(`Wrote ${output}`);
