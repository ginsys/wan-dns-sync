"""Main entry point for wan-dns-sync."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from http.server import HTTPServer
from pathlib import Path

import structlog

from .config import DEFAULT_CONFIG_PATH, Config, load_config_auto
from .health import start_health_server
from .hetzner import HetznerDNSClient
from .kubernetes_updater import update_apex_dns_annotations
from .opnsense import OPNsenseClient
from .verify import verify_a_records

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def configure_logging(level: str) -> None:
    """Configure structlog for stdout logging."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            LOG_LEVELS.get(level.lower(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Dynamic DNS updater for Hetzner Cloud DNS using OPNsense WAN IPs"
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH} if exists, else env vars)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't make changes, just log what would be done",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
        help="Logging level (default: info)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't loop)",
    )
    return parser.parse_args()


class GracefulShutdown:
    """Handle graceful shutdown on SIGTERM/SIGINT."""

    def __init__(self) -> None:
        self.should_exit = False
        signal.signal(signal.SIGTERM, self._handler)
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, signum: int, frame: object) -> None:
        logger = structlog.get_logger()
        logger.info("Received shutdown signal", signal=signum)
        self.should_exit = True


def run_update(
    config: Config,
    opnsense: OPNsenseClient,
    hetzner: HetznerDNSClient,
    dry_run: bool,
) -> None:
    """Run a single update cycle."""
    logger = structlog.get_logger()
    any_changes = False

    # Get current IPs from OPNsense
    try:
        interface_ips = opnsense.get_interface_ips()
    except Exception as e:
        logger.error("Failed to get IPs from OPNsense", error=str(e))
        return

    if not interface_ips:
        logger.warning("No interface IPs found")
        return

    logger.debug("Retrieved interface IPs", ips=interface_ips)

    # Process each record
    for record in config.records:
        # Gather IPs for this record's interfaces
        desired_ips: list[str] = []
        missing_interfaces: list[str] = []

        for iface in record.interfaces:
            if iface in interface_ips:
                desired_ips.append(interface_ips[iface])
            else:
                missing_interfaces.append(iface)

        if missing_interfaces:
            logger.warning(
                "Missing interfaces for record",
                hostname=record.hostname,
                missing=missing_interfaces,
            )

        desired_ips = sorted(set(desired_ips))

        if not desired_ips:
            logger.warning(
                "No IPs available for record, skipping",
                hostname=record.hostname,
            )
            continue

        # Sync DNS records
        try:
            changed = hetzner.sync_a_records(
                record.hostname, desired_ips, dry_run=dry_run
            )

            if changed:
                any_changes = True

            # Verify DNS if changes were made (and not dry run)
            if changed and not dry_run:
                # Wait for DNS propagation before verification
                time.sleep(config.settings.verify_delay)
                verify_a_records(
                    record.hostname,
                    config.hetzner.zone,
                    set(desired_ips),
                )

            # If kubernetes integration is enabled and this is the trigger hostname,
            # reconcile annotations
            if (
                config.kubernetes.enabled
                and record.hostname == config.kubernetes.trigger_hostname
            ):
                logger.info(
                    "Reconciling kubernetes annotations",
                    hostname=record.hostname,
                    ips=desired_ips,
                    changed=changed,
                )
                try:
                    update_apex_dns_annotations(
                        ips=desired_ips,
                        label_selector=config.kubernetes.label_selector,
                        dry_run=dry_run,
                    )
                except Exception as k8s_error:
                    logger.error(
                        "Failed to update kubernetes annotations",
                        hostname=record.hostname,
                        error=str(k8s_error),
                    )
        except Exception as e:
            logger.error(
                "Failed to sync DNS records",
                hostname=record.hostname,
                error=str(e),
            )

    if not any_changes:
        logger.info("No DNS changes needed, all records up to date")


def main() -> None:
    """Main entry point."""
    args = parse_args()
    configure_logging(args.log_level)
    logger = structlog.get_logger()

    logger.info("wan-dns-sync starting", version="0.4.0")

    # Load configuration from file or environment
    try:
        config, config_source = load_config_auto(args.config)
        logger.info("Configuration loaded", source=config_source)
    except Exception as e:
        logger.error("Failed to load configuration", error=str(e))
        sys.exit(1)

    # Override dry_run from CLI if specified
    dry_run = args.dry_run or config.settings.dry_run

    logger.info(
        "Starting wan-dns-sync",
        zone=config.hetzner.zone,
        interval=config.settings.interval,
        dry_run=dry_run,
        record_count=len(config.records),
        health_port=config.settings.health_port,
    )

    shutdown = GracefulShutdown()
    health_server: HTTPServer | None = None

    with (
        OPNsenseClient(config.opnsense) as opnsense,
        HetznerDNSClient(config.hetzner) as hetzner,
    ):
        # Start health server if configured
        if config.settings.health_port:

            def ready_check() -> bool:
                return opnsense.health_check(timeout=2.0) and hetzner.health_check()

            health_server = start_health_server(
                config.settings.health_port, ready_check
            )

        iteration = 0
        while not shutdown.should_exit:
            iteration += 1
            logger.info("Starting update cycle", iteration=iteration)

            run_update(config, opnsense, hetzner, dry_run)

            if args.once:
                logger.info("Single run complete, exiting")
                break

            logger.info(
                "Update cycle complete, sleeping",
                iteration=iteration,
                next_check_seconds=config.settings.interval,
            )

            # Sleep with interrupt checking
            for _ in range(config.settings.interval):
                if shutdown.should_exit:
                    break
                time.sleep(1)

        if health_server:
            health_server.shutdown()

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
