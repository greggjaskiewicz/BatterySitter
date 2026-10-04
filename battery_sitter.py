#!/usr/bin/env python3
"""
BatterySitter - Prevents Sigenstore battery from discharging during Zappi car charging

This script monitors a Zappi EV charger and when it detects charging is active,
changes the Sigenstore operational mode to prevent battery discharge, ensuring grid
power is used for EV charging instead of depleting the home battery.
"""

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone
import signal

from aiohttp import ClientResponseError, ClientSession
from pymyenergi.connection import Connection
from pymyenergi.zappi import Zappi

# Prefer the vendored sigen over any pip-installed copy: upstream is
# unmaintained, so fixes (e.g. the CloudFront WAF user-agent workaround)
# live in vendor/ and must win over site-packages.
sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
from sigen import Sigen, BROWSER_HEADERS


class BatterySitter:
    """Coordinates Zappi monitoring and Sigenstore battery control"""

    def __init__(
        self,
        # Zappi / MyEnergi credentials
        zappi_username: str,
        zappi_password: str,
        zappi_serial: str,
        # Sigenstore / Sigenergy credentials
        sigenergy_username: str,
        sigenergy_password: str,
        sigenergy_region: str = 'eu',
        # in kW, charging power when EV is charging
        charging_power: int = 1,
        charging_duration_minutes: int = 30,
        # Polling settings
        poll_interval: int = 30,
        # Solar-diversion guard: battery discharge (kW) considered "funding
        # the car", and how many consecutive polls it must persist before we
        # pin the battery and let the Zappi see the real grid deficit.
        diversion_max_discharge_kw: float = 1.0,
        diversion_sustained_polls: int = 4,
        # Timed-charge schedule: JSON file of UTC windows during which the
        # battery is force-charged (or blocked from charging), regardless
        # of what the Zappi is doing. Reloaded whenever the file changes.
        schedule_file: str = 'schedule.json',
    ):
        """
        Initialize BatterySitter

        Args:
            zappi_username: MyEnergi Hub serial number (found on device label)
            zappi_password: MyEnergi API key
            zappi_serial: Zappi charger serial number
            sigenergy_username: MySigen app username/email
            sigenergy_password: MySigen app password
            sigenergy_region: Region code (eu, us, cn, apac)
            charging_duration_minutes: Duration for manual charge when EV charging (minutes)
            poll_interval: Seconds between status checks
            diversion_max_discharge_kw: During solar diversion, battery
                discharge above this (kW) counts as draining into the car
            diversion_sustained_polls: Consecutive polls of such discharge
                before the guard trips (must outlast brief cloud gaps)
            schedule_file: Path to the timed-charge schedule JSON file
        """
        self.zappi_username = zappi_username
        self.zappi_password = zappi_password
        self.zappi_serial = zappi_serial

        self.sigenergy_username = sigenergy_username
        self.sigenergy_password = sigenergy_password
        self.sigenergy_region = sigenergy_region
        self.charging_power = charging_power
        self.charging_duration_minutes = charging_duration_minutes

        self.poll_interval = poll_interval

        self.logger = logging.getLogger(__name__)
        self.zappi: Optional[Zappi] = None
        self.sigen: Optional[Sigen] = None
        self.http_session: Optional[ClientSession] = None
        self.is_charging = False
        self.running = False
        self.manual_charge_enabled = False  # Track if WE enabled manual charge
        self.manual_charge_enabled_at: Optional[datetime] = None
        self.ev_stop_first_seen: Optional[datetime] = None
        self.zappi_solar_diverting = False  # Zappi charging from excess solar
        # Session-level diversion classification (Zappi flag + solar sanity)
        self.in_diversion = False
        self._diversion_reject_logged = False
        self.diversion_max_discharge_kw = diversion_max_discharge_kw
        self.diversion_sustained_polls = diversion_sustained_polls
        # Guard against the battery silently funding the car in Eco+: battery
        # discharge keeps the grid CT at zero, so the Zappi never sees the
        # deficit and never pauses. Once tripped, the session is treated as
        # needing normal battery protection until EV charging stops.
        self.diversion_guard_active = False
        self.diversion_discharge_polls = 0
        # Timed-charge schedule state
        self.schedule_file = Path(schedule_file)
        self._schedule_mtime: Optional[float] = None
        self._schedule_entries: list = []
        self.schedule_charge_active = False
        self.schedule_block_active = False
        self.manual_charge_last_duration: Optional[int] = None
        # Post-disable watchdog: Sigenergy has acknowledged a disable
        # (HTTP 200) that the inverter never applied, leaving the battery
        # charging from the grid unwatched (2026-10-04 incident). After a
        # disable, verify for a few minutes that charging actually stopped.
        self.disable_verify_until: Optional[datetime] = None
        self.disable_verify_from: Optional[datetime] = None
        self.disable_retries = 0
        # Stall detection for timed windows: consecutive polls where the
        # battery should be charging but isn't (popped breaker, ignored
        # enable). Without this a dead battery wastes the whole window
        # silently (2026-10-04 fuse incident).
        self.window_stall_polls = 0
        self.last_connection_time: Optional[datetime] = None
        self.reconnect_interval = timedelta(hours=8)  # Reconnect every 8 hours

    async def connect(self):
        """Establish connections to Zappi and Sigenstore"""
        self.logger.info("Connecting to MyEnergi API...")
        connection = Connection(self.zappi_username, self.zappi_password)
        await connection.discoverLocations()
        self.zappi = Zappi(connection, self.zappi_serial)

        await self.zappi.refresh()

        self.logger.info(f"Connecting to Sigenergy Cloud ({self.sigenergy_region})...")
        self.sigen = Sigen(
            username=self.sigenergy_username,
            password=self.sigenergy_password,
            region=self.sigenergy_region
        )
        await self.sigen.async_initialize()

        # Log available operational modes
        modes = self._default_working_modes(
            await self.sigen.get_operational_modes())
        self.logger.info("Available Sigenergy operational modes:")
        for mode in modes:
            self.logger.info(f"  - {mode['label']} (value: {mode['value']})")

        current_mode = await self.sigen.get_operational_mode()
        self.logger.info(f"Current operational mode: {current_mode}")

        self.last_connection_time = datetime.now()
        self.logger.info("Successfully connected to both devices")

    async def disconnect(self):
        """Close connections"""
        try:
            if self.http_session and not self.http_session.closed:
                await self.http_session.close()
        except Exception as exc:  # noqa: broad-except
            self.logger.debug(f"Error closing HTTP session: {exc}")

        self.http_session = None
        self.zappi = None
        self.sigen = None
        self.logger.info("Disconnected from devices")

    async def _reconnect(self, reason: str):
        """Reconnect to external services after failures or periodic refreshes."""
        self.logger.info(f"Reconnecting ({reason})...")
        await self.disconnect()
        try:
            await self.connect()
            self.logger.info("Reconnected successfully")
        except Exception as exc:  # noqa: broad-except
            self.logger.error(f"Reconnect failed: {exc}", exc_info=True)
            raise

    def _manual_charge_expiring(self) -> bool:
        """
        True when the manual charge window is about to lapse. Refreshing it
        just before expiry avoids the gap where the battery reverts to
        self-powered mode and dumps several kW into the car until the next
        poll notices.
        """
        if not self.manual_charge_enabled_at:
            return False
        elapsed = datetime.now() - self.manual_charge_enabled_at
        margin = timedelta(seconds=2 * self.poll_interval)
        duration = (self.manual_charge_last_duration
                    or self.charging_duration_minutes)
        return elapsed >= timedelta(minutes=duration) - margin

    async def get_zappi_charging_status(self) -> Optional[bool]:
        """
        Check if Zappi is currently charging

        Returns:
            True if charging is active, False if not, None if the status
            could not be read (caller should hold its previous state rather
            than treat this as a session end)
        """
        try:
            await self.zappi.refresh()

            # Status codes from pymyenergi:
            # Paused, Charging or Completed
            charging_statuses = ['Charging', 'Boosting']

            is_charging = (
                self.zappi.status in charging_statuses and
                self.zappi.plug_status == 'Charging'
            )

            # Solar diversion: Eco/Eco+ actively 'Charging' (not 'Boosting').
            # Octopus-driven sessions come through as boosts, so this only
            # matches genuine surplus-solar charging - where the battery must
            # stay free to discharge and smooth short cloud gaps instead of
            # being locked into a forced charge.
            self.zappi_solar_diverting = (
                is_charging and
                self.zappi.status == 'Charging' and
                self.zappi.charge_mode in ('Eco', 'Eco+')
            )

            self.logger.debug(
                f"Zappi status: {self.zappi.status}, "
                f"plug_status: {self.zappi.plug_status}, "
                f"charge_mode: {self.zappi.charge_mode}"
            )

            return is_charging

        except Exception as e:
            # Unknown, not "stopped": leave zappi_solar_diverting as it was
            # and let the caller hold its previous state - faking a session
            # end here would release the battery lock mid-charge.
            self.logger.error(
                f"Error reading Zappi status: {type(e).__name__}: {e}"
            )
            return None

    async def get_battery_info(self) -> dict:
        """
        Get current battery and energy flow information

        Returns:
            Dictionary with energy flow data
        """
        # Sigenergy's cloud blips roughly every 5 minutes (502/503 for up to
        # ~1 min), so the retry schedule must outlast a whole blip: attempts
        # at t=0, 5, 20 and 50 seconds. Individual failed attempts are DEBUG;
        # only exhausting all retries is worth an ERROR line.
        attempts = 4
        backoff = (5, 15, 30)
        for attempt in range(1, attempts + 1):
            try:
                energy_flow = await self.sigen.get_energy_flow()
                # Degraded Sigenergy responses come back as HTTP 200 with
                # 'data' set to None or an error *string* - anything but a
                # dict means no usable data.
                if not isinstance(energy_flow, dict):
                    self.logger.warning(
                        "get_energy_flow() returned %s instead of dict: %.120r",
                        type(energy_flow).__name__, energy_flow,
                    )
                    return {}
                return energy_flow
            except ClientResponseError as e:
                # Transient cloud errors (502/503/504, non-JSON error pages).
                # e.status / e.message / e.headers carry the useful detail.
                if e.status in (502, 503, 504) and attempt < attempts:
                    self.logger.debug(
                        "Transient battery info error (attempt %d/%d): "
                        "status=%s", attempt, attempts, e.status,
                    )
                    await asyncio.sleep(backoff[attempt - 1])
                    continue
                self.logger.error(
                    "Battery info unavailable (attempt %d/%d): "
                    "status=%s message=%r url=%s content-type=%s",
                    attempt, attempts, e.status, e.message, e.request_info.url,
                    e.headers.get("Content-Type") if e.headers else None,
                )
                return {}
            except Exception as e:
                if attempt < attempts:
                    self.logger.debug(
                        "Transient battery info error (attempt %d/%d): %s: %s",
                        attempt, attempts, type(e).__name__, e,
                    )
                    await asyncio.sleep(backoff[attempt - 1])
                    continue
                self.logger.error(
                    "Battery info unavailable (attempt %d/%d): %s: %s",
                    attempt, attempts, type(e).__name__, e,
                )
                return {}
        return {}

    @staticmethod
    def _default_working_modes(modes):
        """
        Normalise get_operational_modes() across sigen versions.

        Newer sigen returns a dict {'defaultWorkingModes': [...],
        'energyProfileItems': [...]}; older versions returned a flat list of
        {'label', 'value'} dicts. Always return the list of working modes.
        """
        if isinstance(modes, dict):
            return modes.get('defaultWorkingModes', [])
        return modes or []

    async def set_operational_mode_by_name(self, mode_name: str):
        """
        Set operational mode by its label name

        Args:
            mode_name: Name of the mode (e.g., "Maximum Self-Powered")
        """
        try:
            modes = self._default_working_modes(
                await self.sigen.get_operational_modes())

            # Find the mode by name (case-insensitive partial match)
            target_mode = None
            for mode in modes:
                if mode_name.lower() in mode['label'].lower():
                    target_mode = mode
                    break

            if not target_mode:
                self.logger.error(f"Mode '{mode_name}' not found. Available modes:")
                for mode in modes:
                    self.logger.error(f"  - {mode['label']}")
                raise ValueError(f"Unknown operational mode: {mode_name}")

            mode_value = int(target_mode['value'])
            result = await self.sigen.set_operational_mode(mode_value)
            self.logger.info(f"Set operational mode to '{target_mode['label']}'")

            return result

        except Exception as e:
            self.logger.error(f"Error setting operational mode: {e}")
            raise

    async def set_instant_manual_charge(
        self, enable: bool, duration_minutes: int = 30,
        power_kw: float = 1.0, mode: str = "0"
    ):
        """
        Set instant manual charge control for the battery.
        This allows forcing the battery to charge from the grid for a
        specific duration at a specific rate.

        Args:
            enable: True to enable manual charging, False to disable
            duration_minutes: Duration in minutes (e.g., 30, 60, 120)
            power_kw: Power limitation in kW (e.g., 1.0, 2.5, 5.0)
            mode: Charge mode as string, default "0" (charge mode)

        Returns:
            API response

        Example:
            # Charge at 3kW for 60 minutes
            await sitter.set_instant_manual_charge(True, 60, 3.0)

            # Disable manual charging
            await sitter.set_instant_manual_charge(False, 0, 0)
        """
        try:
            await self.sigen.ensure_valid_token()
            # Note: API has typo "manunal"
            url = f"{self.sigen.BASE_URL}device/energy-profile/instant/manunal"
            payload = {
                'enable': enable,
                'stationId': self.sigen.station_id,
                'mode': mode,
                'duration': str(duration_minutes),
                'powerLimitation': str(power_kw)
            }

            if not self.http_session or self.http_session.closed:
                self.http_session = ClientSession(headers=BROWSER_HEADERS)

            async with self.http_session.put(
                url, headers=self.sigen.headers, json=payload
            ) as response:
                response.raise_for_status()
                result = await response.json()

                # Sigenergy signals failure inside an HTTP 200: the JSON
                # body carries a non-zero 'code'. Trusting the status line
                # alone once logged a successful disable that the inverter
                # never applied (it kept charging until manually stopped).
                code = result.get('code') if isinstance(result, dict) else None
                if code not in (0, None):
                    raise RuntimeError(
                        f"Sigenergy rejected instant manual charge command "
                        f"(enable={enable}): {str(result)[:200]}"
                    )

                if enable:
                    self.manual_charge_enabled_at = datetime.now()
                    self.manual_charge_last_duration = duration_minutes
                    self.disable_verify_until = None
                    self.disable_retries = 0
                    self.logger.info(
                        f"Enabled instant manual charge: "
                        f"{power_kw}kW for {duration_minutes} minutes"
                    )
                else:
                    self.logger.info("Disabled instant manual charge")
                    # Watch the next few polls to confirm the inverter
                    # actually stopped charging. Start checking one poll
                    # later: this poll's power reading predates the
                    # disable, and the inverter needs ~30s to ramp down,
                    # so checking immediately cries wolf (2026-10-04).
                    self.disable_verify_until = (
                        datetime.now() + timedelta(minutes=5)
                    )
                    self.disable_verify_from = (
                        datetime.now()
                        + timedelta(seconds=self.poll_interval + 5)
                    )

                return result

        except Exception as e:
            self.logger.error(f"Error setting instant manual charge: {e}")
            raise

    def _load_schedule(self) -> list:
        """
        Load the timed-charge schedule, re-reading the file whenever its
        mtime changes so entries can be added or removed while the service
        runs (just scp a new schedule.json over - no restart needed).

        The file is a JSON list; each entry:
            {"start": "2026-09-06T13:00:00Z",   # UTC (naive = UTC)
             "duration_minutes": 60,
             "action": "charge",                 # or "block" (default charge)
             "power_kw": 10.0,                   # optional, default config
             "note": "free electricity hour"}    # optional, logged
        Invalid entries are skipped individually so one typo can't take out
        the whole schedule.
        """
        try:
            mtime = self.schedule_file.stat().st_mtime
        except OSError:
            # No schedule file - the feature is simply inactive
            if self._schedule_mtime is not None:
                self.logger.info(
                    f"Schedule file {self.schedule_file} removed"
                )
            self._schedule_mtime = None
            self._schedule_entries = []
            return self._schedule_entries

        if mtime == self._schedule_mtime:
            return self._schedule_entries

        self._schedule_mtime = mtime
        self._schedule_entries = []
        try:
            raw = json.loads(self.schedule_file.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as e:
            self.logger.error(
                f"Schedule file {self.schedule_file} unreadable: {e}"
            )
            return self._schedule_entries
        if not isinstance(raw, list):
            self.logger.error(
                f"Schedule file {self.schedule_file} must contain a JSON "
                f"list, got {type(raw).__name__}"
            )
            return self._schedule_entries

        entries = []
        for idx, item in enumerate(raw):
            try:
                start = datetime.fromisoformat(
                    str(item['start']).replace('Z', '+00:00'))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                duration = float(item['duration_minutes'])
                if duration <= 0:
                    raise ValueError("duration_minutes must be positive")
                action = str(item.get('action', 'charge')).lower()
                if action not in ('charge', 'block'):
                    raise ValueError(f"unknown action {action!r}")
                power_kw = item.get('power_kw')
                if power_kw is not None:
                    power_kw = float(power_kw)
                    if power_kw <= 0:
                        raise ValueError("power_kw must be positive")
                entries.append({
                    'start': start.astimezone(timezone.utc),
                    'end': start + timedelta(minutes=duration),
                    'action': action,
                    'power_kw': power_kw,
                    'note': item.get('note'),
                })
            except (KeyError, TypeError, ValueError) as e:
                self.logger.error(
                    f"Schedule entry {idx} in {self.schedule_file} "
                    f"invalid, skipping: {e}"
                )

        self._schedule_entries = entries
        now = datetime.now(timezone.utc)
        upcoming = [e for e in entries if e['end'] > now]
        self.logger.info(
            f"Schedule loaded: {len(entries)} entries, "
            f"{len(upcoming)} current/upcoming"
        )
        for e in upcoming:
            power = e['power_kw'] if e['power_kw'] is not None \
                else self.charging_power
            detail = f" at {power}kW" if e['action'] == 'charge' else ""
            note = f" - {e['note']}" if e['note'] else ""
            self.logger.info(
                f"  {e['action']} {e['start']:%Y-%m-%d %H:%M} to "
                f"{e['end']:%H:%M} UTC{detail}{note}"
            )
        return entries

    def _active_schedule_entry(self) -> Optional[dict]:
        """The schedule entry whose window covers now, if any."""
        now = datetime.now(timezone.utc)
        for entry in self._load_schedule():
            if entry['start'] <= now < entry['end']:
                return entry
        return None

    async def _handle_schedule_window(
        self, zappi_charging: bool, battery_soc, battery_power
    ) -> bool:
        """
        Apply the timed-charge schedule. Returns True when a window is
        active and owns this poll (the normal EV state machine is skipped);
        False otherwise. A window that has just ended is cleaned up here
        before handing control back to the state machine.
        """
        entry = self._active_schedule_entry()

        if entry is None:
            if self.schedule_charge_active:
                self.schedule_charge_active = False
                if zappi_charging and not self.in_diversion:
                    # EV still drawing power: keep the pin and let the EV
                    # protection maintain it, instead of opening a gap where
                    # the battery dumps into the car.
                    self.logger.info(
                        "⏰ Timed charge window ended - EV still charging, "
                        "handing battery pin over to EV protection"
                    )
                    self.is_charging = True
                else:
                    self.logger.info(
                        f"⏰ Timed charge window ended (SOC: {battery_soc}%)"
                        " - disabling instant manual charge"
                    )
                    if self.manual_charge_enabled:
                        await self.set_instant_manual_charge(
                            enable=False, duration_minutes=0, power_kw=0
                        )
                        self.manual_charge_enabled = False
            if self.schedule_block_active:
                self.schedule_block_active = False
                self.logger.info(
                    "⏰ Timed block window ended - normal operation resumes"
                )
            return False

        now = datetime.now(timezone.utc)
        note = f" ({entry['note']})" if entry['note'] else ""

        if entry['action'] == 'charge':
            self.schedule_block_active = False
            # Cap the requested duration at the window's end, so even if
            # this process dies mid-window Sigenergy stops on time.
            remaining_min = (entry['end'] - now).total_seconds() / 60
            duration = max(1, min(self.charging_duration_minutes,
                                  int(remaining_min) + 1))
            power = entry['power_kw'] if entry['power_kw'] is not None \
                else self.charging_power
            if not self.schedule_charge_active:
                self.logger.info(
                    f"⏰ Timed charge window started{note} - charging at "
                    f"{power}kW until {entry['end']:%H:%M} UTC "
                    f"(SOC: {battery_soc}%, Power: {battery_power}kW)"
                )
                await self.set_instant_manual_charge(
                    enable=True, duration_minutes=duration, power_kw=power
                )
                self.schedule_charge_active = True
                self.manual_charge_enabled = True
                self.window_stall_polls = 0
            else:
                if self._manual_charge_expiring():
                    self.logger.info(
                        f"Timed charge window - refreshing "
                        f"({power}kW for {duration}min)"
                    )
                    await self.set_instant_manual_charge(
                        enable=True, duration_minutes=duration,
                        power_kw=power
                    )

                # Stall detection: mid-window, a healthy battery below
                # full should be visibly charging. A popped breaker or a
                # silently ignored enable otherwise wastes the whole
                # window with nothing in the log.
                power_known = isinstance(battery_power, (int, float))
                soc_known = isinstance(battery_soc, (int, float))
                if (power_known and battery_power < 0.5
                        and (not soc_known or battery_soc < 99.0)):
                    self.window_stall_polls += 1
                    if self.window_stall_polls == 3:
                        # Pin on but no power = likely breaker; pin off =
                        # dropped enable, worth re-sending.
                        state = await self.get_instant_manual_charge()
                        if state is not None and state.get('enable'):
                            self.logger.error(
                                f"Timed charge pin is active but battery "
                                f"takes no power (Power: {battery_power}kW, "
                                f"SOC: {battery_soc}%) - battery may be "
                                f"offline (breaker tripped?)"
                            )
                            self.window_stall_polls = 6  # skip re-enable
                        else:
                            self.logger.warning(
                                f"Timed charge window active but battery "
                                f"not charging (Power: {battery_power}kW, "
                                f"SOC: {battery_soc}%) - re-sending enable"
                            )
                            await self.set_instant_manual_charge(
                                enable=True, duration_minutes=duration,
                                power_kw=power
                            )
                    elif self.window_stall_polls == 6:
                        self.logger.error(
                            f"Timed charge still not charging after "
                            f"re-enable (Power: {battery_power}kW, SOC: "
                            f"{battery_soc}%) - battery may be offline "
                            f"(breaker tripped?)"
                        )
                elif power_known:
                    self.window_stall_polls = 0
        else:  # 'block'
            if self.schedule_charge_active:
                self.schedule_charge_active = False
            if not self.schedule_block_active:
                self.logger.info(
                    f"⏰ Timed block window started{note} - suppressing "
                    f"battery charging until {entry['end']:%H:%M} UTC"
                )
                self.schedule_block_active = True
            if self.manual_charge_enabled:
                self.logger.info(
                    "Timed block window - disabling instant manual charge"
                )
                await self.set_instant_manual_charge(
                    enable=False, duration_minutes=0, power_kw=0
                )
                self.manual_charge_enabled = False

        # Keep the Zappi tracking in sync so the state machine sees no fake
        # start/stop transition when the window ends.
        self.is_charging = zappi_charging
        self.ev_stop_first_seen = None
        return True

    async def get_instant_manual_charge(self) -> Optional[dict]:
        """
        Read the authoritative instant-manual-charge state (enable flag,
        mode, end time) from Sigenergy. Lets the watchdogs check whether
        the pin is actually on/off instead of inferring it from power
        flows. Returns None when the read fails - callers must fall back
        to their heuristic in that case.
        """
        try:
            await self.sigen.ensure_valid_token()
            url = (f"{self.sigen.BASE_URL}device/energy-profile/"
                   f"instant/manunal/{self.sigen.station_id}")
            if not self.http_session or self.http_session.closed:
                self.http_session = ClientSession(headers=BROWSER_HEADERS)
            async with self.http_session.get(
                url, headers=self.sigen.headers
            ) as response:
                response.raise_for_status()
                result = await response.json()
                if isinstance(result, dict) and result.get('code') == 0:
                    return result.get('data')
                self.logger.debug(
                    f"Instant charge state read returned: {result}"
                )
                return None
        except Exception as e:
            self.logger.debug(
                f"Could not read instant charge state: "
                f"{type(e).__name__}: {e}"
            )
            return None

    async def monitor_loop(self):
        """Main monitoring loop"""
        self.running = True
        self.logger.info("Starting monitoring loop...")
        self.logger.info(f"Will check status every {self.poll_interval} seconds")
        self.logger.info(
             f"When EV charging detected: "
             f"Enable instant manual battery charge at "
             f"{self.charging_power}kW for {self.charging_duration_minutes}min"
        )
        self.logger.info(
             f"During solar diversion: battery left free; guard pins it after "
             f"{self.diversion_sustained_polls} polls of discharge >= "
             f"{self.diversion_max_discharge_kw}kW"
        )
        self.logger.info(
            f"Timed-charge schedule file: {self.schedule_file} "
            f"(UTC times, editable while running)"
        )
        self._load_schedule()

        consecutive_errors = 0
        while self.running:
            try:
                # Periodic reconnection to refresh tokens/sessions
                if self.last_connection_time:
                    time_since_connection = datetime.now() - self.last_connection_time
                    if time_since_connection >= self.reconnect_interval:
                        hours = time_since_connection.total_seconds() / 3600
                        await self._reconnect(f"scheduled refresh after {hours:.1f} hours")
                        consecutive_errors = 0

                # Check Zappi charging status. On a failed read (None), hold
                # the previous state - a myenergi blip must not look like a
                # session end.
                zappi_status = await self.get_zappi_charging_status()
                zappi_charging = (
                    self.is_charging if zappi_status is None else zappi_status
                )

                # Get battery/energy info for logging
                battery_info = await self.get_battery_info()
                if not battery_info:
                    self.logger.warning(
                        "Battery info unavailable - "
                        "get_battery_info() returned empty dict"
                    )
                battery_soc = battery_info.get('batterySoc', 'N/A')
                # Positive = charging, Negative = discharging
                battery_power = battery_info.get('batteryPower', 'N/A')

                # Whether we have a usable, numeric power reading. Stays False on
                # a cloud failure (502 -> empty dict), a dropped connection, or a
                # future change to Sigenergy's response fields - so we never make
                # a charge-override decision on a guess.
                battery_power_known = isinstance(battery_power, (int, float))

                if battery_info and not battery_power_known:
                    # Request succeeded but 'batteryPower' is missing/non-numeric:
                    # most likely Sigenergy changed their response schema. Make
                    # this loud so it gets noticed rather than silently degrading.
                    self.logger.warning(
                        f"Battery power missing/non-numeric (got {battery_power!r}) "
                        "- Sigenergy response fields may have changed; "
                        "deferring charge decisions"
                    )
                elif not isinstance(battery_soc, (int, float)) or \
                        not battery_power_known:
                    self.logger.debug(
                        f"Non-numeric battery data - "
                        f"SOC: {battery_soc} (type: {type(battery_soc).__name__}), "
                        f"Power: {battery_power} (type: {type(battery_power).__name__})"
                    )

                # Tri-state on the battery's charge direction. Another source
                # (Sigen AI, timer/TOU) can charge it independently of us, so we
                # distinguish known-charging / known-not-charging / unknown and
                # only ever act on the two *known* states.
                battery_already_charging = battery_power_known and battery_power > 0
                battery_known_not_charging = battery_power_known and battery_power <= 0

                # Any confirmed charging poll cancels a pending stop-debounce
                if zappi_charging:
                    self.ev_stop_first_seen = None

                # Session-level diversion classification. The Zappi also
                # reports plain 'Charging' in Eco+ while winding a session
                # down at night, so *entering* diversion mode additionally
                # requires actual solar production. Once in, brief cloud dips
                # don't kick us out (that smoothing is the whole point) - the
                # discharge guard is the backstop if the sun is gone for good.
                solar_parts = [
                    v for v in (battery_info.get('pvPower'),
                                battery_info.get('thirdPvPower'))
                    if isinstance(v, (int, float))
                ] if battery_info else []
                solar_kw = sum(solar_parts) if solar_parts else None
                if self.zappi_solar_diverting:
                    if not self.in_diversion:
                        if solar_kw is None or solar_kw >= 0.5:
                            self.in_diversion = True
                            self._diversion_reject_logged = False
                        elif not self._diversion_reject_logged:
                            self.logger.info(
                                f"Zappi reports Eco+ charging but solar is "
                                f"only {solar_kw}kW - treating as grid "
                                f"charge, keeping battery protection"
                            )
                            self._diversion_reject_logged = True
                else:
                    self.in_diversion = False
                    self._diversion_reject_logged = False

                # Timed-charge schedule outranks the EV state machine: a
                # pre-programmed window (e.g. a free-electricity hour while
                # away) charges or blocks regardless of what the Zappi does.
                if await self._handle_schedule_window(
                        zappi_charging, battery_soc, battery_power):
                    consecutive_errors = 0
                    await asyncio.sleep(self.poll_interval)
                    continue

                # Handle state changes
                if zappi_charging and not self.is_charging:
                    # EV charging just started
                    self.logger.info(
                        f"⚡ EV charging detected! "
                        f"Battery SOC: {battery_soc}%, Power: {battery_power}kW, "
                        f"Zappi: {self.zappi.status}/{self.zappi.charge_mode}"
                    )

                    self.diversion_guard_active = False
                    self.diversion_discharge_polls = 0

                    if self.in_diversion:
                        # Excess-solar diversion: leave the battery completely
                        # alone so it can discharge through brief cloud gaps
                        # instead of turning them into grid imports. The guard
                        # below steps in if that discharge turns sustained.
                        self.logger.info(
                            "Zappi is diverting excess solar (Eco/Eco+) - "
                            "leaving battery untouched so it can smooth "
                            "cloud gaps"
                        )
                        self.is_charging = True
                        self.manual_charge_enabled = False
                    elif battery_already_charging:
                        self.logger.info(
                            "Battery is already charging - "
                            "not overriding existing charge control"
                        )
                        self.is_charging = True
                        self.manual_charge_enabled = False
                    elif battery_known_not_charging:
                        # Enable instant manual charge at 1kW for 30 minutes
                        self.logger.info(
                            f"Battery not charging - enabling instant manual "
                            f"charge ({self.charging_power}kW for "
                            f"{self.charging_duration_minutes}min)"
                        )
                        await self.set_instant_manual_charge(
                            enable=True,
                            duration_minutes=self.charging_duration_minutes,
                            power_kw=self.charging_power
                        )
                        self.is_charging = True
                        self.manual_charge_enabled = True
                    else:
                        # Battery power unknown (cloud/connection/schema issue).
                        # Don't guess - register that the EV is charging and let a
                        # later poll, once we have valid data, decide whether to
                        # enable manual charge (handled by the continues branch).
                        self.logger.warning(
                            "Battery power unavailable at charge start - "
                            "deferring manual-charge decision to next poll"
                        )
                        self.is_charging = True
                        self.manual_charge_enabled = False

                elif not zappi_charging and self.is_charging:
                    # EV charging looks stopped - but Octopus runs its slots
                    # back-to-back with ~30s gaps, and releasing the lock in a
                    # gap lets the battery dump several kW into the car the
                    # moment the next slot opens. Hold everything until the
                    # stop has persisted long enough to be a real session end.
                    if self.ev_stop_first_seen is None:
                        self.ev_stop_first_seen = datetime.now()
                        self.logger.info(
                            f"EV charging paused (SOC: {battery_soc}%, "
                            f"Power: {battery_power}kW) - holding battery lock "
                            f"in case this is just a slot gap"
                        )
                    elif (datetime.now() - self.ev_stop_first_seen).total_seconds() \
                            >= 3 * self.poll_interval:
                        self.logger.info(
                            f"🔌 EV charging stopped. "
                            f"Battery SOC: {battery_soc}%, Power: {battery_power}kW"
                        )

                        # Disable instant manual charge only if WE enabled it
                        if self.manual_charge_enabled:
                            self.logger.info("Disabling instant manual battery charge")
                            await self.set_instant_manual_charge(
                                enable=False, duration_minutes=0, power_kw=0
                            )
                            self.manual_charge_enabled = False

                        self.is_charging = False
                        self.diversion_guard_active = False
                        self.diversion_discharge_polls = 0
                        self.in_diversion = False
                        self.ev_stop_first_seen = None

                elif zappi_charging and self.is_charging:
                    # EV charging continues
                    if self.in_diversion and not self.diversion_guard_active:
                        # Session is (or has become) solar diversion - the
                        # battery must stay free. If we forced a charge earlier
                        # (e.g. session started as an Octopus boost), release it.
                        if self.manual_charge_enabled:
                            self.logger.info(
                                "Zappi switched to solar diversion - disabling "
                                "instant manual charge to free the battery"
                            )
                            await self.set_instant_manual_charge(
                                enable=False, duration_minutes=0, power_kw=0
                            )
                            self.manual_charge_enabled = False
                            self.diversion_discharge_polls = 0
                        else:
                            # Guard: sustained heavy discharge means the sun is
                            # gone for real and the battery is funding the car
                            # (its discharge keeps the grid CT at zero, so the
                            # Zappi never pauses on its own). Pin the battery;
                            # the Zappi then sees genuine grid import and its
                            # Eco+ logic stops the session.
                            if battery_power_known and \
                                    battery_power <= -self.diversion_max_discharge_kw:
                                self.diversion_discharge_polls += 1
                            elif battery_power_known:
                                self.diversion_discharge_polls = 0
                            # Unknown power: keep the count, decide when data returns

                            if self.diversion_discharge_polls >= self.diversion_sustained_polls:
                                self.logger.info(
                                    f"Battery discharging "
                                    f"{abs(battery_power)}kW for "
                                    f"{self.diversion_discharge_polls} polls during "
                                    f"solar diversion - solar can no longer cover "
                                    f"the car. Pinning battery so the Zappi sees "
                                    f"the grid deficit and pauses"
                                )
                                await self.set_instant_manual_charge(
                                    enable=True,
                                    duration_minutes=self.charging_duration_minutes,
                                    power_kw=self.charging_power
                                )
                                self.manual_charge_enabled = True
                                self.diversion_guard_active = True
                                self.diversion_discharge_polls = 0
                            else:
                                self.logger.debug(
                                    f"Status: solar diversion charging, battery "
                                    f"left alone (SOC: {battery_soc}%, "
                                    f"Power: {battery_power}kW, discharge polls: "
                                    f"{self.diversion_discharge_polls}/"
                                    f"{self.diversion_sustained_polls})"
                                )
                    elif battery_already_charging:
                        if self.manual_charge_enabled and self._manual_charge_expiring():
                            # Renew before the window lapses - reacting after
                            # expiry leaks ~a poll's worth of heavy discharge
                            # into the car.
                            self.logger.info(
                                f"Manual charge window closing - refreshing "
                                f"({self.charging_power}kW for "
                                f"{self.charging_duration_minutes}min)"
                            )
                            await self.set_instant_manual_charge(
                                enable=True,
                                duration_minutes=self.charging_duration_minutes,
                                power_kw=self.charging_power
                            )
                        else:
                            # Battery is charging (either from us or system
                            # AI/timer) - no action needed
                            self.logger.debug(
                                f"Status: charging EV, Battery charging "
                                f"(SOC: {battery_soc}%, Power: {battery_power}kW)"
                            )
                    elif not battery_power_known:
                        # Power unknown (cloud/connection/schema issue) - skip the
                        # (re)enable this poll rather than acting on missing data.
                        # The pre-expiry refresh still runs, though: it needs no
                        # battery reading, and letting the window lapse during a
                        # cloud blip drops the pin exactly while the discharge
                        # is invisible to us.
                        if self.manual_charge_enabled and \
                                self._manual_charge_expiring():
                            self.logger.info(
                                f"Manual charge window closing (battery power "
                                f"unknown) - refreshing ({self.charging_power}kW "
                                f"for {self.charging_duration_minutes}min)"
                            )
                            await self.set_instant_manual_charge(
                                enable=True,
                                duration_minutes=self.charging_duration_minutes,
                                power_kw=self.charging_power
                            )
                        else:
                            self.logger.debug(
                                "Status: charging EV, battery power unknown - "
                                "skipping manual-charge (re)enable this poll"
                            )
                    elif self.manual_charge_enabled and \
                            isinstance(battery_soc, (int, float)) and \
                            battery_soc >= 99.0:
                        # Full battery can't accept charge, so 0kW here is the
                        # healthy pinned state - the manual charge is purely a
                        # discharge-lock now. Only the pre-expiry refresh is
                        # needed; re-enabling every poll would hammer the API.
                        if self._manual_charge_expiring():
                            self.logger.info(
                                f"Manual charge window closing - refreshing "
                                f"({self.charging_power}kW for "
                                f"{self.charging_duration_minutes}min)"
                            )
                            await self.set_instant_manual_charge(
                                enable=True,
                                duration_minutes=self.charging_duration_minutes,
                                power_kw=self.charging_power
                            )
                        else:
                            self.logger.debug(
                                f"Status: charging EV, battery full "
                                f"(SOC: {battery_soc}%) - discharge-lock held"
                            )
                    else:
                        # Battery is NOT charging - enable/re-enable manual charge
                        if not self.manual_charge_enabled:
                            self.logger.info(
                                f"Battery not charging - enabling instant manual "
                                f"charge ({self.charging_power}kW for "
                                f"{self.charging_duration_minutes}min)"
                            )
                        else:
                            # Battery stopped charging even though we enabled it
                            # (API failure or timer expired before the refresh)
                            # Re-enable to ensure it charges
                            self.logger.warning(
                                f"Battery not charging despite manual charge "
                                f"enabled (SOC: {battery_soc}%, Power: {battery_power}kW) "
                                f"- retrying enable"
                            )

                        # Always (re-)enable when battery should be charging but isn't
                        await self.set_instant_manual_charge(
                            enable=True,
                            duration_minutes=self.charging_duration_minutes,
                            power_kw=self.charging_power
                        )
                        self.manual_charge_enabled = True

                else:
                    # No state change. First, the post-disable watchdog: if
                    # we recently disabled the manual charge but the battery
                    # is still pulling power, the inverter ignored the
                    # disable - re-send it rather than leave an unwatched
                    # grid charge running.
                    if (self.disable_verify_until and battery_power_known
                            and (self.disable_verify_from is None
                                 or datetime.now()
                                 >= self.disable_verify_from)):
                        if datetime.now() >= self.disable_verify_until:
                            self.disable_verify_until = None
                            self.disable_retries = 0
                        elif battery_power > 0.5:
                            # Ask Sigenergy directly whether the pin is
                            # really still on - charging power alone can
                            # also be solar or the inverter's own plan.
                            state = await self.get_instant_manual_charge()
                            if state is not None and not state.get('enable'):
                                self.logger.debug(
                                    "Disable confirmed by API; charging "
                                    "power is from another source"
                                )
                                self.disable_verify_until = None
                                self.disable_retries = 0
                            elif self.disable_retries < 3:
                                self.disable_retries += 1
                                self.logger.warning(
                                    f"Battery still charging "
                                    f"({battery_power}kW) after disable - "
                                    f"re-sending disable (attempt "
                                    f"{self.disable_retries}/3)"
                                )
                                await self.set_instant_manual_charge(
                                    enable=False, duration_minutes=0,
                                    power_kw=0
                                )
                            else:
                                self.logger.error(
                                    f"Battery still charging "
                                    f"({battery_power}kW) after 3 disable "
                                    f"retries - may need manual intervention"
                                )
                                self.disable_verify_until = None
                                self.disable_retries = 0

                    # Log status periodically
                    if isinstance(battery_power, (int, float)):
                        if battery_power > 0:
                            power_status = "charging"
                        elif battery_power < 0:
                            power_status = "discharging"
                        else:
                            power_status = "idle"
                        self.logger.debug(
                            f"Status: idle, Battery SOC: {battery_soc}%, "
                            f"Battery {power_status}: {abs(battery_power)}kW"
                        )
                    else:
                        self.logger.debug(
                            f"Status: idle, Battery SOC: {battery_soc}% "
                            f"(battery power: {battery_power})"
                        )

                # Wait before next poll
                await asyncio.sleep(self.poll_interval)

            except KeyboardInterrupt:
                self.logger.info("Received shutdown signal")
                break
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    try:
                        await self._reconnect("recover after repeated errors")
                        consecutive_errors = 0
                    except Exception:
                        # Keep retrying after sleep even if reconnect failed
                        pass
                # Continue monitoring despite errors
                await asyncio.sleep(self.poll_interval)
            else:
                consecutive_errors = 0

    async def shutdown(self):
        """Graceful shutdown - restore battery to normal operation"""
        self.logger.info("Shutting down BatterySitter...")
        self.running = False

        # Disable instant manual charge only if WE enabled it
        if self.manual_charge_enabled:
            self.logger.info("Disabling instant manual charge on shutdown")
            try:
                await self.set_instant_manual_charge(enable=False, duration_minutes=0, power_kw=0)
                self.manual_charge_enabled = False
            except Exception as e:
                self.logger.error(f"Error disabling instant manual charge: {e}")

        await self.disconnect()
        self.logger.info("Shutdown complete")

    async def run(self):
        """Run the battery sitter service"""
        try:
            await self.connect()
            await self.monitor_loop()
        finally:
            await self.shutdown()


async def main():
    """
    Main entry point - for testing only!
    In production, use run.py which loads configuration from config.json
    """
    # Configure logging
    rotating_handler = RotatingFileHandler(
        'battery_sitter.log', maxBytes=5_000_000, backupCount=3
    )
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            rotating_handler,
            logging.StreamHandler(sys.stdout)
        ]
    )

    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("BatterySitter starting in DIRECT MODE")
    logger.info("For production use, run: python3 run.py")
    logger.info("=" * 60)

    # Load from config.json if available, otherwise exit
    import json
    import os

    if not os.path.exists('config.json'):
        logger.error("config.json not found!")
        logger.error("Please copy config.example.json to config.json and configure it")
        sys.exit(1)

    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    charge_duration = config['sigenergy'].get('charging_duration_minutes', 30)

    sitter = BatterySitter(
        zappi_username=config['zappi']['username'],
        zappi_password=config['zappi']['password'],
        zappi_serial=config['zappi']['serial'],
        sigenergy_username=config['sigenergy']['username'],
        sigenergy_password=config['sigenergy']['password'],
        sigenergy_region=config['sigenergy']['region'],
        poll_interval=config['polling']['interval_seconds'],
        charging_power=config['sigenergy']['charging_power'],
        charging_duration_minutes=charge_duration
    )

    # Handle signals for graceful shutdown
    loop = asyncio.get_event_loop()

    def signal_handler(sig, _frame):
        logger.info(f"Received signal {sig}")
        loop.create_task(sitter.shutdown())
        loop.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run the service
    await sitter.run()


if __name__ == "__main__":
    asyncio.run(main())
