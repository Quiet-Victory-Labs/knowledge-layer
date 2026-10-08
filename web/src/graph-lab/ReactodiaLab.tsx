import { useEffect, useMemo, useRef, useState } from 'react';
import * as R from '@reactodia/workspace';
import LayoutWorker from '@reactodia/workspace/layout.worker?worker';
import type { GraphLabRendererProps } from './types';
import { graphKindColors } from './types';
import { BoundedGraphIndex, downloadDiagram, graphLinkIri, readableProperty, pruneDiagram, validateDiagram } from './rdfGraphData';
import './rdfLibraries.css';

const layouts = R.defineLayoutWorker(() => new LayoutWorker());
const labelProperty = 'http://www.w3.org/2000/01/rdf-schema#label';

export class WorkspaceJsonProvider extends R.EmptyDataProvider {
  constructor(readonly index: BoundedGraphIndex) { super(); }
  private literals(values: string[]) { return values.map(value => this.factory.literal(value, '')); }
  private properties(values: Record<string, string[]>) {
    return Object.fromEntries(Object.entries(values).map(([id, items]) => [id, this.literals(items)]));
  }
  private node(node: Parameters<BoundedGraphIndex['nodeProperties']>[0]): R.ElementModel {
    return { id: node.id, types: [`urn:knowledge-layer:kind:${node.kind}`], properties: { ...this.properties(this.index.nodeProperties(node)), [labelProperty]: this.literals([node.label]) } };
  }
  async knownElementTypes() { return { elementTypes: this.index.classes().map(type => ({ ...type, label: this.literals([type.label]) })), subtypeOf: [] }; }
  async knownLinkTypes() { return this.index.linkTypes().map(type => ({ ...type, label: this.literals([type.label]) })); }
  async elementTypes(params: Parameters<R.DataProvider['elementTypes']>[0]) {
    return new Map((await this.knownElementTypes()).elementTypes.filter(type => params.classIds.includes(type.id)).map(type => [type.id, type]));
  }
  async propertyTypes(params: Parameters<R.DataProvider['propertyTypes']>[0]) {
    return new Map(params.propertyIds.map(id => [id, { id, label: this.literals([readableProperty(id)]) }]));
  }
  async linkTypes(params: Parameters<R.DataProvider['linkTypes']>[0]) {
    return new Map((await this.knownLinkTypes()).filter(type => params.linkTypeIds.includes(type.id)).map(type => [type.id, type]));
  }
  async elements(params: Parameters<R.DataProvider['elements']>[0]) {
    return new Map(params.elementIds.flatMap(id => { const node = this.index.nodes.get(id); return node ? [[id, this.node(node)] as const] : []; }));
  }
  async links(params: Parameters<R.DataProvider['links']>[0]): Promise<R.LinkModel[]> {
    const primary = new Set<string>(params.primary), secondary = new Set<string>(params.secondary);
    return this.index.groupedEdges().filter(([edge]) => (!params.linkTypeIds || params.linkTypeIds.includes(graphLinkIri(edge)))
      && (primary.has(edge.subject_id) && secondary.has(edge.object_id) || secondary.has(edge.subject_id) && primary.has(edge.object_id)))
      .map(group => ({ sourceId: group[0].subject_id, targetId: group[0].object_id, linkTypeId: graphLinkIri(group[0]), properties: this.properties(this.index.linkProperties(group)) }));
  }
  async connectedLinkStats(params: Parameters<R.DataProvider['connectedLinkStats']>[0]) { return this.index.stats(params.elementId); }
  async lookup(params: R.DataProviderLookupParams): Promise<R.DataProviderLookupItem[]> {
    return this.index.lookup(params).map(item => ({ ...item, element: this.node(item.node) }));
  }
}

function ReactodiaContents(props: GraphLabRendererProps & { workerLayouts: R.DefaultLayouts }) {
  const context = R.useWorkspace();
  const provider = useMemo(() => new WorkspaceJsonProvider(new BoundedGraphIndex(props.nodes, props.edges)), [props.nodes, props.edges]);
  const callbacks = useRef(props);
  callbacks.current = props;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const initialized = useRef(false);
  const operation = useRef(0);

  const locale = useMemo(() => {
    const value = new R.DefaultDataLocaleProvider({ model: context.model, translation: context.translation });
    value.prepareAnchor = id => ({ href: '#', onClick: event => { event.preventDefault(); callbacks.current.onSelect(id); } });
    return value;
  }, [context]);

  useEffect(() => {
    const abort = new AbortController();
    const version = ++operation.current;
    const model = context.model;
    const first = !initialized.current;
    const diagram = initialized.current ? pruneDiagram(model.exportLayout(), provider.index) : undefined;
    setError(null);
    void (async () => {
      await model.importLayout({ dataProvider: provider, locale, diagram, signal: abort.signal });
      if (abort.signal.aborted || operation.current !== version) return;
      initialized.current = true;
      const present = new Set(model.elements.flatMap(element => [...R.iterateEntitiesOf(element)].map(entity => entity.id)));
      let added = 0;
      for (const node of provider.index.nodes.values()) if (!present.has(node.id)) {
        const element = model.createElement(node.id);
        element.setPosition({ x: (added % 5) * 290, y: Math.floor(added / 5) * 150 });
        added++;
      }
      await model.requestData();
      if (abort.signal.aborted) return;
      if (first) await context.performLayout({ signal: abort.signal });
      else await context.view.findAnyCanvas()?.zoomToFit();
    })().catch(reason => { if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => abort.abort();
  }, [provider, context, locale]);

  useEffect(() => {
    const select = () => {
      const item = context.model.selection[0];
      if (!item) { setSelectedId(null); return; }
      if (item instanceof R.Element) {
        const id = [...R.iterateEntitiesOf(item)][0]?.id;
        setSelectedId(id || null);
        if (id) callbacks.current.onSelect(id);
      } else {
        setSelectedId(null);
        const relation = [...R.iterateRelationsOf(item)][0];
        const edge = relation && provider.index.edges.find(edge => edge.subject_id === relation.sourceId && edge.object_id === relation.targetId && graphLinkIri(edge) === relation.linkTypeId);
        if (edge) callbacks.current.onSelect(edge.id);
      }
    };
    context.model.events.on('changeSelection', select);
    return () => context.model.events.off('changeSelection', select);
  }, [context, provider]);

  const loadDiagram = async (file: File) => {
    try {
      if (file.size > 5_000_000) throw new Error('Diagram files must be smaller than 5 MB.');
      const diagram: unknown = JSON.parse(await file.text());
      validateDiagram(diagram, provider.index);
      await context.model.importLayout({ dataProvider: provider, locale, diagram: diagram as unknown as R.SerializedDiagram });
      await context.model.requestData();
      await context.view.findAnyCanvas()?.zoomToFit();
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };

  const annotate = () => context.getCommandBus(R.AnnotationTopic).trigger('createAnnotation', {
    targets: context.model.selection.filter((item): item is R.Element => item instanceof R.Element),
    content: { type: 'plaintext', text: 'Local diagram note — edit this text' },
  });

  return <div className="rdf-library-lab">
    <div className="rdf-library-lab__controls">
      <button type="button" disabled={!selectedId} onClick={() => selectedId && props.onExpand(selectedId)}>Fetch selected neighborhood</button>
      <button type="button" onClick={annotate}>Add local note</button>
      <button type="button" onClick={() => void context.performLayout({ layoutFunction: props.workerLayouts.flowLayout }).catch(reason => setError(String(reason)))}>Flow layout</button>
      <button type="button" onClick={() => void context.performLayout({ layoutFunction: props.workerLayouts.forceLayout }).catch(reason => setError(String(reason)))}>Force layout</button>
      <span>{props.nodes.length} authorized nodes · {provider.index.edges.length} relationships</span>
    </div>
    {error && <p className="rdf-library-lab__error" role="alert">{error}</p>}
    <div className="rdf-library-lab__canvas">
      <R.DefaultWorkspace colorScheme="light" dropOnCanvas={null} menu={<>
        <R.ToolbarAction onSelect={() => downloadDiagram('knowledge-layer-reactodia-diagram.json', context.model.exportLayout())}>Save diagram JSON</R.ToolbarAction>
        <R.ToolbarActionOpen fileAccept=".json,application/json" onSelect={file => void loadDiagram(file)}>Load diagram JSON</R.ToolbarActionOpen>
        <R.ToolbarActionExport kind="exportSvg" fileName="knowledge-layer-reactodia.svg" />
        <R.ToolbarActionExport kind="exportRaster" fileName="knowledge-layer-reactodia.png" />
        <R.ToolbarActionExport kind="print" />
        <R.ToolbarActionClearAll />
      </>} />
    </div>
    <p className="rdf-library-lab__notice">Native Reactodia search includes nodes, classes and relationship types. Connections explore the loaded graph; Fetch selected neighborhood requests more authorized data. Notes, grouping and diagram edits stay local. Updating source filters reloads diagram history.</p>
  </div>;
}

export default function ReactodiaLab(props: GraphLabRendererProps) {
  const workerLayouts = R.useWorker(layouts);
  return <R.Workspace defaultLayout={workerLayouts.defaultLayout} typeStyleResolver={types => ({ color: graphKindColors[types[0]?.split(':').pop() || ''] || '#638566' })}>
    <ReactodiaContents {...props} workerLayouts={workerLayouts} />
  </R.Workspace>;
}
