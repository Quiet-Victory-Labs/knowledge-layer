import { useEffect, useMemo, useRef, useState } from 'react';
import * as Z from 'graph-explorer';
import type { GraphLabRendererProps } from './types';
import { graphKindColors } from './types';
import { BoundedGraphIndex, downloadDiagram, graphLinkIri, graphPropertyIri, readableProperty, pruneDiagram, validateDiagram } from './rdfGraphData';
import './rdfLibraries.css';

export class ZazukoJsonProvider implements Z.DataProvider {
  constructor(readonly index: BoundedGraphIndex) {}
  private labels(value: string) { return { values: [{ value, language: '' }] }; }
  private properties(values: Record<string, string[]>): Record<string, Z.Property> {
    return Object.fromEntries(Object.entries(values).map(([id, items]) => [id, { type: 'string', values: items.map(value => ({ value, language: '' })) }]));
  }
  private node(node: Parameters<BoundedGraphIndex['nodeProperties']>[0]): Z.ElementModel {
    return { id: node.id as Z.ElementIri, types: [`urn:knowledge-layer:kind:${node.kind}` as Z.ElementTypeIri], label: this.labels(node.label), properties: this.properties(this.index.nodeProperties(node)), sources: node.connection_id ? [node.connection_id] : [] };
  }
  async classTree(): Promise<Z.ClassModel[]> { return this.index.classes().map(type => ({ ...type, id: type.id as Z.ElementTypeIri, label: this.labels(type.label), children: [] })); }
  async linkTypes(): Promise<Z.LinkType[]> { return this.index.linkTypes().map(type => ({ ...type, id: type.id as Z.LinkTypeIri, label: this.labels(type.label) })); }
  async classInfo(params: Parameters<Z.DataProvider['classInfo']>[0]) { return (await this.classTree()).filter(type => params.classIds.includes(type.id)); }
  async propertyInfo(params: { propertyIds: Z.PropertyTypeIri[] }): Promise<Z.Dictionary<Z.PropertyModel>> {
    return Object.fromEntries(params.propertyIds.map(id => [id, { id, label: this.labels(readableProperty(id)) }]));
  }
  async linkTypesInfo(params: Parameters<Z.DataProvider['linkTypesInfo']>[0]) { return (await this.linkTypes()).filter(type => params.linkTypeIds.includes(type.id)); }
  async elementInfo(params: Parameters<Z.DataProvider['elementInfo']>[0]): Promise<Z.Dictionary<Z.ElementModel>> {
    return Object.fromEntries(params.elementIds.flatMap(id => { const node = this.index.nodes.get(id); return node ? [[id, this.node(node)]] : []; }));
  }
  async linksInfo(params: Parameters<Z.DataProvider['linksInfo']>[0]): Promise<Z.LinkModel[]> {
    const ids = new Set<string>(params.elementIds);
    return this.index.groupedEdges().filter(([edge]) => ids.has(edge.subject_id) && ids.has(edge.object_id) && (!params.linkTypeIds.length || params.linkTypeIds.includes(graphLinkIri(edge) as Z.LinkTypeIri)))
      .map(group => ({ sourceId: group[0].subject_id as Z.ElementIri, targetId: group[0].object_id as Z.ElementIri, linkTypeId: graphLinkIri(group[0]) as Z.LinkTypeIri, properties: this.properties(this.index.linkProperties(group)) }));
  }
  async linkTypesOf(params: Parameters<Z.DataProvider['linkTypesOf']>[0]): Promise<Z.LinkCount[]> { return this.index.stats(params.elementId).map(stat => ({ ...stat, id: stat.id as Z.LinkTypeIri })); }
  async linkElements(params: Z.LinkElementsParams): Promise<Z.Dictionary<Z.ElementModel>> {
    return this.filter({ refElementId: params.elementId, refElementLinkId: params.linkId || undefined, linkDirection: params.direction, limit: params.limit, offset: params.offset, languageCode: '' });
  }
  async filter(params: Z.FilterParams): Promise<Z.Dictionary<Z.ElementModel>> { return Object.fromEntries(this.index.lookup(params).map(item => [item.node.id, this.node(item.node)])); }
}

// Graph Explorer ships a non-remembering default. Its public CommandHistory
// extension point accepts an in-memory inverse-command history for local edits.
export class LocalDiagramHistory implements Z.CommandHistory {
  readonly events = new Z.EventSource<Z.CommandHistoryEvents>();
  readonly undoStack: Z.Command[] = [];
  readonly redoStack: Z.Command[] = [];
  private batches: Array<{ title?: string; commands: Z.Command[] }> = [];
  private changed() { this.events.trigger('historyChanged', { hasChanges: this.undoStack.length > 0 }); }
  reset() { this.undoStack.length = 0; this.redoStack.length = 0; this.batches = []; this.changed(); }
  undo() { const command = this.undoStack.pop(); if (command) { this.redoStack.push(command.invoke()); this.changed(); } }
  redo() { const command = this.redoStack.pop(); if (command) { this.undoStack.push(command.invoke()); this.changed(); } }
  execute(command: Z.Command) { this.registerToUndo(command.invoke()); }
  registerToUndo(command: Z.Command) {
    const batch = this.batches.at(-1);
    if (batch) batch.commands.push(command);
    else { this.undoStack.push(command); this.redoStack.length = 0; this.changed(); }
  }
  startBatch(title?: string): Z.Batch {
    const batch = { title, commands: [] as Z.Command[] };
    this.batches.push(batch);
    const compound = (commands: Z.Command[]): Z.Command => ({ title, invoke: () => compound([...commands].reverse().map(command => command.invoke())) });
    const finish = (store: boolean) => {
      const position = this.batches.indexOf(batch);
      if (position < 0) return;
      this.batches.splice(position, 1);
      if (store && batch.commands.length) this.registerToUndo(compound(batch.commands));
    };
    return { history: this, store: () => finish(true), discard: () => finish(false) };
  }
}

export default function ZazukoLab(props: GraphLabRendererProps) {
  const workspace = useRef<Z.Workspace | null>(null);
  const callbacks = useRef(props);
  callbacks.current = props;
  const provider = useMemo(() => new ZazukoJsonProvider(new BoundedGraphIndex(props.nodes, props.edges)), [props.nodes, props.edges]);
  const history = useMemo(() => new LocalDiagramHistory(), []);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [historyCounts, setHistoryCounts] = useState({ undo: 0, redo: 0 });
  const [error, setError] = useState<string | null>(null);
  const initialized = useRef(false);
  const operation = useRef(0);

  useEffect(() => {
    const update = () => setHistoryCounts({ undo: history.undoStack.length, redo: history.redoStack.length });
    history.events.on('historyChanged', update);
    return () => history.events.off('historyChanged', update);
  }, [history]);

  useEffect(() => {
    const instance = workspace.current;
    if (!instance) return;
    const model = instance.getModel();
    const first = !initialized.current;
    const diagram = initialized.current ? pruneDiagram(model.exportLayout(), provider.index) : undefined;
    let disposed = false;
    const version = ++operation.current;
    setError(null);
    void (async () => {
      await model.importLayout({ dataProvider: provider, diagram, validateLinks: true });
      if (disposed || operation.current !== version) return;
      initialized.current = true;
      const present = new Set(model.elements.map(element => element.iri));
      let added = 0;
      for (const node of provider.index.nodes.values()) if (!present.has(node.id as Z.ElementIri)) {
        const element = model.createElement(node.id as Z.ElementIri);
        element.setPosition({ x: (added % 5) * 290, y: Math.floor(added / 5) * 150 });
        added++;
      }
      await model.requestElementData([...provider.index.nodes.keys()] as Z.ElementIri[]);
      await model.requestLinksOfType();
      if (disposed || operation.current !== version) return;
      instance.getDiagram().performSyncUpdate();
      if (first) instance.forceLayout();
      instance.zoomToFit();
    })().catch(reason => { if (!disposed) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => { disposed = true; };
  }, [provider]);

  const select = () => {
    const item = workspace.current?.getEditor().selection[0];
    if (!item) { setSelectedId(null); return; }
    if (item instanceof Z.Element) {
      setSelectedId(item.iri);
      callbacks.current.onSelect(item.iri);
    } else {
      setSelectedId(null);
      const edge = callbacks.current.edges.find(edge => edge.subject_id === item.data.sourceId && edge.object_id === item.data.targetId && graphLinkIri(edge) === item.typeId);
      if (edge) callbacks.current.onSelect(edge.id);
    }
  };

  const loadDiagram = async (file: File) => {
    try {
      if (file.size > 5_000_000) throw new Error('Diagram files must be smaller than 5 MB.');
      const diagram: unknown = JSON.parse(await file.text());
      validateDiagram(diagram, provider.index);
      const instance = workspace.current;
      if (!instance) return;
      await instance.getModel().importLayout({ dataProvider: provider, diagram: diagram as unknown as Z.SerializedDiagram, validateLinks: true });
      await instance.getModel().requestLinksOfType();
      instance.zoomToFit();
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };

  return <div className="rdf-library-lab">
    <div className="rdf-library-lab__controls">
      <button type="button" disabled={!selectedId} onClick={() => selectedId && props.onExpand(selectedId)}>Fetch selected neighborhood</button>
      <button type="button" disabled={!historyCounts.undo} onClick={() => workspace.current?.undo()}>Undo ({historyCounts.undo})</button>
      <button type="button" disabled={!historyCounts.redo} onClick={() => workspace.current?.redo()}>Redo ({historyCounts.redo})</button>
      <label className="rdf-library-lab__file">Load diagram JSON<input type="file" accept=".json,application/json" onChange={event => { const file = event.currentTarget.files?.[0]; if (file) void loadDiagram(file); event.currentTarget.value = ''; }} /></label>
      <span>{props.nodes.length} authorized nodes · {provider.index.edges.length} relationships</span>
    </div>
    {error && <p className="rdf-library-lab__error" role="alert">{error}</p>}
    <div className="rdf-library-lab__canvas">
      <Z.Workspace ref={workspace} history={history} leftPanelInitiallyOpen rightPanelInitiallyOpen collapseNavigator
        onSaveDiagram={instance => downloadDiagram('knowledge-layer-zazuko-diagram.json', instance.getModel().exportLayout())}
        onWorkspaceEvent={event => { if (event === Z.WorkspaceEventKey.editorChangeSelection) select(); }}
        onPointerUp={event => { if (event.triggerAsClick && event.target instanceof Z.Element) { setSelectedId(event.target.iri); callbacks.current.onSelect(event.target.iri); } }}
        typeStyleResolver={types => ({ color: graphKindColors[types[0]?.split(':').pop() || ''] || '#638566' })}
        viewOptions={{ onIriClick: event => { event.originalEvent.preventDefault(); callbacks.current.onSelect(event.iri); } }}
        linkTemplateResolver={() => ({ markerTarget: { d: 'M 0 0 L 10 5 L 0 10 z', width: 10, height: 10, fill: '#637d72', stroke: '#637d72' }, renderLink: link => {
          const status = link.data.properties?.[graphPropertyIri('status')];
          const value = status?.type === 'string' ? status.values[0]?.value : '';
          return { connection: { stroke: ['candidate', 'proposed', 'pending', 'pending_review', 'unreviewed'].includes(value || '') ? '#b97d36' : '#637d72', 'stroke-dasharray': ['candidate', 'proposed', 'pending', 'pending_review', 'unreviewed'].includes(value || '') ? '5 3' : '' } };
        } })}
      />
    </div>
    <p className="rdf-library-lab__notice">Native Zazuko class, search, connection and relationship panels explore the loaded graph. Its toolbar saves diagrams, lays out, zooms, prints and exports SVG/PNG. Fetch selected neighborhood requests more authorized data. Diagram edits stay local; this library has no native annotation tool. Updating source filters reloads diagram history.</p>
  </div>;
}
