# MegaRAID Exporter

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus-exporter-E6522C?logo=prometheus&logoColor=white)

Lightweight Prometheus exporter for Broadcom/AVAGO MegaRAID controllers. It reads StorCLI JSON output and exposes controller, virtual drive, and physical drive health metrics.

## Features

- Read-only StorCLI commands
- Multi-controller support
- Prometheus-compatible metrics
- Cached scrapes with shorter failure TTL
- Liveness and readiness endpoints
- No third-party Python dependencies

## Metrics

| Metric | Description |
| --- | --- |
| `megaraid_exporter_up` | `1` when the complete StorCLI scrape succeeds |
| `megaraid_exporter_scrape_error` | Last scrape error by stable reason |
| `megaraid_controller_scrape_success` | Per-controller scrape status |
| `megaraid_controller_info` | Controller model, serial and firmware information |
| `megaraid_controller_health` | Controller health state |
| `megaraid_virtual_drive_state` | Virtual drive state |
| `megaraid_physical_drive_state` | Physical drive state |
| `megaraid_storcli_command_duration_seconds` | StorCLI command duration |

## Quick start

Build the image:

```bash
docker build -t megaraid-exporter:1.0.0 .
```

Run it with the MegaRAID ioctl device mounted into the container:

```bash
docker run --rm \
  --name megaraid-exporter \
  -p 9634:9634 \
  -v /proc/devices:/host/proc/devices:ro \
  -v /dev:/host/dev:ro \
  --device /dev/megaraid_sas_ioctl_node:/dev/megaraid_sas_ioctl_node \
  megaraid-exporter:1.0.0
```

Open `http://localhost:9634/metrics`.

## Endpoints

| Path | Purpose |
| --- | --- |
| `/metrics` | Prometheus metrics |
| `/healthz` | Process liveness |
| `/readyz` | StorCLI readiness; returns `503` after a failed scrape |

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `LISTEN_ADDR` | `0.0.0.0` | HTTP listen address |
| `LISTEN_PORT` | `9634` | HTTP listen port |
| `STORCLI_PATH` | `/opt/MegaRAID/storcli/storcli64` | StorCLI executable |
| `STORCLI_TIMEOUT_SECONDS` | `20` | Command timeout |
| `SCRAPE_CACHE_SECONDS` | `300` | Successful scrape TTL |
| `SCRAPE_FAILURE_CACHE_SECONDS` | `15` | Failed scrape TTL |
| `MEGARAID_CREATE_IOCTL_NODE` | `false` | Allow compatibility-mode device creation |

Device creation is disabled by default. Provision and mount the ioctl device outside the exporter whenever possible.

## Development

```bash
python -m unittest discover -s tests -v
```

The Docker build verifies the bundled StorCLI archive using SHA-256 before extraction.

## Versioning

This project follows [Semantic Versioning](https://semver.org/). Version `1.0.0` introduces the stable, unlabeled `megaraid_exporter_up` metric.

## License

Exporter source code is available under the [MIT License](LICENSE).

The bundled Broadcom StorCLI archive is distributed under Broadcom's separate Public Use License and is not covered by MIT. See [Third-party notices](THIRD_PARTY_NOTICES.md).
