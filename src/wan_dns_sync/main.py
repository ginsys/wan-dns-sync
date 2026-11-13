"""Main entry point for wan-dns-sync."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections.abc import Callable
from http.server import HTTPServer
from pathlib import Path

import structlog
from octodns.provider.base import BaseProvider

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Config, load_config
from .dns import WriteUncertainError, load_provider, sync_zone
from .health import HealthState, start_health_server
from .kubernetes_updater import update_apex_dns_annotations
from .opnsense import OPNsenseClient
from .verify import verify_a_records

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

Desired = dict[str, dict[str, list[str]]]  # zone -> record name -> addresses


def _addresses(interfaces: list[str], interface_ips: dict[str, str]) -> list[str]:
    """Sorted distinct addresses of the named interfaces that currently have one."""
    return sorted({interface_ips[i] for i in interfaces if i in interface_ips})


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
        description="Keep DNS A records in step with the WAN addresses of an OPNsense router"
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
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
        help="Run once and exit (non-zero if the cycle failed)",
    )
    return parser.parse_args()


class GracefulShutdown:
    """Handle graceful shutdown on SIGTERM/SIGINT."""

    def __init__(self) -> None:
        self.should_exit = False
        signal.signal(signal.SIGTERM, self._handler)
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, signum: int, frame: object) -> None:
        structlog.get_logger().info("Received shutdown signal", signal=signum)
        self.should_exit = True


class Updater:
    """One update cycle: read the WAN addresses, write DNS, maintain annotations."""

    def __init__(
        self,
        config: Config,
        opnsense: OPNsenseClient,
        dry_run: bool,
        provider_loader: Callable[[str, dict[str, object]], BaseProvider] = load_provider,
        verify: Callable[[str, str, set[str]], bool] = verify_a_records,
        annotate: Callable[..., bool] = update_apex_dns_annotations,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.opnsense = opnsense
        self.dry_run = dry_run
        self._load_provider = provider_loader
        self._verify = verify
        self._annotate = annotate
        self._sleep = sleep
        self._clock = clock
        self._last_applied: Desired | None = None
        self._last_reconcile = 0.0
        # zones written (or possibly written) and not yet seen at every nameserver
        self._unverified: set[str] = set()

    def _desired(self, interface_ips: dict[str, str]) -> Desired:
        """Addresses per owned record. A record with none is left out, so it is never deleted."""
        logger = structlog.get_logger()
        desired: Desired = {}
        for zone, zone_config in self.config.zones.items():
            records: dict[str, list[str]] = {}
            for record in zone_config.records:
                missing = [i for i in record.interfaces if i not in interface_ips]
                if missing:
                    logger.warning(
                        "Missing interfaces for record",
                        record=record.name,
                        zone=zone,
                        missing=missing,
                    )
                addresses = _addresses(record.interfaces, interface_ips)
                if addresses:
                    records[record.name] = addresses
                else:
                    logger.warning("No IPs available for record, skipping", record=record.name)
            desired[zone] = records
        return desired

    def _sync_dns(self, desired: Desired) -> bool:
        """Write every zone to every provider. True when all of it succeeded."""
        logger = structlog.get_logger()
        ok = True
        any_changes = False
        for zone, owned in desired.items():
            zone_config = self.config.zones[zone]
            written = False  # a write was made, or may have been (an apply that raised)
            for provider_name in zone_config.providers if owned else []:
                try:
                    provider = self._load_provider(
                        provider_name, self.config.providers[provider_name]
                    )
                    if sync_zone(provider, zone, owned, zone_config.ttl, self.dry_run):
                        any_changes = written = True
                except Exception as e:
                    ok = False
                    # Only a failed apply may have written; a failure before it wrote nothing.
                    written = written or isinstance(e, WriteUncertainError)
                    logger.error(
                        "Failed to sync DNS records",
                        zone=zone,
                        provider=provider_name,
                        error_type=type(e).__name__,
                        error=str(e),
                    )
            if self.dry_run:
                continue
            if written:
                # The retry of a write finds no change to apply, and must not read that
                # as a verified zone: keep the zone marked until a check passes.
                self._unverified.add(zone)
                self._sleep(self.config.settings.verify_delay)
            if zone in self._unverified:
                # Always check what is owned now, never what an earlier cycle wrote: a
                # record that has lost its addresses is neither published nor checked.
                results = [self._verify(n, zone, set(a)) for n, a in owned.items()]
                if all(results):
                    self._unverified.discard(zone)
                else:
                    ok = False
        if not any_changes:
            logger.info("No DNS changes applied")
        return ok

    def run_cycle(self) -> bool:
        """Run one cycle. True when every part of it succeeded."""
        logger = structlog.get_logger()

        try:
            interface_ips = self.opnsense.get_interface_ips()
        except Exception as e:
            logger.error("Failed to get IPs from OPNsense", error=str(e))
            return False
        if not interface_ips:
            logger.warning("No interface IPs found")
            return False
        logger.debug("Retrieved interface IPs", ips=interface_ips)

        ok = True
        desired = self._desired(interface_ips)
        now = self._clock()
        reconcile_due = now - self._last_reconcile >= self.config.settings.reconcile_interval
        if desired != self._last_applied or reconcile_due:
            if self._sync_dns(desired):
                self._last_applied = desired
                self._last_reconcile = now
            else:
                self._last_applied = None  # retry in full next cycle
                ok = False
        else:
            logger.debug("Addresses unchanged since the last sync, skipping DNS")

        kubernetes = self.config.kubernetes
        if kubernetes.enabled and kubernetes.label_selector:
            addresses = _addresses(kubernetes.interfaces, interface_ips)
            try:
                self._annotate(
                    ips=addresses,
                    label_selector=kubernetes.label_selector,
                    dry_run=self.dry_run,
                )
            except Exception as e:
                ok = False
                logger.error("Failed to update kubernetes annotations", error=str(e))
        return ok


def main() -> None:
    """Main entry point."""
    args = parse_args()
    configure_logging(args.log_level)
    logger = structlog.get_logger()

    logger.info("wan-dns-sync starting", version=__version__)

    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error("Failed to load configuration", path=str(args.config), error=str(e))
        sys.exit(1)

    dry_run = args.dry_run or config.settings.dry_run
    logger.info(
        "Configuration loaded",
        zones=sorted(config.zones),
        providers=sorted(config.providers),
        interval=config.settings.interval,
        reconcile_interval=config.settings.reconcile_interval,
        dry_run=dry_run,
    )

    shutdown = GracefulShutdown()
    health = HealthState(config.settings.interval, config.settings.verify_delay)
    health_server: HTTPServer | None = None
    exit_code = 0

    with OPNsenseClient(config.opnsense, config.settings.allow_private_ips) as opnsense:
        if config.settings.health_port:
            health_server = start_health_server(config.settings.health_port, health)

        updater = Updater(config, opnsense, dry_run)
        iteration = 0
        while not shutdown.should_exit:
            iteration += 1
            logger.info("Starting update cycle", iteration=iteration)
            ok = updater.run_cycle()
            health.cycle_done(ok)

            if args.once:
                exit_code = 0 if ok else 1
                break

            for _ in range(config.settings.interval):
                if shutdown.should_exit:
                    break
                health.beat()
                time.sleep(1)

        if health_server:
            health_server.shutdown()

    logger.info("Shutdown complete")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
