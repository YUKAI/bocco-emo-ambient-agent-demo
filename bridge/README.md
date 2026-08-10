# bocco-bridge

Runtime service for the BOCCO emo and Hermes Agent demo. The public BOCCO
Webhook listener binds to loopback and is handed to Cloudflare Quick Tunnel.
The bridge calls the loopback-only Hermes Responses API and sends generated
replies back to the originating BOCCO room. Platform message IDs, with a
one-shot content-hash fallback, prevent those replies from re-entering Hermes
when BOCCO emits their `message.received` Webhook echoes.

See the repository root README and `docs/design/bridge-runtime.md` for setup.
