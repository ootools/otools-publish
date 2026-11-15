import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const original = `  let resolved = Resolved::resolve(&acl, capabilities, target).expect("failed to resolve ACL");

  let acl_tokens = map_lit(
    quote! { ::std::collections::BTreeMap },
    &acl,
    str_lit,
    identity,
  );

  let runtime_authority = quote!(#root::runtime_authority!(#acl_tokens, #resolved));`;
const bounded = `  let runtime_authority =
    crate::runtime_acl::runtime_authority_tokens(&acl, capabilities, target, &root)?;`;

const readRuntimeAcl = () => {
  // The publisher carries an exact reviewed snapshot for immutable source tags.
  // Source builds use the permanent module directly, without another copy.
  const candidates = [
    path.join(here, 'tauri-context-runtime-acl.rs'),
    path.join(here, '../src-tauri/vendor/tauri-codegen/src/runtime_acl.rs'),
  ];
  const source = candidates.find(p => fs.existsSync(p));
  if (!source) throw new Error('Missing reviewed runtime ACL implementation');
  return fs.readFileSync(source, 'utf8');
};

const packageSection = text => {
  const match = text.match(/^\[package\]\s*\n([\s\S]*?)(?=^\[|$(?![\s\S]))/m);
  if (!match) throw new Error('Missing package section in host or vendor manifest');
  return match[1];
};

/** Apply only the known ACL expansion fix; refuse unknown source implementations. */
export const prepareTauriContext = ({ sourceRoot }) => {
  const root = path.resolve(sourceRoot);
  const host = packageSection(fs.readFileSync(path.join(root, 'src-tauri/Cargo.toml'), 'utf8'));
  const vendor = path.join(root, 'src-tauri/vendor/tauri-codegen');
  const identity = packageSection(fs.readFileSync(path.join(vendor, 'Cargo.toml'), 'utf8'));
  if (!/^name\s*=\s*"OTools"\s*$/m.test(host)) throw new Error('Refusing to modify a non-OTools host');
  if (!/^name\s*=\s*"tauri-codegen"\s*$/m.test(identity)
    || !/^version\s*=\s*"2\.5\.5"\s*$/m.test(identity)) {
    throw new Error('Unsupported vendored tauri-codegen version');
  }
  const contextFile = path.join(vendor, 'src/context.rs');
  const libFile = path.join(vendor, 'src/lib.rs');
  const runtimeFile = path.join(vendor, 'src/runtime_acl.rs');
  const context = fs.readFileSync(contextFile, 'utf8');
  const lib = fs.readFileSync(libFile, 'utf8');
  const runtime = readRuntimeAcl();
  const sha256 = createHash('sha256').update(runtime).digest('hex');
  const existing = fs.existsSync(runtimeFile) ? fs.readFileSync(runtimeFile, 'utf8') : null;
  if (existing !== null && existing !== runtime) throw new Error('Refusing to overwrite a different existing runtime ACL implementation');
  if (context.includes(bounded)) {
    if (context.includes(original) || !lib.includes('\nmod runtime_acl;\n') || existing !== runtime) {
      throw new Error('Incomplete existing bounded context implementation');
    }
    return { changed: false, sha256 };
  }
  if (context.split(original).length !== 2 || context.includes('runtime_authority_tokens')
    || !lib.includes('mod context;\n') || lib.includes('mod runtime_acl;')) {
    throw new Error('Unknown Tauri context implementation; refusing a partial patch');
  }
  const preparedContext = context.replace(original, bounded)
    .replace('use std::convert::identity;\n', '')
    .replace('get_capabilities, manifest::Manifest, resolved::Resolved, ACL_MANIFESTS_FILE_NAME,', 'get_capabilities, manifest::Manifest, ACL_MANIFESTS_FILE_NAME,')
    .replace('  tokens::{map_lit, str_lit},\n', '');
  const preparedLib = lib.replace('mod context;\n', 'mod context;\nmod runtime_acl;\n');
  // Validate all known inputs before the first write; never touch app/tag metadata.
  fs.writeFileSync(runtimeFile, runtime);
  fs.writeFileSync(contextFile, preparedContext);
  fs.writeFileSync(libFile, preparedLib);
  return { changed: true, sha256 };
};

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.length !== 2 || args[0] !== '--source-root' || !args[1]) {
      throw new Error('Usage: prepare-tauri-context.mjs --source-root SOURCE_ROOT');
    }
    const result = prepareTauriContext({ sourceRoot: args[1] });
    console.log(`[context-build] bounded Tauri ACL ${result.changed ? 'prepared' : 'already prepared'} (sha256:${result.sha256})`);
  } catch (error) {
    console.error(`[context-build] preparation failed: ${error.message}`);
    process.exitCode = 1;
  }
}
