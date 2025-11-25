#!/usr/bin/env python3
"""
BatterySitter - Prevents Sigenstore battery from discharging during Zappi car charging

This script monitors a Zappi EV charger and when it detects charging is active,
changes the Sigenstore operational mode to prevent battery discharge, ensuring grid
power is used for EV charging instead of depleting the home battery.
"""

import asyncio
import logging
import sys
from typing import Optional
from datetime import datetime, timedelta
import signal

from pymyenergi.connection import Connection
from pymyenergi.zappi import Zappi
from sigen import Sigen


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
        # Polling settings
        poll_interval: int = 30,
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
            poll_interval: Seconds between status checks
        """
        self.zappi_username = zappi_username
        self.zappi_password = zappi_password
        self.zappi_serial = zappi_serial

        self.sigenergy_username = sigenergy_username
        self.sigenergy_password = sigenergy_password
        self.sigenergy_region = sigenergy_region
        self.charging_power = charging_power

        self.poll_interval = poll_interval

        self.logger = logging.getLogger(__name__)
        self.zappi: Optional[Zappi] = None
        self.sigen: Optional[Sigen] = None
        self.is_charging = False
        self.running = False
        self.manual_charge_enabled = False  # Track if WE enabled manual charge
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
        modes = await self.sigen.get_operational_modes()
        self.logger.info("Available Sigenergy operational modes:")
        for mode in modes:
            self.logger.info(f"  - {mode['label']} (value: {mode['value']})")

        current_mode = await self.sigen.get_operational_mode()
        self.logger.info(f"Current operational mode: {current_mode}")

        self.last_connection_time = datetime.now()
        self.logger.info("Successfully connected to both devices")

    async def disconnect(self):
        """Close connections"""
        self.logger.info("Disconnected from devices")

    async def get_zappi_charging_status(self) -> bool:
        """
        Check if Zappi is currently charging

        Returns:
            True if charging is active, False otherwise
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

            self.logger.debug(
                f"Zappi status: {self.zappi.status}, "
                f"plug_status: {self.zappi.plug_status}, "
                f"charge_mode: {self.zappi.charge_mode}"
            )

            return is_charging

        except Exception as e:
            self.logger.error(f"Error reading Zappi status: {e}")
            return False

    async def get_battery_info(self) -> dict:
        """
        Get current battery and energy flow information

        Returns:
            Dictionary with energy flow data
        """
        try:
            energy_flow = await self.sigen.get_energy_flow()
            # Handle case where API returns None instead of raising exception
            if energy_flow is None:
                self.logger.warning("get_energy_flow() returned None")
                return {}
            return energy_flow
        except Exception as e:
            self.logger.error(f"Error reading battery info: {e}")
            # Log the raw data for debugging
            try:
                raw_data = getattr(self.sigen, '_last_response', None)
                if raw_data:
                    self.logger.error(f"Raw response data: {raw_data}")
            except Exception:
                pass
            return {}

    async def set_operational_mode_by_name(self, mode_name: str):
        """
        Set operational mode by its label name

        Args:
            mode_name: Name of the mode (e.g., "Maximum Self-Powered")
        """
        try:
            modes = await self.sigen.get_operational_modes()

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

            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    url, headers=self.sigen.headers, json=payload
                ) as response:
                    result = await response.json()

                    if enable:
                        self.logger.info(
                            f"Enabled instant manual charge: "
                            f"{power_kw}kW for {duration_minutes} minutes"
                        )
                    else:
                        self.logger.info("Disabled instant manual charge")

                    return result

        except Exception as e:
            self.logger.error(f"Error setting instant manual charge: {e}")
            raise

    async def monitor_loop(self):
        """Main monitoring loop"""
        self.running = True
        self.logger.info("Starting monitoring loop...")
        self.logger.info(f"Will check status every {self.poll_interval} seconds")
        self.logger.info(
             f"When EV charging detected: "
             f"Enable instant manual battery charge at {self.charging_power}kW for 30min"
        )

        while self.running:
            try:
                # Periodic reconnection to refresh tokens/sessions
                if self.last_connection_time:
                    time_since_connection = datetime.now() - self.last_connection_time
                    if time_since_connection >= self.reconnect_interval:
                        self.logger.info(
                            f"Reconnecting after {time_since_connection.total_seconds() / 3600:.1f} hours"
                        )
                        await self.connect()

                # Check Zappi charging status
                zappi_charging = await self.get_zappi_charging_status()

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

                # Debug log the raw values if they're not numeric
                if not isinstance(battery_power, (int, float)) or \
                   not isinstance(battery_soc, (int, float)):
                    self.logger.debug(
                        f"Non-numeric battery data - "
                        f"SOC: {battery_soc} (type: {type(battery_soc).__name__}), "
                        f"Power: {battery_power} (type: {type(battery_power).__name__})"
                    )

                # Check if battery is already charging from another source
                # (AI, timer, etc.)
                battery_already_charging = (
                    isinstance(battery_power, (int, float)) and
                    battery_power > 0
                )

                # Handle state changes
                if zappi_charging and not self.is_charging:
                    # EV charging just started
                    self.logger.info(
                        f"⚡ EV charging detected! "
                        f"Battery SOC: {battery_soc}%, Power: {battery_power}W"
                    )

                    if battery_already_charging:
                        self.logger.info(
                            "Battery is already charging - "
                            "not overriding existing charge control"
                        )
                        self.is_charging = True
                        self.manual_charge_enabled = False
                    else:
                        # Enable instant manual charge at 1kW for 30 minutes
                        self.logger.info(
                            f"Battery not charging - enabling instant manual "
                            f"charge ({self.charging_power}kW for 30min)"
                        )
                        await self.set_instant_manual_charge(
                            enable=True, duration_minutes=30,
                            power_kw=self.charging_power
                        )
                        self.is_charging = True
                        self.manual_charge_enabled = True

                elif not zappi_charging and self.is_charging:
                    # EV charging just stopped
                    self.logger.info(
                        f"🔌 EV charging stopped. "
                        f"Battery SOC: {battery_soc}%, Power: {battery_power}W"
                    )

                    # Disable instant manual charge only if WE enabled it
                    if self.manual_charge_enabled:
                        self.logger.info("Disabling instant manual battery charge")
                        await self.set_instant_manual_charge(
                            enable=False, duration_minutes=0, power_kw=0
                        )
                        self.manual_charge_enabled = False

                    self.is_charging = False

                elif zappi_charging and self.is_charging:
                    # EV charging continues
                    if battery_already_charging:
                        # Battery is charging (either from us or system AI/timer) - no action needed
                        self.logger.debug(
                            f"Status: charging EV, Battery charging "
                            f"(SOC: {battery_soc}%, Power: {battery_power}W)"
                        )
                    else:
                        # Battery is NOT charging - enable/re-enable manual charge
                        if not self.manual_charge_enabled:
                            self.logger.info(
                                f"Battery not charging - enabling instant manual "
                                f"charge ({self.charging_power}kW for 30min)"
                            )
                        else:
                            # Battery stopped charging even though we enabled it
                            # (API failure, timer expired, or SOC at 100%)
                            # Re-enable to ensure it charges
                            self.logger.warning(
                                f"Battery not charging despite manual charge "
                                f"enabled (SOC: {battery_soc}%, Power: {battery_power}W) "
                                f"- retrying enable"
                            )

                        # Always (re-)enable when battery should be charging but isn't
                        await self.set_instant_manual_charge(
                            enable=True, duration_minutes=30,
                            power_kw=self.charging_power
                        )
                        self.manual_charge_enabled = True

                else:
                    # No state change - just log status periodically
                    if isinstance(battery_power, (int, float)):
                        if battery_power > 0:
                            power_status = "charging"
                        elif battery_power < 0:
                            power_status = "discharging"
                        else:
                            power_status = "idle"
                        self.logger.debug(
                            f"Status: idle, Battery SOC: {battery_soc}%, "
                            f"Battery {power_status}: {abs(battery_power)}W"
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
                # Continue monitoring despite errors
                await asyncio.sleep(self.poll_interval)

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
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('battery_sitter.log'),
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

    sitter = BatterySitter(
        zappi_username=config['zappi']['username'],
        zappi_password=config['zappi']['password'],
        zappi_serial=config['zappi']['serial'],
        sigenergy_username=config['sigenergy']['username'],
        sigenergy_password=config['sigenergy']['password'],
        sigenergy_region=config['sigenergy']['region'],
        poll_interval=config['polling']['interval_seconds'],
        charging_power=config['sigenergy']['charging_power']
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
