import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const section = (text, name) => {
  const header = new RegExp(`^\\[${name}\\]\\s*(?:#.*)?$`, 'm').exec(text);
  if (!header) throw new Error(`Missing required [${name}] section`);
  const start = header.index + header[0].length;
  const tail = text.slice(start);
  const next = /^\s*\[(?!\[)/m.exec(tail);
  return { start, body: next ? tail.slice(0, next.index) : tail };
};

/** Reduce only the known host library's redundant desktop linkage outputs. */
export const prepareIntelHost = ({ sourceRoot }) => {
  const manifest = path.join(path.resolve(sourceRoot), 'src-tauri', 'Cargo.toml');
  const text = fs.readFileSync(manifest, 'utf8');
  const packageSection = section(text, 'package');
  const lib = section(text, 'lib');
  if (!/^name\s*=\s*"OTools"\s*(?:#.*)?$/m.test(packageSection.body)
    || !/^name\s*=\s*"app_lib"\s*(?:#.*)?$/m.test(lib.body)) {
    throw new Error('Refusing to modify a manifest other than OTools/app_lib');
  }
  const declaration = /^crate-type\s*=\s*\[([^\]]*)\]/m.exec(lib.body);
  if (!declaration) throw new Error('Missing host library crate-type declaration');
  const values = declaration[1].replace(/#[^\r\n]*/g, '').trim();
  if (!/^\s*"(?:staticlib|cdylib|rlib)"(?:\s*,\s*"(?:staticlib|cdylib|rlib)")*\s*,?\s*$/.test(values)) {
    throw new Error('Unexpected host library crate types');
  }
  const crateTypes = [...values.matchAll(/"([^"]+)"/g)].map(match => match[1]);
  if (!crateTypes.includes('rlib')) throw new Error('Host library must retain rlib');
  const changed = crateTypes.length !== 1 || crateTypes[0] !== 'rlib';
  if (changed) {
    const start = lib.start + declaration.index;
    const end = start + declaration[0].length;
    fs.writeFileSync(manifest, text.slice(0, start) + 'crate-type = ["rlib"]' + text.slice(end));
  }
  return { changed, crateTypes: ['rlib'] };
};

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.length !== 2 || args[0] !== '--source-root' || !args[1]) {
      throw new Error('Usage: prepare-intel-host.mjs --source-root SOURCE_ROOT');
    }
    const result = prepareIntelHost({ sourceRoot: args[1] });
    console.log(`[intel-build] desktop host linkage: rlib (${result.changed ? 'removed redundant native library outputs' : 'already prepared'})`);
  } catch (error) {
    console.error(`[intel-build] preparation failed: ${error.message}`);
    process.exitCode = 1;
  }
}
