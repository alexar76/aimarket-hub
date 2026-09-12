# hello-capability

Minimal signed HTTP provider for the [15-minute developer quickstart](https://github.com/alexar76/argus/blob/main/docs/developer-guide/en.md).

| File | Use |
|------|-----|
| `capability.json` | Local dev (`127.0.0.1`) + `AIMARKET_ALLOW_LOCAL_PUBLISH=1` on hub |
| `capability.vps.json` | **VPS / Hub in Docker** — set `invoke_url` to `http://<PUBLIC_IP>:3456/invoke` |
| `publish.sh` | `CAPABILITY_PUBLIC_HOST=<PUBLIC_IP> ./publish.sh` |

Hub in Docker cannot call `127.0.0.1` on the host unless `AIMARKET_INVOKE_HOST_GATEWAY=host.docker.internal` is set on the hub (`deploy_hub.sh` does this).

```bash
python3 server.py
CAPABILITY_PUBLIC_HOST=<PUBLIC_IP> AIMARKET_ADMIN_TOKEN=... ./publish.sh
```
