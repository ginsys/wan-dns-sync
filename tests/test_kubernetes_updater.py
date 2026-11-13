"""Tests for the kubernetes apex DNS annotation updater."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException
from structlog.testing import capture_logs
from urllib3.exceptions import ReadTimeoutError

from wan_dns_sync.kubernetes_updater import (
    STALE_ANNOTATION_KEYS,
    TARGET_ANNOTATION_KEY,
    KubernetesUpdateError,
    _update_httproutes,
    _update_ingresses,
    update_apex_dns_annotations,
)

NEW_KEY = "external-dns.kubernetes.io/target"
ALPHA_KEY = "external-dns.alpha.kubernetes.io/target"
TARGET = "1.2.3.4,5.6.7.8"
STALE = "9.9.9.9"
SELECTOR = "ginsys.net/apex-dns=true"
EXPECTED_BODY = {"metadata": {"annotations": {NEW_KEY: TARGET, ALPHA_KEY: None}}}


def make_networking_api(annotations: dict[str, str] | None) -> MagicMock:
    """Create a NetworkingV1Api stand-in listing one ingress with the given annotations."""
    ing = MagicMock()
    ing.metadata.namespace = "web"
    ing.metadata.name = "apex"
    ing.metadata.annotations = annotations

    api = MagicMock()
    api.list_ingress_for_all_namespaces.return_value.items = [ing]
    return api


def make_custom_api(annotations: dict[str, str] | None) -> MagicMock:
    """Create a CustomObjectsApi stand-in listing one HTTPRoute with the given annotations."""
    metadata: dict[str, Any] = {"namespace": "web", "name": "apex"}
    if annotations is not None:
        metadata["annotations"] = annotations

    api = MagicMock()
    api.list_cluster_custom_object.return_value = {"items": [{"metadata": metadata}]}
    return api


def run_ingresses(api: MagicMock, dry_run: bool = False) -> bool:
    """Run the ingress updater against a stand-in API."""
    return _update_ingresses(
        networking_v1_api=api,
        label_selector=SELECTOR,
        target_value=TARGET,
        dry_run=dry_run,
    )


def run_httproutes(api: MagicMock, dry_run: bool = False) -> bool:
    """Run the HTTPRoute updater against a stand-in API."""
    return _update_httproutes(
        custom_api=api,
        label_selector=SELECTOR,
        target_value=TARGET,
        dry_run=dry_run,
    )


def assert_patched(patch_call: MagicMock) -> None:
    """Assert exactly one patch was sent: the new key set, the alpha key deleted."""
    patch_call.assert_called_once()
    assert patch_call.call_args.kwargs["body"] == EXPECTED_BODY
    assert patch_call.call_args.kwargs["name"] == "apex"
    assert patch_call.call_args.kwargs["namespace"] == "web"


class TestAnnotationKeys:
    """Tests for the key constants and the wire form of the patch."""

    def test_keys(self) -> None:
        """Only the new prefix is written; the alpha prefix is the one stale key."""
        assert TARGET_ANNOTATION_KEY == NEW_KEY
        assert STALE_ANNOTATION_KEYS == (ALPHA_KEY,)

    def test_stale_key_serializes_as_null(self) -> None:
        """The client keeps the None value, so the merge patch carries a JSON null (a delete)."""
        wire = ApiClient().sanitize_for_serialization(EXPECTED_BODY)
        assert json.loads(json.dumps(wire)) == {
            "metadata": {"annotations": {NEW_KEY: TARGET, ALPHA_KEY: None}}
        }


class TestUpdateIngresses:
    """Tests for _update_ingresses."""

    def test_no_annotations_patched(self) -> None:
        """An ingress with no annotations gets one patch setting the new key."""
        api = make_networking_api(None)

        assert run_ingresses(api) is True
        assert_patched(api.patch_namespaced_ingress)

    def test_new_key_current_not_patched(self) -> None:
        """An ingress with the new key at the target and no alpha key is left alone."""
        api = make_networking_api({NEW_KEY: TARGET, "other": "x"})

        assert run_ingresses(api) is False
        api.patch_namespaced_ingress.assert_not_called()

    def test_both_keys_current_removes_alpha(self) -> None:
        """An ingress carrying both keys at the target (as 0.3.0 left it) loses the alpha key."""
        api = make_networking_api({NEW_KEY: TARGET, ALPHA_KEY: TARGET})

        assert run_ingresses(api) is True
        assert_patched(api.patch_namespaced_ingress)

    def test_only_alpha_key_patched(self) -> None:
        """An ingress with only the alpha key at the target gets the new key, alpha removed."""
        api = make_networking_api({ALPHA_KEY: TARGET})

        assert run_ingresses(api) is True
        assert_patched(api.patch_namespaced_ingress)

    def test_new_key_stale_patched(self) -> None:
        """An ingress whose new key holds an old target is patched."""
        api = make_networking_api({NEW_KEY: STALE})

        assert run_ingresses(api) is True
        assert_patched(api.patch_namespaced_ingress)

    def test_dry_run_does_not_patch(self) -> None:
        """Dry run reports an update without patching."""
        api = make_networking_api({ALPHA_KEY: STALE})

        assert run_ingresses(api, dry_run=True) is True
        api.patch_namespaced_ingress.assert_not_called()

    def test_update_log_keeps_scalar_old(self) -> None:
        """The `old` log field stays a string; per-key values go to `old_by_key`."""
        api = make_networking_api({ALPHA_KEY: STALE})

        with capture_logs() as logs:
            run_ingresses(api, dry_run=True)

        event = next(e for e in logs if e["event"] == "Updating ingress annotation")
        assert event["old"] == STALE
        assert event["old_by_key"] == {NEW_KEY: None, ALPHA_KEY: STALE}


class TestUpdateHttproutes:
    """Tests for _update_httproutes."""

    def test_no_annotations_patched(self) -> None:
        """An HTTPRoute with no annotations gets one patch setting the new key."""
        api = make_custom_api(None)

        assert run_httproutes(api) is True
        assert_patched(api.patch_namespaced_custom_object)

    def test_new_key_current_not_patched(self) -> None:
        """An HTTPRoute with the new key at the target and no alpha key is left alone."""
        api = make_custom_api({NEW_KEY: TARGET})

        assert run_httproutes(api) is False
        api.patch_namespaced_custom_object.assert_not_called()

    def test_both_keys_current_removes_alpha(self) -> None:
        """An HTTPRoute carrying both keys at the target (as 0.3.0 left it) loses the alpha key."""
        api = make_custom_api({NEW_KEY: TARGET, ALPHA_KEY: TARGET})

        assert run_httproutes(api) is True
        assert_patched(api.patch_namespaced_custom_object)

    def test_only_alpha_key_patched(self) -> None:
        """An HTTPRoute with only the alpha key at the target gets the new key, alpha removed."""
        api = make_custom_api({ALPHA_KEY: TARGET})

        assert run_httproutes(api) is True
        assert_patched(api.patch_namespaced_custom_object)

    def test_update_log_keeps_scalar_old(self) -> None:
        """The `old` log field is null when no key is set; per-key values go to `old_by_key`."""
        api = make_custom_api(None)

        with capture_logs() as logs:
            run_httproutes(api, dry_run=True)

        event = next(e for e in logs if e["event"] == "Updating httproute annotation")
        assert event["old"] is None
        assert event["old_by_key"] == {NEW_KEY: None, ALPHA_KEY: None}


class TestUpdateApexDnsAnnotations:
    """Tests for update_apex_dns_annotations wiring."""

    def test_patches_both_resource_kinds(self) -> None:
        """The public entry point patches ingresses and HTTPRoutes."""
        networking_api = make_networking_api(None)
        custom_api = make_custom_api(None)

        with (
            patch("wan_dns_sync.kubernetes_updater.config.load_incluster_config"),
            patch(
                "wan_dns_sync.kubernetes_updater.client.NetworkingV1Api",
                return_value=networking_api,
            ),
            patch(
                "wan_dns_sync.kubernetes_updater.client.CustomObjectsApi",
                return_value=custom_api,
            ),
        ):
            assert update_apex_dns_annotations(["5.6.7.8", "1.2.3.4"], SELECTOR) is True

        assert_patched(networking_api.patch_namespaced_ingress)
        assert_patched(custom_api.patch_namespaced_custom_object)

    def test_a_failing_ingress_does_not_stop_the_httproutes(self) -> None:
        networking_api = make_networking_api(None)
        networking_api.patch_namespaced_ingress.side_effect = ApiException(status=403)
        custom_api = make_custom_api(None)

        with (
            patch("wan_dns_sync.kubernetes_updater.config.load_incluster_config"),
            patch(
                "wan_dns_sync.kubernetes_updater.client.NetworkingV1Api",
                return_value=networking_api,
            ),
            patch(
                "wan_dns_sync.kubernetes_updater.client.CustomObjectsApi",
                return_value=custom_api,
            ),
            pytest.raises(KubernetesUpdateError, match="ingress"),
        ):
            update_apex_dns_annotations(["5.6.7.8", "1.2.3.4"], SELECTOR)

        assert_patched(custom_api.patch_namespaced_custom_object)

    def test_a_transport_error_on_ingresses_does_not_stop_the_httproutes(self) -> None:
        """urllib3 errors are not ApiExceptions and the client does not wrap them."""
        networking_api = make_networking_api(None)
        networking_api.list_ingress_for_all_namespaces.side_effect = ReadTimeoutError(
            None, "/apis/networking.k8s.io/v1/ingresses", "read timed out"
        )
        custom_api = make_custom_api(None)

        with (
            patch("wan_dns_sync.kubernetes_updater.config.load_incluster_config"),
            patch(
                "wan_dns_sync.kubernetes_updater.client.NetworkingV1Api",
                return_value=networking_api,
            ),
            patch(
                "wan_dns_sync.kubernetes_updater.client.CustomObjectsApi",
                return_value=custom_api,
            ),
            pytest.raises(KubernetesUpdateError, match="ReadTimeoutError while updating ingresses"),
        ):
            update_apex_dns_annotations(["5.6.7.8", "1.2.3.4"], SELECTOR)

        assert_patched(custom_api.patch_namespaced_custom_object)
