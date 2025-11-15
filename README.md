# BatterySitter

Automatically prevents your Sigenstore home battery from discharging when your Zappi EV charger is actively charging your car by enabling battery charge mode from the grid.

## Problem

When charging an EV with a Zappi charger, the Sigenstore battery may discharge to supply power to the car instead of pulling from the grid. This can be undesirable if you want to preserve your home battery for household use or if you have cheaper grid rates for EV charging.

## Solution

BatterySitter monitors your Zappi charger status and automatically enables battery charging when EV charging is detected:

- **EV Charging Detected** → Enable instant manual battery charge (e.g., 1kW for 30min) to pull from grid
- **EV Charging Stopped** → Disable manual charge, return to normal operation
- **Smart Detection** → If battery is already charging (via AI/timer), doesn't interfere

## Architecture

```
┌─────────────────┐
│  Zappi Charger  │
│   (MyEnergi)    │◄─── Monitor charging status
└─────────────────┘
        │
        │ pymyenergi
        │ (Cloud API)
        ▼
┌─────────────────┐
│ BatterySitter   │
│   (This App)    │
└─────────────────┘
        │
        │ sigen
        │ (Cloud API)
        ▼
┌─────────────────┐
│  Sigenstore     │
│   Battery       │◄─── Control operational mode
└─────────────────┘
```

## Requirements

- **Zappi EV Charger** with MyEnergi API access
- **Sigenstore Battery** with MySigen cloud account
- **Python 3.9+**
- **Internet connection** for both devices (WiFi is fine for Sigenstore)

## Installation

### 1. Clone or download this repository

```bash
cd ~/Projects
git clone <your-repo-url> BatterySitter
cd BatterySitter
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure credentials

```bash
cp config.example.json config.json
nano config.json  # Or use your preferred editor
```

Fill in your credentials in the JSON file:
- **MyEnergi Hub Serial**: Found on hub device or in MyEnergi app
- **MyEnergi API Key**: Generate in MyEnergi app → Advanced Settings
- **Zappi Serial**: Found on Zappi device or in app
- **MySigen Email/Password**: Your MySigen app login
- **Region**: Your Sigenergy region (eu, us, cn, apac)

### 5. Test the configuration

```bash
python3 run.py
```

Watch the logs to confirm it connects to both services successfully. Press `Ctrl+C` to stop.

## Usage

### Run manually

```bash
source venv/bin/activate
python3 run.py
```

### Run as a background service (Linux/Raspberry Pi)

1. **Edit the service file** to match your paths:

```bash
nano battery-sitter.service
```

Update `User`, `WorkingDirectory`, and `ExecStart` paths.

2. **Install the service**:

```bash
sudo cp battery-sitter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable battery-sitter
sudo systemctl start battery-sitter
```

3. **Check status**:

```bash
sudo systemctl status battery-sitter
```

4. **View logs**:

```bash
tail -f battery_sitter.log
# Or
journalctl -u battery-sitter -f
```

## Configuration Options

Edit `config.json` to customize:

| Setting | Description | Default |
|---------|-------------|---------|
| `sigenergy.charging_power` | Battery charge power in kW when EV charging | 1 |
| `polling.interval_seconds` | Seconds between status checks | 30 |

### Charging Power

The `charging_power` setting controls how much power (in kW) the battery charges at when EV charging is detected. Common values:
- **1 kW** - Gentle charge, minimal grid impact
- **2-3 kW** - Moderate charge rate
- **Higher values** - Faster charging (check your battery specs)

## How It Works

1. **Polling**: Every N seconds (default 30), checks Zappi charging status via MyEnergi cloud API
2. **Detection**: When Zappi status changes to "Charging" or "Boosting" with EV connected
3. **Smart Intervention**:
   - Checks if battery is already charging (from AI mode, timer, etc.)
   - If battery NOT charging: Enables instant manual charge at configured power (e.g., 1kW) for 30min
   - If battery IS charging: Does nothing (respects existing charge control)
4. **Monitoring**: Continues checking battery charge status during EV charging
   - If battery stops charging unexpectedly, re-enables manual charge
5. **Restoration**: When EV charging stops, disables manual charge (only if we enabled it)
6. **Logging**: All actions logged to `battery_sitter.log` and console

## Troubleshooting

### "Failed to get access token" (MyEnergi)
- Check your hub serial and API key
- API key must be generated in MyEnergi app settings

### "Failed to get access token" (Sigenergy)
- Verify your MySigen app email/password
- Confirm you selected the correct region

### Battery still discharging during EV charging
- Check the logs - is manual charge being enabled successfully?
- Verify `charging_power` is set appropriately in config.json
- Check if battery SOC is at maximum (won't charge if full)
- Review battery settings in MySigen app - some settings may prevent manual charge

### Script stops or crashes
- Check logs: `tail -f battery_sitter.log`
- If using systemd, it will auto-restart after 10 seconds

## Security Notes

- **Never commit `config.json`** to version control (it contains passwords)
- Store credentials securely
- The script only uses official cloud APIs from MyEnergi and Sigenergy
- All communication uses HTTPS

## Libraries Used

- **[pymyenergi](https://github.com/CJNE/pymyenergi)**: Async Python library for MyEnergi devices (Zappi, Eddi, Harvi)
- **[sigen](https://pypi.org/project/sigen/)**: Python library for Sigenergy cloud API
  - **Note**: The original GitHub repository is no longer accessible. A backup copy (v0.1.9) is maintained in the `vendor/` directory for archival purposes.

## License

MIT License - See LICENSE file

## Contributing

Contributions welcome! Please open an issue or PR.

## Disclaimer

This project is not affiliated with MyEnergi or Sigenergy. Use at your own risk.
