import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { prepareTauriContext } from './prepare-tauri-context.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const snapshot = [path.join(here, 'tauri-context-runtime-acl.rs'), path.join(here, '../src-tauri/vendor/tauri-codegen/src/runtime_acl.rs')].find(p => fs.existsSync(p));
const oldBlock = `  let resolved = Resolved::resolve(&acl, capabilities, target).expect("failed to resolve ACL");

  let acl_tokens = map_lit(
    quote! { ::std::collections::BTreeMap },
    &acl,
    str_lit,
    identity,
  );

  let runtime_authority = quote!(#root::runtime_authority!(#acl_tokens, #resolved));`;
const expectedBlock = `  let runtime_authority =
    crate::runtime_acl::runtime_authority_tokens(&acl, capabilities, target, &root)?;`;
const fixture = () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'otools-tauri-context-'));
  const vendor = path.join(root, 'src-tauri/vendor/tauri-codegen');
  fs.mkdirSync(path.join(vendor, 'src'), { recursive: true });
  fs.writeFileSync(path.join(root, 'src-tauri/Cargo.toml'), '[package]\nname="OTools"\nversion="0.9.4"\n[lib]\nname="app_lib"\n');
  fs.writeFileSync(path.join(vendor, 'Cargo.toml'), '[package]\nname="tauri-codegen"\nversion="2.5.5"\n');
  const context = `use std::convert::identity;\nuse tauri_utils::{\n  acl::{\n    get_capabilities, manifest::Manifest, resolved::Resolved, ACL_MANIFESTS_FILE_NAME,\n    CAPABILITIES_FILE_NAME,\n  },\n  tokens::{map_lit, str_lit},\n};\nfn context_codegen() {\n${oldBlock}\n  let plugin_global_api_scripts = ();\n}\n`;
  const lib = 'mod context;\npub mod embedded_assets;\n';
  fs.writeFileSync(path.join(vendor, 'src/context.rs'), context);
  fs.writeFileSync(path.join(vendor, 'src/lib.rs'), lib);
  return { root, vendor, context, lib, clean: () => fs.rmSync(root, { recursive: true, force: true }) };
};

test('prepares finalized ACL generation without changing source app/package versions', () => {
  const f = fixture();
  try {
    const before = fs.readFileSync(path.join(f.root, 'src-tauri/Cargo.toml'), 'utf8');
    const result = prepareTauriContext({ sourceRoot: f.root });
    assert.equal(result.changed, true);
    const context = fs.readFileSync(path.join(f.vendor, 'src/context.rs'), 'utf8');
    assert(context.includes(expectedBlock));
    assert(!context.includes(oldBlock));
    assert(!context.includes('use std::convert::identity;'));
    assert(!context.includes('tokens::{map_lit, str_lit}'));
    assert.equal(fs.readFileSync(path.join(f.vendor, 'src/lib.rs'), 'utf8'), 'mod context;\nmod runtime_acl;\npub mod embedded_assets;\n');
    assert.equal(fs.readFileSync(path.join(f.vendor, 'src/runtime_acl.rs'), 'utf8'), fs.readFileSync(snapshot, 'utf8'));
    assert.equal(fs.readFileSync(path.join(f.root, 'src-tauri/Cargo.toml'), 'utf8'), before);
    assert.equal(result.sha256, createHash('sha256').update(fs.readFileSync(snapshot)).digest('hex'));
  } finally { f.clean(); }
});

test('is a no-op on the exact already-prepared codegen implementation', () => {
  const f = fixture();
  try {
    prepareTauriContext({ sourceRoot: f.root });
    const files = ['context.rs', 'lib.rs', 'runtime_acl.rs'];
    const before = files.map(name => fs.readFileSync(path.join(f.vendor, 'src', name), 'utf8'));
    assert.equal(prepareTauriContext({ sourceRoot: f.root }).changed, false);
    assert.deepEqual(files.map(name => fs.readFileSync(path.join(f.vendor, 'src', name), 'utf8')), before);
  } finally { f.clean(); }
});

test('rejects unknown context code before any partial source writes', () => {
  const f = fixture();
  try {
    const unexpected = f.context.replace('let runtime_authority = quote!', 'let different_authority = quote!');
    fs.writeFileSync(path.join(f.vendor, 'src/context.rs'), unexpected);
    assert.throws(() => prepareTauriContext({ sourceRoot: f.root }), /context|implementation/i);
    assert.equal(fs.readFileSync(path.join(f.vendor, 'src/context.rs'), 'utf8'), unexpected);
    assert.equal(fs.readFileSync(path.join(f.vendor, 'src/lib.rs'), 'utf8'), f.lib);
    assert.equal(fs.existsSync(path.join(f.vendor, 'src/runtime_acl.rs')), false);
  } finally { f.clean(); }
});

test('rejects unsupported vendor version and a different host identity without writes', () => {
  for (const [file, from, to] of [['Cargo.toml', '2.5.5', '3.0.0'], ['../../Cargo.toml', 'OTools', 'AnotherApp']]) {
    const f = fixture();
    try {
      const p = path.resolve(f.vendor, file);
      fs.writeFileSync(p, fs.readFileSync(p, 'utf8').replace(from, to));
      assert.throws(() => prepareTauriContext({ sourceRoot: f.root }), /vendor|OTools|version|host/i);
      assert.equal(fs.readFileSync(path.join(f.vendor, 'src/context.rs'), 'utf8'), f.context);
      assert.equal(fs.existsSync(path.join(f.vendor, 'src/runtime_acl.rs')), false);
    } finally { f.clean(); }
  }
});

test('never overwrites a concurrently different runtime ACL implementation', () => {
  const f = fixture();
  try {
    fs.writeFileSync(path.join(f.vendor, 'src/runtime_acl.rs'), '// unrelated implementation\n');
    assert.throws(() => prepareTauriContext({ sourceRoot: f.root }), /runtime.*ACL|existing|different/i);
    assert.equal(fs.readFileSync(path.join(f.vendor, 'src/runtime_acl.rs'), 'utf8'), '// unrelated implementation\n');
    assert.equal(fs.readFileSync(path.join(f.vendor, 'src/context.rs'), 'utf8'), f.context);
  } finally { f.clean(); }
});

test('CLI validates arguments instead of silently applying to the workspace', () => {
  assert.throws(() => execFileSync('node', [path.join(here, 'prepare-tauri-context.mjs')], { encoding: 'utf8', stdio: 'pipe' }), /Usage/);
});


test('source and publishing runtime ACL copies cannot drift from the same reviewed implementation', () => {
  const source = path.join(here, '../src-tauri/vendor/tauri-codegen/src/runtime_acl.rs');
  const publisher = path.join(here, '../../otools-publish/scripts/tauri-context-runtime-acl.rs');
  if (fs.existsSync(source) && fs.existsSync(publisher)) {
    assert.equal(fs.readFileSync(source, 'utf8'), fs.readFileSync(publisher, 'utf8'));
  }
});
