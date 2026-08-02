# firmware-images/

Drop prebuilt, merged firmware images here — the flasher looks in this
folder next to itself by default (or next to `AmpHiveFlasher.exe` when
running the packaged build).

Expected filenames (see `amphive_flasher/images.py::CHIP_IMAGE_FILENAMES`):

- `amphive-gateway-esp32c3-merged.bin`
- `amphive-gateway-esp32-merged.bin`

These are produced by a maintainer with the full ESP-IDF toolchain — see
`tools/flasher/scripts/build-merged-image.ps1` and the "Maintainer: producing
images" section of `tools/flasher/README.md`. They are build artifacts, not
source, so `.bin` files in this folder are gitignored; only this README is
tracked to keep the folder present in a checkout.
