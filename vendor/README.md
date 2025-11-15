# Vendor Directory

This directory contains vendored (backed-up) copies of third-party dependencies.

## sigen (v0.1.9)

**Source:** https://pypi.org/project/sigen/
**Original GitHub:** https://github.com/fbradyirl/sigen (currently 404)
**License:** MIT (see sigen_LICENSE)
**Date backed up:** 2025-11-15

### Why vendored?

The `sigen` library is critical to BatterySitter's operation, providing the interface to Sigenergy cloud APIs. The original GitHub repository is no longer accessible (404), and the PyPI package description states:

> "This repository is only sporadically maintained. Breaking API changes will be maintained on a best efforts basis."

To ensure long-term availability and prevent dependency failures, we've backed up the library here.

### Installation

The library should still be installed via pip from PyPI when available:
```bash
pip install sigen
```

If PyPI becomes unavailable, you can install from this backup:
```bash
pip install ./vendor/sigen.py
```

Or import directly in the code by adjusting the Python path.

### Version Info

- **Version:** 0.1.9
- **File size:** ~15KB (single file)
- **Dependencies:** aiohttp, pycryptodome, requests
- **Python:** >=3.9
