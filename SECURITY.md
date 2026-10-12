# Security

Report vulnerabilities privately through GitHub Security Advisories for this repository. Include the affected release, a reproduction and the expected trust boundary. Do not publish working credentials or sensitive customer source.

Forge 0.1 is a single-operator development factory. A token grants access to repository source, generated code, worker commands and publication. Run it on a dedicated trusted development host, bind to loopback, and use TLS/access controls when exposing it remotely. CLI sandboxes and confined Agentic file tools do not constitute a multi-tenant operating-system sandbox for verification commands. Never connect an untrusted repository to a host holding unrelated production credentials.

Secrets use environment configuration and Git credential helpers. Generated artifacts exclude private paths. Interrupted runs require an explicit retry. Pull-request publication requires passing verification; Forge never merges generated changes.
