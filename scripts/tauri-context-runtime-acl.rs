// Copyright 2019-2024 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT

use std::collections::BTreeMap;

use proc_macro2::TokenStream;
use quote::quote;
use tauri_utils::{
  acl::{capability::Capability, manifest::Manifest, resolved::Resolved},
  platform::Target,
};

use crate::embedded_assets::EmbeddedAssetsResult;

pub(crate) fn runtime_authority_tokens(
  acl: &BTreeMap<String, Manifest>,
  capabilities: BTreeMap<String, Capability>,
  target: Target,
  root: &TokenStream,
) -> EmbeddedAssetsResult<TokenStream> {
  // Keep invalid permissions a build-time error, before embedding any data.
  Resolved::resolve(acl, capabilities.clone(), target).expect("failed to resolve ACL");
  // Expanding every command/context/window rule into constructors creates millions
  // of MIR/LLVM instructions. Embed the finalized input and reuse Tauri's resolver
  // as a bounded runtime loop instead. No capabilities or scopes are changed.
  let data = serde_json::to_vec(&(acl, &capabilities)).expect("failed to serialize ACL input");
  let data = crate::Cached::try_from(data)?;
  let target = match target {
    Target::Linux => quote!(#root::utils::platform::Target::Linux),
    Target::MacOS => quote!(#root::utils::platform::Target::MacOS),
    Target::Windows => quote!(#root::utils::platform::Target::Windows),
    Target::Android => quote!(#root::utils::platform::Target::Android),
    Target::Ios => quote!(#root::utils::platform::Target::Ios),
    _ => panic!("unsupported Tauri target for embedded ACL: {target:?}"),
  };
  Ok(quote!({
      #[inline(never)]
      fn __otools_load_runtime_authority() -> #root::ipc::RuntimeAuthority {
          let (acl, capabilities): (
              ::std::collections::BTreeMap<::std::string::String, #root::utils::acl::manifest::Manifest>,
              ::std::collections::BTreeMap<::std::string::String, #root::utils::acl::capability::Capability>,
          ) = ::serde_json::from_slice(::std::include_bytes!(#data))
              .expect("invalid embedded Tauri ACL input");
          let resolved = #root::utils::acl::resolved::Resolved::resolve(&acl, capabilities, #target)
              .expect("failed to resolve embedded Tauri ACL");
          #root::runtime_authority!(acl, resolved)
      }
      __otools_load_runtime_authority()
  }))
}

#[cfg(test)]
mod tests {
  use super::*;
  use serde_json::{json, Value};
  use std::{
    fs,
    path::PathBuf,
    sync::{
      atomic::{AtomicU64, Ordering},
      Mutex,
    },
  };

  static ENVIRONMENT: Mutex<()> = Mutex::new(());
  static SEQUENCE: AtomicU64 = AtomicU64::new(0);

  struct OutputDir {
    path: PathBuf,
    previous: Option<std::ffi::OsString>,
  }

  impl OutputDir {
    fn new() -> Self {
      let path = std::env::temp_dir().join(format!(
        "otools-acl-codegen-{}-{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
      ));
      fs::create_dir_all(&path).unwrap();
      let previous = std::env::var_os("OUT_DIR");
      // SAFETY: all tests that mutate OUT_DIR are serialized by ENVIRONMENT and
      // this suite is run with one test thread.
      unsafe { std::env::set_var("OUT_DIR", &path) };
      Self { path, previous }
    }
  }

  impl Drop for OutputDir {
    fn drop(&mut self) {
      // SAFETY: all tests that mutate OUT_DIR are serialized by ENVIRONMENT and
      // this suite is run with one test thread.
      unsafe {
        if let Some(previous) = &self.previous {
          std::env::set_var("OUT_DIR", previous);
        } else {
          std::env::remove_var("OUT_DIR");
        }
      }
      fs::remove_dir_all(&self.path).unwrap();
    }
  }

  fn fixture(command_count: usize) -> (BTreeMap<String, Manifest>, BTreeMap<String, Capability>) {
    let commands: Vec<_> = (0..command_count)
      .map(|i| format!("command_{i:04}"))
      .collect();
    let acl = json!({ "__app-acl__": {
        "permissions": {
            "app-all": { "identifier": "app-all", "commands": { "allow": commands } },
            "deny-delete": { "identifier": "deny-delete", "commands": { "deny": ["delete"] } },
            "linux-only": { "identifier": "linux-only", "platforms": ["linux"], "commands": { "allow": ["linux_command"] } },
            "scoped": { "identifier": "scoped", "commands": { "allow": ["read"] }, "scope": { "allow": [{ "path": "$HOME/files" }], "deny": [{ "path": "$HOME/private" }] } },
            "global": { "identifier": "global", "scope": { "allow": [{ "value": 7 }], "deny": [{ "value": 9 }] } }
        }, "permission_sets": {}
    }});
    let capabilities = json!({ "finalized": {
        "identifier": "finalized",
        "local": true,
        "remote": { "urls": ["https://photopea.com/*", "https://*.photopea.com/*", "http://localhost:*", "http://127.0.0.1:*", "https://localhost:*", "https://127.0.0.1:*", "file:///*"] },
        "windows": ["main", "tools-tab-*", "otools-panel", "panel-plugin-*", "capture-*", "crop-*", "custom-*"],
        "webviews": ["main", "embedded-*"],
        "permissions": ["app-all", "deny-delete", "linux-only", "scoped", "global"]
    }});
    (
      serde_json::from_value(acl).unwrap(),
      serde_json::from_value(capabilities).unwrap(),
    )
  }

  fn read_embedded(
    output: &OutputDir,
  ) -> (BTreeMap<String, Manifest>, BTreeMap<String, Capability>) {
    let files: Vec<_> = fs::read_dir(&output.path)
      .unwrap()
      .map(|p| p.unwrap().path())
      .collect();
    assert_eq!(
      files.len(),
      1,
      "ACL data must be embedded as one content-addressed payload"
    );
    serde_json::from_slice(&fs::read(&files[0]).unwrap()).unwrap()
  }

  #[test]
  fn hundreds_of_commands_do_not_unroll_context_constructors() {
    let _environment = ENVIRONMENT.lock().unwrap_or_else(|e| e.into_inner());
    let _output = OutputDir::new();
    let (acl, capabilities) = fixture(512);
    let tokens = runtime_authority_tokens(&acl, capabilities, Target::Linux, &quote!(::tauri))
      .unwrap()
      .to_string();
    assert!(
      tokens.len() < 20_000,
      "generated ACL expanded to {} bytes",
      tokens.len()
    );
    assert_eq!(tokens.matches(". parse () . unwrap ()").count(), 0);
    assert!(tokens.contains("Resolved :: resolve"));
  }

  #[test]
  fn embedded_finalized_permissions_preserve_all_acl_semantics_and_targets() {
    let _environment = ENVIRONMENT.lock().unwrap_or_else(|e| e.into_inner());
    for target in [Target::Linux, Target::MacOS, Target::Windows] {
      let output = OutputDir::new();
      let (acl, mut capabilities) = fixture(2);
      capabilities.insert(
        "additional".into(),
        serde_json::from_value(json!({
            "identifier": "additional", "local": false,
            "remote": { "urls": ["https://extra.example/*"] },
            "windows": ["extra-*"], "permissions": ["scoped"]
        }))
        .unwrap(),
      );
      let expected = Resolved::resolve(&acl, capabilities.clone(), target).unwrap();
      let tokens = runtime_authority_tokens(&acl, capabilities, target, &quote!(::tauri))
        .unwrap()
        .to_string();
      let (embedded_acl, embedded_capabilities) = read_embedded(&output);
      let actual = Resolved::resolve(&embedded_acl, embedded_capabilities, target).unwrap();
      assert_eq!(format!("{actual:?}"), format!("{expected:?}"));
      assert_eq!(
        actual.allowed_commands.contains_key("linux_command"),
        target == Target::Linux
      );
      assert!(actual.denied_commands.contains_key("delete"));
      assert!(!actual.command_scope.is_empty());
      assert!(!actual.global_scope.is_empty());
      assert!(tokens.contains(&format!("Target :: {target:?}")));
      let rules = &actual.allowed_commands["read"];
      assert!(rules
        .iter()
        .any(|rule| rule.windows.iter().any(|w| w.matches("extra-window"))));
    }
  }

  #[test]
  fn embedded_payload_is_content_addressed_and_changes_with_permissions() {
    let _environment = ENVIRONMENT.lock().unwrap_or_else(|e| e.into_inner());
    let output = OutputDir::new();
    let (acl, capabilities) = fixture(2);
    let first =
      runtime_authority_tokens(&acl, capabilities.clone(), Target::Linux, &quote!(::tauri))
        .unwrap()
        .to_string();
    let repeated = runtime_authority_tokens(&acl, capabilities, Target::Linux, &quote!(::tauri))
      .unwrap()
      .to_string();
    assert_eq!(first, repeated);
    assert_eq!(fs::read_dir(&output.path).unwrap().count(), 1);
    let (changed_acl, changed_capabilities) = fixture(3);
    let changed = runtime_authority_tokens(
      &changed_acl,
      changed_capabilities,
      Target::Linux,
      &quote!(::tauri),
    )
    .unwrap()
    .to_string();
    assert_ne!(first, changed);
    assert_eq!(fs::read_dir(&output.path).unwrap().count(), 2);
  }

  #[test]
  fn invalid_permission_is_rejected_during_generation_before_embedding() {
    let _environment = ENVIRONMENT.lock().unwrap_or_else(|e| e.into_inner());
    let output = OutputDir::new();
    let (acl, mut capabilities) = fixture(1);
    capabilities
      .get_mut("finalized")
      .unwrap()
      .permissions
      .push(serde_json::from_value(Value::String("missing-permission".into())).unwrap());
    let failure = std::panic::catch_unwind(|| {
      runtime_authority_tokens(&acl, capabilities, Target::Linux, &quote!(::tauri)).unwrap();
    });
    assert!(
      failure.is_err(),
      "invalid permission must fail before startup"
    );
    assert_eq!(fs::read_dir(&output.path).unwrap().count(), 0);
  }
}
