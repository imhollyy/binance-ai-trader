import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

const root = path.resolve(".next/static");
const forbidden = [
  "X-Admin-Token",
  "E2E_ADMIN_TOKEN",
  "Yerel ADMIN_TOKEN girilmedi.",
  "Yerel API Bağlantısı",
  "ADMIN_TOKEN",
];

async function filesUnder(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const entryPath = path.join(directory, entry.name);
      return entry.isDirectory() ? filesUnder(entryPath) : [entryPath];
    }),
  );
  return nested.flat();
}

const files = await filesUnder(root);
const findings = [];
for (const file of files) {
  const content = await readFile(file);
  for (const marker of forbidden) {
    if (content.includes(Buffer.from(marker))) findings.push(`${file}: ${marker}`);
  }
}

if (findings.length > 0) {
  console.error(`Staging bundle contains local admin markers:\n${findings.join("\n")}`);
  process.exitCode = 1;
} else {
  console.log(`Staging bundle clean: scanned ${files.length} static files.`);
}
