import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const runTool = (command, args) => execFileSync(command, args, {
  encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 75_000,
});
const requireX64 = (file, run) => {
  const architectures = run('lipo', ['-archs', file]).trim().split(/\s+/);
  if (architectures.length !== 1 || architectures[0] !== 'x86_64') {
    throw new Error('Candidate must contain only x86_64 code');
  }
};

export const preflight = ({ sourceRoot, outputDir, run = runTool }) => {
  const host = run('uname', ['-m']).trim();
  if (host !== 'arm64') throw new Error('Cross-build preflight requires an Apple Silicon host');
  const destination = path.resolve(outputDir);
  fs.mkdirSync(destination, { recursive: true });
  const cSource = path.join(destination, 'cross-smoke.c');
  const cObject = path.join(destination, 'cross-smoke.o');
  const swiftObject = path.join(destination, 'recording-x64.o');
  fs.writeFileSync(cSource, 'int otools_cross_smoke(void) { return 64; }\n');
  run('xcrun', ['clang', '-target', 'x86_64-apple-macosx11.0', '-c', cSource, '-o', cObject]);
  requireX64(cObject, run);
  const recording = path.join(path.resolve(sourceRoot), 'src-tauri', 'native', 'recording');
  run('xcrun', ['swiftc', '-parse-as-library', '-whole-module-optimization', '-O', '-emit-object',
    '-module-name', 'OToolsRecording', '-target', 'x86_64-apple-macosx11.0',
    path.join(recording, 'Recording.swift'), path.join(recording, 'Export.swift'), '-o', swiftObject]);
  requireX64(swiftObject, run);
  const result = { host, target: 'x86_64-apple-darwin', cArchitecture: 'x86_64', swiftArchitecture: 'x86_64' };
  fs.writeFileSync(path.join(destination, 'native-preflight.json'), JSON.stringify(result, null, 2) + '\n');
  return result;
};

export const verifyApp = ({ appPath, run = runTool }) => {
  const app = path.resolve(appPath);
  const name = run('plutil', ['-extract', 'CFBundleExecutable', 'raw', '-o', '-', path.join(app, 'Contents', 'Info.plist')]).trim();
  if (!name || name !== path.basename(name) || name === '.' || name === '..') {
    throw new Error('Invalid bundle executable name');
  }
  const executable = path.join(app, 'Contents', 'MacOS', name);
  if (!fs.statSync(executable).isFile()) throw new Error('Missing app executable');
  requireX64(executable, run);
  const libraries = run('otool', ['-L', executable]);
  for (const line of libraries.split('\n').slice(1)) {
    const library = line.trim().split(/\s+\(compatibility version/)[0];
    if (!library) continue;
    if (!library.startsWith('/System/Library/') && !library.startsWith('/usr/lib/')
      && !library.startsWith('@rpath/') && !library.startsWith('@loader_path/')
      && !library.startsWith('@executable_path/')) {
      throw new Error('Candidate contains an external machine-local library dependency');
    }
  }
  return { architecture: 'x86_64', executable };
};

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    let result;
    if (args.length === 2 && args[0] === '--app') {
      result = verifyApp({ appPath: args[1] });
    } else if (args.length === 4 && args[0] === '--source-root' && args[2] === '--output-dir') {
      result = preflight({ sourceRoot: args[1], outputDir: args[3] });
    } else {
      throw new Error('Usage: --source-root ROOT --output-dir DIR | --app APP');
    }
    console.log('[macos-x64-cross] ' + JSON.stringify(result));
  } catch {
    // Compiler/tool stderr may carry environment-derived paths; keep CI output bounded.
    console.error('[macos-x64-cross] native architecture/dependency validation failed');
    process.exitCode = 1;
  }
}
