import { randomBytes, scryptSync, timingSafeEqual } from "node:crypto";

// Compiler-owned password encoding shared by seeds, credential handlers and test fixtures:
// scrypt$<16-byte salt hex>$<64-byte scrypt digest hex>, default scrypt cost parameters.
export const PASSWORD_SALT_BYTES = 16;
export const PASSWORD_DIGEST_BYTES = 64;

const HEX = /^(?:[0-9a-f]{2})+$/i;

/** Encode a password with an explicit hex salt (deterministic; used by test fixtures). */
export function hashPasswordWithSalt(password: string, saltHex: string): string {
  if (typeof password !== "string") throw new TypeError("Password must be a string");
  if (typeof saltHex !== "string" || !HEX.test(saltHex)) throw new TypeError("Salt must be non-empty hex");
  const digest = scryptSync(password, Buffer.from(saltHex, "hex"), PASSWORD_DIGEST_BYTES).toString("hex");
  return `scrypt$${saltHex.toLowerCase()}$${digest}`;
}

/** Hash a password for storage with a fresh random salt. */
export function hashPassword(password: string): string {
  return hashPasswordWithSalt(password, randomBytes(PASSWORD_SALT_BYTES).toString("hex"));
}

/** Check a password against a stored scrypt$salt$digest value; false for malformed values. */
export function verifyPassword(password: string, stored: unknown): boolean {
  if (typeof password !== "string" || typeof stored !== "string") return false;
  const parts = stored.split("$");
  if (parts.length !== 3 || parts[0] !== "scrypt") return false;
  const [, saltHex, digestHex] = parts;
  if (!HEX.test(saltHex) || !HEX.test(digestHex)) return false;
  try {
    const expected = Buffer.from(digestHex, "hex");
    const actual = scryptSync(password, Buffer.from(saltHex, "hex"), expected.length);
    return actual.length === expected.length && timingSafeEqual(actual, expected);
  } catch {
    return false;
  }
}
