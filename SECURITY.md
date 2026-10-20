# Security policy

## Reporting a problem

Please report security problems privately and not in a public issue. Use GitHub's private vulnerability reporting for this repository. Open the Security tab of the repository, choose "Report a vulnerability", and describe the problem there. Only the maintainer can see a report made this way.

ablit is maintained by one person, so no response time is promised, but every report will be read. Please include the version or commit, the steps that reproduce the problem, and what you expected to happen.

No release exists yet. Until the first release, only the main branch is covered.

## What ablit handles

ablit runs on your computer, and it handles three kinds of secret.

1. Marketplace API keys. They are stored in the operating system keyring, or kept for the current session only. ablit never writes a plaintext key to disk without asking. A key that is given to the rented machine is limited, where the marketplace allows it, to destroying that one instance, and the machine never receives your full account key.
2. Endpoint keys. Each run gets a random 32-byte key for the inference endpoint. It is passed to the engine through an environment variable, saved in the keyring under the run ID, and never placed on a command line. The engine listens on `127.0.0.1` on the rented machine, and requests with no key or a wrong key are refused.
3. SSH access to rented machines. The tunnel to the engine checks the host key of the rented machine, and ablit uses a free local port.

A redaction filter removes the endpoint key and the marketplace keys from logs, run files and the output of `ablit up --report`. Tests use fake secrets to check it.

## Problems to report

- A secret that reaches a log, a run file, a report, a command line or the network when it should not.
- A way to make ablit rent, keep or leave running a machine without the confirmation the user gave.
- A way to make the spend cap fail without the notice in the README applying.
- A catalog entry or a file from a model repository that can make ablit run a command that no reviewed engine template contains.
- A way for referral data to influence a ranking. POLICY.md describes the rule.
- A flaw in how ablit checks file sizes, hashes or revisions before it runs a downloaded file.

## Known limits

If the rented machine freezes or loses its network while your computer is off or asleep, neither the watchdog nor the local check can run, and the marketplace keeps billing. The spend-cap notice in the README describes this. It is a documented limit, so it is not a vulnerability by itself.

Engines and model files come from third parties. ablit pins revisions and checks hashes, and it cannot judge the code inside an engine or a model.

None of the bootstrap scripts has run on a rented machine yet, so the behavior they describe is not tested.
