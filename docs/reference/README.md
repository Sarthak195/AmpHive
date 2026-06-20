# Reference Material

Background notes on the **upstream / inspiration projects** that AmpHive builds
on. These are *not* AmpHive's own code or runtime docs — they are research
context kept for convenience. (They were previously the loose
`esp32_tailscale_wol_context/` and `headscale_context/` folders at the repo root;
moved here on 2026-06-20 to declutter the root.)

| Folder | What it documents | Related AmpHive code |
|--------|-------------------|----------------------|
| [`esp32-tailscale-wol/`](esp32-tailscale-wol/) | ESP32-S3 Tailscale VPN gateway + Wake-on-LAN firmware — the prior art for an ESP32 speaking the Tailscale protocol. | `firmware/components/microlink/`, `firmware/components/wireguard_lwip/` |
| [`headscale/`](headscale/) | Headscale, the self-hosted Tailscale control server (key exchange, ACLs, DERP, DB schema). | The overlay control plane (`deploy/k8s/headscale.yaml`, the VPN plane in [../ARCHITECTURE.md](../ARCHITECTURE.md)) |

> Live, buildable copies of these upstream projects are also referenced as git
> submodules under [`../../context_repos/`](../../context_repos/)
> (`ESP32-Tailscale-WoL`, `headscale`, and `ChargeHub`). The folders here hold the
> distilled *notes*; the submodules hold the actual source.
</content>
