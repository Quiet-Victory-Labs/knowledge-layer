# Graph lab third-party credits

The comparison uses these upstream projects directly. Versions and license evidence below were checked against the installed packages and local upstream checkouts on 2026-10-04.

| Interface / upstream | Installed version | License and attribution | Local license evidence |
| --- | --- | --- | --- |
| [Cytoscape.js](https://github.com/cytoscape/cytoscape.js) and [fCoSE](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose) | `cytoscape` 3.34.3; `cytoscape-fcose` 2.2.0 | MIT. Cytoscape Consortium, 2016–2026; iVis-at-Bilkent, 2018–present. | `web/node_modules/cytoscape/LICENSE`; `web/node_modules/cytoscape-fcose/LICENSE` |
| [AntV G6](https://github.com/antvis/G6) | `@antv/g6` 5.1.1 | MIT. Alipay.inc, 2018. | `web/node_modules/@antv/g6/LICENSE` |
| [Reactodia Workspace](https://github.com/reactodia/reactodia-workspace) | `@reactodia/workspace` 0.35.2 | LGPL-2.1-or-later. Alexey Morozov, 2026; metaphacts GmbH, 2019. | `web/node_modules/@reactodia/workspace/LICENSE.md` and `NOTICE.md` |
| [Zazuko Graph Explorer](https://github.com/zazuko/graph-explorer) | `graph-explorer` 2.1.0 | LGPL-2.1-or-later in the bundled license. Netage B.V.; Zazuko GmbH; based on Ontodia, metaphacts GmbH, 2019. | `web/node_modules/graph-explorer/LICENSE` |
| [AWS Graph Explorer](https://github.com/aws/graph-explorer/tree/v3.2.2) | v3.2.2, commit `0f17c39375e2f6f04edf7d7a0c852fec6aff7c1e` | Apache-2.0. Amazon.com, Inc. or its affiliates. | `.local/graph-lab/aws/LICENSE` and `NOTICE` |
| [Skosmos](https://github.com/NatLibFi/Skosmos) | Official v3.4 container, `linux/amd64`, digest below | MIT for Skosmos source. Aalto University / University of Helsinki, 2012–2013; University of Helsinki / National Library of Finland, 2013–2016. | `.local/graph-lab/skosmos/LICENSE` |

Skosmos image: `quay.io/natlibfi/skosmos@sha256:4f9503f6a57e3c2b5ee73d7f224784ab45edeecac78c4a6ebd9edcdfefbb454b`. Its OCI metadata identifies v3.4 and upstream revision `410dd659a9fdad9579f66f74e6a7d5238c77f7ca`; the separate local source checkout is `f135b2c3feb9eaddfc6d0afe55c8f9ce6086b98c`. The image's license label is `NOASSERTION`; the source MIT text is not a complete inventory of its PHP, JavaScript or operating-system dependencies.

Zazuko's package metadata abbreviates the license as `LGPL-2.1`; its bundled LICENSE explicitly allows version 2.1 or any later version. Reactodia's package declaration and NOTICE also allow later versions. These two semantic workspaces are distinct from the MIT renderers and Apache AWS app; preserve their upstream license/notice files when packaging them.

This page credits the six comparison options. It is not the complete transitive-dependency notice bundle; use the repository's [dependency license review](dependency-license-review.md) for release packaging. The local comparison runtime's optional `graph-lab` Python extra locks `pyoxigraph` 0.5.11 and keeps it outside the base profile.

See [Graph lab reproduction and scope](graph-lab.md) for the actual exposed controls, retained-copy boundary and startup commands.
