// Copyright Materialize, Inc. and contributors. All rights reserved.
//
// Use of this software is governed by the Business Source License
// included in the LICENSE file.
//
// As of the Change Date specified in that file, in accordance with
// the Business Source License, use of this software will be governed
// by the Apache License, Version 2.0.

// BEGIN LINT CONFIG
// DO NOT EDIT. Automatically generated by bin/gen-lints.
// Have complaints about the noise? See the note in misc/python/materialize/cli/gen-lints.py first.
#![allow(unknown_lints)]
#![allow(clippy::style)]
#![allow(clippy::complexity)]
#![allow(clippy::large_enum_variant)]
#![allow(clippy::mutable_key_type)]
#![allow(clippy::stable_sort_primitive)]
#![allow(clippy::map_entry)]
#![allow(clippy::box_default)]
#![allow(clippy::drain_collect)]
#![warn(clippy::bool_comparison)]
#![warn(clippy::clone_on_ref_ptr)]
#![warn(clippy::no_effect)]
#![warn(clippy::unnecessary_unwrap)]
#![warn(clippy::dbg_macro)]
#![warn(clippy::todo)]
#![warn(clippy::wildcard_dependencies)]
#![warn(clippy::zero_prefixed_literal)]
#![warn(clippy::borrowed_box)]
#![warn(clippy::deref_addrof)]
#![warn(clippy::double_must_use)]
#![warn(clippy::double_parens)]
#![warn(clippy::extra_unused_lifetimes)]
#![warn(clippy::needless_borrow)]
#![warn(clippy::needless_question_mark)]
#![warn(clippy::needless_return)]
#![warn(clippy::redundant_pattern)]
#![warn(clippy::redundant_slicing)]
#![warn(clippy::redundant_static_lifetimes)]
#![warn(clippy::single_component_path_imports)]
#![warn(clippy::unnecessary_cast)]
#![warn(clippy::useless_asref)]
#![warn(clippy::useless_conversion)]
#![warn(clippy::builtin_type_shadow)]
#![warn(clippy::duplicate_underscore_argument)]
#![warn(clippy::double_neg)]
#![warn(clippy::unnecessary_mut_passed)]
#![warn(clippy::wildcard_in_or_patterns)]
#![warn(clippy::crosspointer_transmute)]
#![warn(clippy::excessive_precision)]
#![warn(clippy::overflow_check_conditional)]
#![warn(clippy::as_conversions)]
#![warn(clippy::match_overlapping_arm)]
#![warn(clippy::zero_divided_by_zero)]
#![warn(clippy::must_use_unit)]
#![warn(clippy::suspicious_assignment_formatting)]
#![warn(clippy::suspicious_else_formatting)]
#![warn(clippy::suspicious_unary_op_formatting)]
#![warn(clippy::mut_mutex_lock)]
#![warn(clippy::print_literal)]
#![warn(clippy::same_item_push)]
#![warn(clippy::useless_format)]
#![warn(clippy::write_literal)]
#![warn(clippy::redundant_closure)]
#![warn(clippy::redundant_closure_call)]
#![warn(clippy::unnecessary_lazy_evaluations)]
#![warn(clippy::partialeq_ne_impl)]
#![warn(clippy::redundant_field_names)]
#![warn(clippy::transmutes_expressible_as_ptr_casts)]
#![warn(clippy::unused_async)]
#![warn(clippy::disallowed_methods)]
#![warn(clippy::disallowed_macros)]
#![warn(clippy::disallowed_types)]
#![warn(clippy::from_over_into)]
// END LINT CONFIG

use mz_catalog::durable::debug::SettingCollection;
use mz_catalog::durable::{
    persist_backed_catalog_state, test_bootstrap_args, test_stash_backed_catalog_state,
    CatalogError, DurableCatalogError, OpenableDurableCatalogState,
};
use mz_ore::collections::CollectionExt;
use mz_ore::now::NOW_ZERO;
use mz_persist_client::PersistClient;
use mz_stash::DebugStashFactory;
use mz_stash_types::objects::proto;
use uuid::Uuid;

#[mz_ore::test(tokio::test)]
#[cfg_attr(miri, ignore)] //  unsupported operation: can't call foreign function `TLS_client_method` on OS `linux`
async fn test_stash_debug() {
    let debug_factory = DebugStashFactory::new().await;
    let debug_openable_state1 = test_stash_backed_catalog_state(&debug_factory);
    let debug_openable_state2 = test_stash_backed_catalog_state(&debug_factory);
    let debug_openable_state3 = test_stash_backed_catalog_state(&debug_factory);
    test_debug(
        "stash",
        debug_openable_state1,
        debug_openable_state2,
        debug_openable_state3,
    )
    .await;
    debug_factory.drop().await;
}

#[mz_ore::test(tokio::test)]
#[cfg_attr(miri, ignore)] //  unsupported operation: can't call foreign function `TLS_client_method` on OS `linux`
async fn test_persist_debug() {
    let persist_client = PersistClient::new_for_tests().await;
    let organization_id = Uuid::new_v4();
    let persist_openable_state1 =
        persist_backed_catalog_state(persist_client.clone(), organization_id).await;
    let persist_openable_state2 =
        persist_backed_catalog_state(persist_client.clone(), organization_id).await;
    let persist_openable_state3 =
        persist_backed_catalog_state(persist_client.clone(), organization_id).await;

    test_debug(
        "persist",
        persist_openable_state1,
        persist_openable_state2,
        persist_openable_state3,
    )
    .await;
}

async fn test_debug(
    catalog_kind: &str,
    mut openable_state1: impl OpenableDurableCatalogState,
    mut openable_state2: impl OpenableDurableCatalogState,
    mut openable_state3: impl OpenableDurableCatalogState,
) {
    // Check initial empty trace.
    let err = openable_state1.trace().await.unwrap_err();
    assert_eq!(
        err.to_string(),
        CatalogError::Durable(DurableCatalogError::Uninitialized).to_string()
    );

    // Use `NOW_ZERO` for consistent timestamps in the snapshots.
    let _ = Box::new(openable_state1)
        .open(NOW_ZERO(), &test_bootstrap_args(), None)
        .await
        .unwrap();

    // Check opened trace.
    let trace = openable_state2.trace().await.unwrap();
    insta::assert_debug_snapshot!(format!("{catalog_kind}_opened_trace"), trace);

    let mut debug_state = Box::new(openable_state2).open_debug().await.unwrap();

    assert_eq!(
        openable_state3.trace().await.unwrap(),
        trace,
        "opening a debug catalog should not modify the contents"
    );

    // Check adding a new value via `edit`.
    let prev = debug_state
        .edit::<SettingCollection>(
            proto::SettingKey {
                name: "debug-key".to_string(),
            },
            proto::SettingValue {
                value: "initial".to_string(),
            },
        )
        .await
        .unwrap();
    assert_eq!(prev, None);
    let trace = openable_state3.trace().await.unwrap();
    let mut settings = trace.settings.values;
    differential_dataflow::consolidation::consolidate_updates(&mut settings);
    assert_eq!(settings.len(), 1);
    let ((key, value), _ts, diff) = settings.into_element();
    assert_eq!(
        key,
        proto::SettingKey {
            name: "debug-key".to_string(),
        }
    );
    assert_eq!(
        value,
        proto::SettingValue {
            value: "initial".to_string(),
        },
    );
    assert_eq!(diff, 1);

    // Check modifying an existing value via `edit`.
    let prev = debug_state
        .edit::<SettingCollection>(
            proto::SettingKey {
                name: "debug-key".to_string(),
            },
            proto::SettingValue {
                value: "final".to_string(),
            },
        )
        .await
        .unwrap();
    assert_eq!(
        prev,
        Some(proto::SettingValue {
            value: "initial".to_string(),
        })
    );
    let trace = openable_state3.trace().await.unwrap();
    let mut settings = trace.settings.values;
    differential_dataflow::consolidation::consolidate_updates(&mut settings);
    assert_eq!(settings.len(), 1);
    let ((key, value), _ts, diff) = settings.into_element();
    assert_eq!(
        key,
        proto::SettingKey {
            name: "debug-key".to_string(),
        }
    );
    assert_eq!(
        value,
        proto::SettingValue {
            value: "final".to_string(),
        },
    );
    assert_eq!(diff, 1);

    // Check deleting a value via `delete`.
    debug_state
        .delete::<SettingCollection>(proto::SettingKey {
            name: "debug-key".to_string(),
        })
        .await
        .unwrap();
    let trace = openable_state3.trace().await.unwrap();
    let mut settings = trace.settings.values;
    differential_dataflow::consolidation::consolidate_updates(&mut settings);
    assert_eq!(settings.len(), 0);
}
