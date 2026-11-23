import type { TestContext } from "node:test";
import { createDatabaseClient } from "../../src/db/client.js";
import { initializeDatabase } from "../../src/db/initialize.js";
import { hashPasswordWithSalt } from "../../src/db/password.js";
import { basename, dirname } from "node:path";

// All tests in a run share the real backend; test files and cases execute sequentially.
export function createApiTest(context: TestContext) {
  const path = process.env.ARC_API_DB_FILE;
  const baseURL = process.env.ARC_API_BASE_URL;
  if (!path || !baseURL || basename(path) !== "app.db" || !basename(dirname(path)).startsWith("arc-api-"))
    throw new Error("Use npm run test:backend or the step 08 HTTP test runner");
  const database = createDatabaseClient(path);
  try {
    database.pragma("foreign_keys = OFF");
    database.transaction(() => {
      const tables = database.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").all() as { name: string }[];
      for (const table of tables) database.exec(`DELETE FROM "${table.name.replaceAll('"', '""')}"`);
    })();
    database.pragma("foreign_keys = ON");
    initializeDatabase(database);
    context.after(() => database.close());
    return { database, baseURL };
  } catch (error) {
    database.close();
    throw error;
  }
}

// Fixture encoding policy for credential APIs. Fixture setup is not API behavior.
// Same scrypt$saltHex$digestHex format and parameters as backend/src/db/password.ts
// (hashPassword/verifyPassword) and seed_password_hash, with a fixed salt so fixture
// rows are deterministic. No test relies on an exact random hash or generated UUID.
export function fixturePasswordHash(password: string): string {
  return hashPasswordWithSalt(password, "00112233445566778899aabbccddeeff");
}
