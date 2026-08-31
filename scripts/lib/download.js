// Resumable, verified downloads.
//
// Design notes, because the obvious implementations are subtly wrong:
//
//   * Downloads land in "<name>.part" and are renamed only after the hash
//     matches. A file with the real name is therefore always a good file --
//     the app never has to wonder whether a ZIM is half-written.
//   * Resume re-hashes the bytes already on disk before appending. Carrying
//     the hash state forward is what makes resume safe; without it a resumed
//     download can only be verified by re-reading the whole file at the end.
//   * A server that ignores our Range header and replies 200 instead of 206
//     would silently append a second copy of the file from byte zero. That
//     case is detected and the partial file is discarded.

import { createWriteStream, createReadStream } from 'node:fs';
import { stat, rename, unlink, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { dirname } from 'node:path';

/** Hash an existing file, feeding an optional pre-seeded hash object. */
export async function hashFile(path, hash = createHash('sha256'), onProgress) {
  let read = 0;
  const stream = createReadStream(path);
  for await (const chunk of stream) {
    hash.update(chunk);
    read += chunk.length;
    onProgress?.(read);
  }
  return hash;
}

export async function sha256File(path, onProgress) {
  const hash = await hashFile(path, createHash('sha256'), onProgress);
  return hash.digest('hex');
}

/** True if the file exists and matches the expected size and hash. */
export async function isVerified(path, { size_bytes, sha256 }, onProgress) {
  let st;
  try {
    st = await stat(path);
  } catch {
    return false;
  }
  if (st.size !== size_bytes) return false;
  return (await sha256File(path, onProgress)) === sha256;
}

class HttpError extends Error {
  constructor(status, url) {
    super(`HTTP ${status} from ${url}`);
    this.status = status;
  }
}

/**
 * Download one source to dest, resuming a prior .part file if present and
 * falling through the mirror list on failure.
 *
 * onProgress({ received, total, phase }) is called roughly 4x/second.
 * phase is 'hashing' while re-reading a partial file, 'downloading' otherwise.
 */
export async function downloadVerified({ urls, dest, size_bytes, sha256, onProgress, signal }) {
  const part = `${dest}.part`;
  await mkdir(dirname(dest), { recursive: true });

  let lastError;
  for (const url of urls) {
    try {
      await attempt(url);
      return { url };
    } catch (err) {
      if (signal?.aborted) throw err;
      lastError = err;
      // Fall through to the next mirror. Keep the .part file: the next mirror
      // serves the same bytes, so the partial download is still useful.
    }
  }
  throw new Error(`all sources failed for ${dest}\n  last error: ${lastError?.message ?? 'unknown'}`);

  async function attempt(url) {
    let offset = 0;
    let hash = createHash('sha256');

    try {
      const st = await stat(part);
      if (st.size > 0 && st.size < size_bytes) {
        onProgress?.({ received: 0, total: st.size, phase: 'hashing' });
        hash = await hashFile(part, hash, (read) => onProgress?.({ received: read, total: st.size, phase: 'hashing' }));
        offset = st.size;
      } else if (st.size >= size_bytes) {
        // Oversized or complete-but-unrenamed: start clean rather than guess.
        await unlink(part);
      }
    } catch (err) {
      if (err.code !== 'ENOENT') throw err;
    }

    const headers = offset > 0 ? { Range: `bytes=${offset}-` } : {};
    const res = await fetch(url, { headers, signal, redirect: 'follow' });
    if (!res.ok) throw new HttpError(res.status, url);

    if (offset > 0 && res.status !== 206) {
      // Server ignored the Range request; appending would corrupt the file.
      await unlink(part).catch(() => {});
      throw new Error(`${url} does not support resume (replied ${res.status}, expected 206)`);
    }

    const out = createWriteStream(part, { flags: offset > 0 ? 'a' : 'w' });
    let received = offset;
    let lastTick = 0;

    const counted = Readable.from(
      (async function* () {
        for await (const chunk of res.body) {
          hash.update(chunk);
          received += chunk.length;
          const now = Date.now();
          if (now - lastTick > 250) {
            lastTick = now;
            onProgress?.({ received, total: size_bytes, phase: 'downloading' });
          }
          yield chunk;
        }
      })(),
    );

    await pipeline(counted, out);
    onProgress?.({ received, total: size_bytes, phase: 'downloading' });

    const st = await stat(part);
    if (st.size !== size_bytes) {
      throw new Error(`size mismatch: expected ${size_bytes} bytes, got ${st.size}`);
    }

    const digest = hash.digest('hex');
    if (digest !== sha256) {
      // A bad hash means the bytes are wrong, not merely incomplete. Keeping
      // the .part file would poison every future resume attempt.
      await unlink(part).catch(() => {});
      throw new Error(`checksum mismatch\n    expected ${sha256}\n    got      ${digest}`);
    }

    await rename(part, dest);
  }
}
