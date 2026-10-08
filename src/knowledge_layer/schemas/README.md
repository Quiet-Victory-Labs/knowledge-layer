The `ossie-schema-v0.2.0.dev0.json` file is copied from Apache Ossie at
commit `b6c702ed1c07e91382a69e870c875cbd19570828`,
`core-spec/ossie-schema.json`. The upstream project licenses the specification
under Apache License 2.0. Pinning this copy prevents a moving development schema
from silently changing local contract validation. The accompanying upstream
NOTICE is included next to the schema for wheel and image distributions.

Upstream: https://github.com/apache/ossie/blob/b6c702ed1c07e91382a69e870c875cbd19570828/core-spec/ossie-schema.json

`knowledge_shapes.ttl` is the canonical project-owned SHACL Core profile for
Knowledge Layer snapshot RDF projections. It is bundled in wheels and images;
optional offline validation loads it through package resources. See
[validation and limits](../../../docs/rdf-validation.md).
