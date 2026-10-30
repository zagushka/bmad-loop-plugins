# Delivery configuration

Copy [the example](../../examples/delivery.config.json) to a private location outside the repository. Set the real repository, destination branch, absolute private state directory, authorized orchestrators and expected check names. The file must be owned by your user and not writable by other users. Keep configuration outside the installed plugin directory, so an update cannot replace it.

An explicit empty `expected_checks` list is permitted only when Actions are disabled for the repository. Branch protection remains enforced. Unreadable Actions or branch-protection responses stop delivery. The plugin does not change Actions settings.

Set the ignored loop policy:

```toml
[tickets]
mark_done = "never"

[plugins.delivery]
config = "/absolute/private/delivery-config.json"
```

An empty `config` setting keeps delivery inert. Review the installation before granting content. The private configuration and state directory define the local trust boundary. An orchestrator name is an attestation, not a cryptographic signature. Restrict access to the authorized delivery process.

See the [full reference](README.md) for supported origins, transport restrictions, evidence fields, checks, retry limits and worker lifetime. Do not put secrets or private birthday data in evidence or check output.
