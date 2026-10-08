# Graph lab: local comparison

This is an archived, local-only comparison from commit `50803bb`. It is not
part of the production web app or its npm dependency lock. To reproduce the
six-way comparison below, use that commit in a separate checkout and follow
the commands there. The current app keeps the Knowledge Map for reviewing
published meaning and evidence.

Open **Graph lab** in the Knowledge Layer UI at [127.0.0.1:4173](http://127.0.0.1:4173/). The six choices use the actual upstream libraries or applications. They render retained knowledge; they do not construct, approve or publish semantic meaning.

| Interface | What this demo exposes |
| --- | --- |
| Cytoscape + fCoSE | Five layouts, compound source groups, fit/zoom, node/link selection, neighborhood expansion, neighbor and shortest-path highlighting, labels and PNG export. No group-collapse or minimap extension. |
| AntV G6 | Five layouts, native source combos with collapse/expand, minimap, fit/zoom, selection, neighborhood expansion, neighbor/path highlighting, labels and viewport PNG export. |
| Reactodia Workspace | Native search/classes/connections, force/flow layout, local notes, diagram tools/history, JSON save/load, SVG/PNG export and printing. |
| Zazuko Graph Explorer | Native class/search/connection panels, diagram history, JSON save/load, layout/zoom and SVG/PNG export. No annotation tool. |
| AWS Graph Explorer | Complete upstream application: graph, table and schema exploration, native query editor and saved views, connected through generic read-only SPARQL. Neptune Gremlin, openCypher, IAM and Neptune-specific summary facilities are unavailable here. |
| Skosmos | Complete upstream vocabulary browser: term search, alphabetical index, source collections and concept properties. Broader/narrower/related links require actual approved assertions; the projection invents no SKOS hierarchy. |

The first four share API search, source/type/review filters, an evidence inspector, records/relationships/schema tabs, JSON export and browser-local saved comparison views. Their view is bounded to 400 records; native library searches cover that loaded view. **Fetch selected neighborhood**, or double-click in Cytoscape/G6, retrieves another authorized API neighborhood. Matching-label hubs are navigation/reconciliation candidates. Undirected paths show connectivity, not approved joins or equivalent meaning.

## Data and edit boundaries

The embedded libraries read the current Knowledge Layer workspace API. Their layouts, grouping, notes and diagram files are browser-local presentation changes. Changing source filters reloads diagram history. Reactodia/Zazuko diagram imports are checked against the currently loaded records and relationship types.

AWS and Skosmos use the separate retained copy produced by [graph_lab_runtime.py](../examples/graph_lab_runtime.py). Export reads snapshots already retained by the local demo API; it does not refresh a source, query or contact upstream source systems. The runtime loads `knowledge.trig` into memory once and exposes read queries only. The copy stays fixed until explicitly exported again and the runtime restarted. **Reload view** reloads API/browser metadata; it does not rebuild this copy. Counts, export time and budget status are recorded in `.local/graph-lab/data/info.json`; `/lab/info` also reports the active query engine.

The copy retains source/snapshot identity, review/trust state and evidence references. Pending relationships use `pending_` predicate names and separate relationship records. Approved definitions become SKOS definitions; unreviewed wording remains marked review notes. Only concept/metric/ontology records enter the Skosmos vocabulary. Source `skos:Collection` groups are navigation collections, not concept ancestry.

This is a loopback, single-user comparison runtime, without production user authentication or live authorization/withdrawal checks on the exported copy. Keep the copied files and endpoints local. The SPARQL endpoint permits SELECT/ASK/CONSTRUCT/DESCRIBE, rejects updates, remote datasets and SERVICE clauses, and serializes query execution. Query-size limits are not a general execution-time limit.

## Ports and reproduction

| Address | Service |
| --- | --- |
| `127.0.0.1:4173` | Existing Knowledge Layer UI/API, including Graph lab |
| `127.0.0.1:4182/aws/explorer` | AWS Graph Explorer; static assets share port 4182 |
| `127.0.0.1:4182/sparql` | Read-only SPARQL over the retained copy |
| `127.0.0.1:4182/lab/info` / `/lab/knowledge.ttl` | Copy metadata / RDF download |
| `127.0.0.1:4183/example/en/` | Skosmos vocabulary browser |

Commands below run from the repository root and assume the prepared local checkout: existing Knowledge Layer registry/profile, installed web/AWS dependencies, `.local/graph-lab/aws` at **v3.2.2**, and the existing Skosmos configuration. A reusable [Skosmos configuration](../examples/graph_lab_skosmos_config.ttl) includes the explicit concept scheme and read-only bridge endpoint. Use the existing Knowledge Layer service on 4173; its normal setup is documented in the [README](../README.md#try-it-locally). Do not start another copy on that port.

Rebuild the embedded libraries with the existing Node 26 runtime:

```sh
KL_NODE_DIR="$PWD/.local/release-tools/node-v26.10.0-darwin-arm64/bin"
(cd web && PATH="$KL_NODE_DIR:$PATH" npm run build)
```

The optional graph-lab extra provides **pyoxigraph 0.5.11** for the standalone apps' schema scans. Without it, the runtime falls back to RDFLib. Install the locked Python profile when preparing this environment:

```sh
uv sync --extra graph-lab --extra dev
```

To replace the comparison copy, stop its task-owned runtime first, then export retained snapshots and start it again. Re-exporting overwrites the three comparison files; it changes no upstream records or services.

```sh
PYTHONPATH=src .venv/bin/python examples/graph_lab_runtime.py export \
  --api http://127.0.0.1:4173 --directory .local/graph-lab/data
PYTHONPATH=src .venv/bin/python examples/graph_lab_runtime.py serve \
  --directory .local/graph-lab/data \
  --aws-dist .local/graph-lab/aws/packages/graph-explorer/dist --port 4182
```

The serve command runs in the foreground; Ctrl-C stops that runtime. To rebuild the already prepared AWS checkout without its default 6 GiB build-heap setting:

```sh
(cd .local/graph-lab/aws/packages/graph-explorer && \
  PATH="$KL_NODE_DIR:$PATH" NODE_OPTIONS=--max-old-space-size=2048 \
  ./node_modules/.bin/vite build --mode production)
```

AWS ships identifier labels by default. The native styling import at `/lab/aws-styles.json` sets readable `rdfs:label` names for every class; load it under **Settings → Styles → Load styles**. This changes browser styles only. Native partial matching searches language-tagged RDF labels. Graph queries requesting JSON receive Neptune-compatible triple bindings; RDF/XML and Turtle remain available.

The Skosmos container mounts a local copy of its own v3.4 `GenericSparql.php` with one syntax correction: `FILTER (?depr = True)` becomes `FILTER (?depr = true)`. This preserves vocabulary statistics with standards-compliant Oxigraph. The correction affects no data.

Restart the existing Skosmos container with `docker start knowledge-layer-graph-lab-skosmos`. To recreate that comparison container after deliberately removing it, use the already cached official **v3.4, linux/amd64** image and existing config:

```sh
docker run -d --name knowledge-layer-graph-lab-skosmos \
  --pull never --platform linux/amd64 \
  --publish 127.0.0.1:4183:80 --read-only \
  --memory 384m --memory-swap 768m --cpus 1 --pids-limit 96 \
  --tmpfs /tmp:rw,size=64m \
  --tmpfs /var/run/apache2:rw,size=8m \
  --tmpfs /var/lock/apache2:rw,size=8m \
  --tmpfs /var/log/apache2:rw,size=16m \
  --mount "type=bind,src=$PWD/.local/graph-lab/skosmos-config/config.ttl,dst=/var/www/html/config.ttl,readonly" \
  --mount "type=bind,src=$PWD/.local/graph-lab/skosmos-config/GenericSparql.php,dst=/var/www/html/src/model/sparql/GenericSparql.php,readonly" \
  quay.io/natlibfi/skosmos@sha256:4f9503f6a57e3c2b5ee73d7f224784ab45edeecac78c4a6ebd9edcdfefbb454b
```

The config uses Docker Desktop's `host.docker.internal:4182/sparql`, Generic SPARQL, English, a 30-second SPARQL timeout, 20 search results, and bounded transitive/list navigation. It selects `urn:kl:graph:vocabulary`. This setup has no Fuseki/Varnish services or customer source mounts. It uses the image's default Apache user setup and bridge network; it does not claim non-root execution or outbound network isolation. Stop only this comparison container with `docker stop knowledge-layer-graph-lab-skosmos`.

Package pins and upstream attribution are in [Graph lab third-party credits](graph-lab-credits.md).
