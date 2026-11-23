import { cpSync, existsSync, mkdirSync } from "node:fs";

// SQL stays in separate files and is copied beside the compiled DB modules.
// A missing source directory (for example no tables or seeds yet) must not break the build.
for (const directory of ["tables", "seeds"]) {
  const source = new URL(`../src/db/${directory}/`, import.meta.url);
  const target = new URL(`../dist/db/${directory}/`, import.meta.url);
  mkdirSync(target, { recursive: true });
  if (existsSync(source)) cpSync(source, target, { recursive: true });
}
