"""Kubernetes Ingress/HTTPRoute annotation updater for apex DNS."""

import structlog
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = structlog.get_logger(__name__)

# external-dns >= 0.22 reads the target from this key.
TARGET_ANNOTATION_KEY = "external-dns.kubernetes.io/target"

# Keys earlier releases also wrote: 0.3.0 wrote the alpha prefix for clusters running external-dns
# with `--annotation-prefix=external-dns.alpha.kubernetes.io/`. Every patch removes them (a merge
# patch `null`), so a stale target does not linger on the resource.
STALE_ANNOTATION_KEYS: tuple[str, ...] = ("external-dns.alpha.kubernetes.io/target",)


def update_apex_dns_annotations(
    ips: list[str],
    label_selector: str = "ginsys.net/apex-dns=true",
    dry_run: bool = False,
) -> bool:
    """
    Update the external-dns target annotation on labeled resources.

    The target is written under TARGET_ANNOTATION_KEY (`external-dns.kubernetes.io/target`),
    and the same patch removes every key in STALE_ANNOTATION_KEYS. A resource is left alone
    only when the target key already equals the target value and no stale key is present.

    Args:
        ips: List of IP addresses to set as target
        label_selector: Kubernetes label selector for finding resources
        dry_run: If True, only log what would be changed

    Returns:
        True if any resources were updated, False otherwise
    """
    if not ips:
        logger.warning("No IPs provided for kubernetes annotation update")
        return False

    target_value = ",".join(sorted(ips))
    updated = False

    logger.info(
        "Updating kubernetes resources",
        ips=ips,
        target=target_value,
        label_selector=label_selector,
        dry_run=dry_run,
    )

    try:
        # Load in-cluster config
        config.load_incluster_config()
    except config.ConfigException:
        logger.error("Failed to load in-cluster kubernetes config")
        return False

    # Update Ingresses
    updated |= _update_ingresses(
        networking_v1_api=client.NetworkingV1Api(),
        label_selector=label_selector,
        target_value=target_value,
        dry_run=dry_run,
    )

    # Update HTTPRoutes
    updated |= _update_httproutes(
        custom_api=client.CustomObjectsApi(),
        label_selector=label_selector,
        target_value=target_value,
        dry_run=dry_run,
    )

    return updated


def _current_annotations(annotations: dict[str, str]) -> dict[str, str | None]:
    """Return the target key and every stale key, each with its value or None when unset."""
    return {key: annotations.get(key) for key in (TARGET_ANNOTATION_KEY, *STALE_ANNOTATION_KEYS)}


def _is_current(current: dict[str, str | None], target_value: str) -> bool:
    """True when the target key holds the target and no stale key is present."""
    return current[TARGET_ANNOTATION_KEY] == target_value and all(
        current[key] is None for key in STALE_ANNOTATION_KEYS
    )


def _patch_body(target_value: str) -> dict[str, dict[str, dict[str, str | None]]]:
    """Set the target key and delete every stale key, in one merge patch."""
    annotations: dict[str, str | None] = dict.fromkeys(STALE_ANNOTATION_KEYS)
    annotations[TARGET_ANNOTATION_KEY] = target_value
    return {"metadata": {"annotations": annotations}}


def _previous_target(current: dict[str, str | None]) -> str | None:
    """Return the first set value in key order, keeping the `old` log field a string or null."""
    return next((value for value in current.values() if value is not None), None)


def _update_ingresses(
    networking_v1_api: client.NetworkingV1Api,
    label_selector: str,
    target_value: str,
    dry_run: bool,
) -> bool:
    """Update Ingress resources with the target annotation."""
    updated = False

    try:
        ingresses = networking_v1_api.list_ingress_for_all_namespaces(
            label_selector=label_selector
        )
    except ApiException as e:
        logger.error("Failed to list ingresses", error=str(e))
        return False

    for ing in ingresses.items:
        namespace = ing.metadata.namespace
        name = ing.metadata.name
        current = _current_annotations(ing.metadata.annotations or {})

        if _is_current(current, target_value):
            logger.debug(
                "Ingress annotation already up-to-date",
                namespace=namespace,
                name=name,
                target=target_value,
            )
            continue

        logger.info(
            "Updating ingress annotation",
            namespace=namespace,
            name=name,
            old=_previous_target(current),
            old_by_key=current,
            new=target_value,
            dry_run=dry_run,
        )

        if not dry_run:
            try:
                networking_v1_api.patch_namespaced_ingress(
                    name=name,
                    namespace=namespace,
                    body=_patch_body(target_value),
                )
                updated = True
            except ApiException as e:
                logger.error(
                    "Failed to patch ingress",
                    namespace=namespace,
                    name=name,
                    error=str(e),
                )
        else:
            updated = True

    return updated


def _update_httproutes(
    custom_api: client.CustomObjectsApi,
    label_selector: str,
    target_value: str,
    dry_run: bool,
) -> bool:
    """Update HTTPRoute resources with the target annotation."""
    updated = False

    try:
        httproutes = custom_api.list_cluster_custom_object(
            group="gateway.networking.k8s.io",
            version="v1",
            plural="httproutes",
            label_selector=label_selector,
        )
    except ApiException as e:
        logger.error("Failed to list httproutes", error=str(e))
        return False

    for route in httproutes.get("items", []):
        namespace = route["metadata"]["namespace"]
        name = route["metadata"]["name"]
        current = _current_annotations(route.get("metadata", {}).get("annotations", {}))

        if _is_current(current, target_value):
            logger.debug(
                "HTTPRoute annotation already up-to-date",
                namespace=namespace,
                name=name,
                target=target_value,
            )
            continue

        logger.info(
            "Updating httproute annotation",
            namespace=namespace,
            name=name,
            old=_previous_target(current),
            old_by_key=current,
            new=target_value,
            dry_run=dry_run,
        )

        if not dry_run:
            try:
                custom_api.patch_namespaced_custom_object(
                    group="gateway.networking.k8s.io",
                    version="v1",
                    plural="httproutes",
                    name=name,
                    namespace=namespace,
                    body=_patch_body(target_value),
                )
                updated = True
            except ApiException as e:
                logger.error(
                    "Failed to patch httproute",
                    namespace=namespace,
                    name=name,
                    error=str(e),
                )
        else:
            updated = True

    return updated
