"""AmpHive Gateway Flasher.

A friendly, no-toolchain-required Windows tool that detects a plugged-in ESP32
gateway over USB and flashes it with a prebuilt AmpHive firmware image.

This package intentionally never compiles firmware - see
``tools/flasher/README.md`` for the full design rationale. All per-device
configuration (Wi-Fi, MQTT broker credentials, plug roster) is handled at
runtime by the gateway's own captive portal, not baked into the image.
"""

__version__ = "1.0.0"
