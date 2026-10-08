import KnowledgeGraphCanvas from '../KnowledgeGraphCanvas';
import type { GraphLabRendererProps } from './types';

export { boundedConnectivityPath } from '../KnowledgeGraphCanvas';

export default function G6Lab(props: GraphLabRendererProps) {
  return <KnowledgeGraphCanvas {...props} variant="lab" />;
}
