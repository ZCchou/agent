# Security

Do not include API keys, local databases, prompts, private project content or logs containing personal information in public issues. Use GitHub's private vulnerability reporting when enabled, or contact the repository owner privately.

Model output, downloaded pages, skills and project documents are untrusted input. Review proposed file changes and terminal commands. Terminal commands execute with the current user's OS privileges, including access outside their working directory and network access; this feature is not an OS sandbox. Recovery points do not undo arbitrary terminal side effects.

The application stores provider secrets in the OS credential manager. Never commit credentials or updater private keys. The tracked updater public key is intentionally empty: update checks fail closed before contacting a feed until a new signing identity is configured.
