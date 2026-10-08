# Governance

Knowledge Layer is maintained by Quiet Victory Labs (QVL). The repository is private during its alpha qualification; the Apache-2.0 license and contribution process are in place for a later public release. Repository access does not imply that the service is qualified for a customer's production data.

## Decision rights

- QVL maintainers own releases, security response, compatibility policy, and the default branch.
- Source and domain experts own business meaning. An agent may draft a definition or relationship, but it may not certify or publish one on the expert's behalf.
- Changes to authentication, source scope, credential handling, retained evidence, publication rules, migrations, or API/MCP contracts require a documented risk and compatibility review in the pull request.
- A connector is described as qualified only after a restricted-role, denied-access, drift, and credential-rotation test against that provider. A generic adapter is not a certification.

## Changes

Open an issue for significant behavior or contract changes, then send a focused pull request. The author supplies tests for changed behavior and updates the relevant operator documentation. A maintainer reviews the change and its evidence before merge. Security reports follow [SECURITY.md](SECURITY.md), outside public issues.

The default branch is the integration branch. Releases use immutable `vMAJOR.MINOR.PATCH` tags and the procedure in [RELEASING.md](RELEASING.md). Before public launch, QVL will publish named maintainer and security contacts, verify license compatibility, and revisit this governance document with prospective contributors.
