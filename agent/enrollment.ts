import { createInterface } from 'node:readline';
import type { Readable } from 'node:stream';

export async function readEnrollmentToken(input: Readable = process.stdin): Promise<string> {
  const lines = createInterface({input, terminal: false, crlfDelay: Infinity});
  try {
    for await (const line of lines) {
      const token = line.trim();
      if (!token) throw new Error('Enrollment token is empty. Paste a token from Workers and press Enter.');
      if (token.length > 4096) throw new Error('Enrollment token too long');
      return token;
    }
    throw new Error('No enrollment token received. Paste a token from Workers and press Enter.');
  } finally {
    lines.close();
    input.pause();
  }
}
